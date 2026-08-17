"""Exploration-Aware Investigator.

The agent does NOT ask "what should I do next?". Instead it asks:
  1. What evidence is missing?
  2. Which unexplored directory/module is most likely to contain it?

LLM is used only for strategic planning (missing-evidence detection and
directory selection). Tactical execution (reading files in a directory) is
done deterministically by the worklist.
"""
import json
import sys
from pathlib import Path
from typing import Dict, List, Set

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_ROOT))

from config import get_repo_root
from src.qa.investigation.base import BaseInvestigator, InvestigationState, load_prompt
from src.qa.investigation.worklist import CheapLLMClient
from src.qa.retrievers.fast_embedding import FastEmbeddingRetriever
from src.search.code_reader import read_file_lines


class ExplorationAwareInvestigator(BaseInvestigator):
    """Exploration-Aware Investigator.

    Combines:
      - Deterministic scan of Top-K candidate files
      - LLM Missing-Evidence Detector
      - LLM Directory-Level Planner
      - Deterministic batch reading inside selected directory
      - Optional Counter-Hypothesis check
    """

    def __init__(
        self,
        max_scan: int = 20,
        max_exploration_rounds: int = 5,
        files_per_directory: int = 5,
        lines_per_file: int = 100,
        min_files_before_finish: int = 10,
        repo_path: str = None,
        coverage_only: bool = True,
    ):
        # max_steps is not used the same way; exploration rounds drive the loop
        super().__init__(max_steps=max_exploration_rounds, repo_path=repo_path)
        self.max_scan = max_scan
        self.max_exploration_rounds = max_exploration_rounds
        self.files_per_directory = files_per_directory
        self.lines_per_file = lines_per_file
        self.min_files_before_finish = min_files_before_finish
        self.coverage_only = coverage_only
        self.nav_llm = CheapLLMClient()
        self.missing_prompt = load_prompt("exploration_missing_evidence")
        self.planner_prompt = load_prompt("exploration_directory_planner")
        self.counter_prompt = load_prompt("exploration_counter_hypothesis")
        self.retriever: FastEmbeddingRetriever | None = None

        # Runtime state
        self.visited_files: List[str] = []
        self.visited_dirs: Set[str] = set()
        self.all_dirs: Set[str] = set()
        self.dir_files: Dict[str, List[str]] = {}
        self.log: List[dict] = []

    def _normalize_file_path(self, file_path: str) -> str:
        if not file_path:
            return file_path
        if self.repo_path_obj and file_path.startswith(str(self.repo_path_obj)):
            rel = Path(file_path).relative_to(self.repo_path_obj)
            return str(rel)
        return file_path.lstrip("./").lstrip("/")

    def _get_retriever(self) -> FastEmbeddingRetriever:
        if self.retriever is None:
            self.retriever = FastEmbeddingRetriever(repo_root=str(self.repo_path_obj) if self.repo_path_obj else "")
        return self.retriever

    def _build_directory_map(self) -> None:
        """Build directory -> files map from the embedding index."""
        retriever = self._get_retriever()
        for ch in retriever.chunks:
            fp = ch.get("meta", {}).get("file_path", "")
            if not fp:
                continue
            fp = self._normalize_file_path(fp)
            d = str(Path(fp).parent)
            self.all_dirs.add(d)
            self.dir_files.setdefault(d, []).append(fp)
        # Deduplicate files in each directory
        for d in self.dir_files:
            seen = set()
            unique = []
            for fp in self.dir_files[d]:
                if fp not in seen:
                    seen.add(fp)
                    unique.append(fp)
            self.dir_files[d] = unique

    def _read_file(self, fp: str) -> str:
        fp = self._normalize_file_path(fp)
        if fp in self.state.files_content:
            return self.state.files_content[fp]
        try:
            content = read_file_lines(fp, 1, self.lines_per_file)
            self.visited_files.append(fp)
            self.state.visited_files.append(fp)
            self.state.files_content[fp] = content
            self.visited_dirs.add(str(Path(fp).parent))
            return content
        except Exception as e:
            self.state.files_content[fp] = f"[读取失败: {e}]"
            return ""

    def _scan_candidates(self, candidate_files: List[str]) -> None:
        """Phase 1: deterministically scan the initial candidate pool."""
        for fp in candidate_files[: self.max_scan]:
            self._read_file(fp)

    def _summaries_text(self, max_files: int = 20, max_chars: int = 600) -> str:
        parts = []
        for fp in self.visited_files[:max_files]:
            content = self.state.files_content.get(fp, "")
            snippet = content[:max_chars].replace("\n", " ")
            parts.append(f"=== {fp} ===\n{snippet}")
        return "\n\n".join(parts)

    def _missing_evidence_analysis(self, question: str) -> dict:
        """Ask LLM: what is missing?"""
        unvisited = sorted(self.all_dirs - self.visited_dirs)[:30]
        prompt = self.missing_prompt.format(
            question=question,
            file_summaries=self._summaries_text(),
            visited_dirs="\n".join(f"- {d}" for d in sorted(self.visited_dirs)) or "(无)",
            unvisited_dirs="\n".join(f"- {d}" for d in unvisited) or "(无)",
        )
        result = self.nav_llm.call_json(prompt, max_tokens=1500)
        if "error" in result:
            return {
                "supported_hypothesis": "",
                "missing_evidence": "",
                "unexplored_regions": "",
                "falsification_target": "",
                "suggested_search_queries": [],
            }
        # The prompt asks for plain sections, but LLM may return JSON or text.
        # Try to parse whichever.
        text = result.get("raw", "")
        if not text:
            text = json.dumps(result, ensure_ascii=False)
        queries = result.get("SUGGESTED_SEARCH_QUERIES", "")
        if isinstance(queries, str):
            queries = [q.strip().lstrip("-").strip() for q in queries.split("\n") if q.strip()]
        return {
            "supported_hypothesis": result.get("SUPPORTED_HYPOTHESIS", ""),
            "missing_evidence": result.get("MISSING_EVIDENCE", ""),
            "unexplored_regions": result.get("UNEXPLORED_REGIONS", ""),
            "falsification_target": result.get("FALSIFICATION_TARGET", ""),
            "suggested_search_queries": queries,
            "raw": text,
        }

    def _get_query_grounded_directories(
        self, queries: List[str], query_emb
    ) -> tuple[List[str], Dict[str, List[str]]]:
        """Retrieve files for suggested queries and return their directories."""
        retriever = self._get_retriever()
        dir_to_files: Dict[str, List[str]] = {}
        all_files: List[str] = []

        # Also use original question embedding to ground directories
        results = retriever.retrieve(query_emb, top_k=20)
        for r in results:
            fp = self._normalize_file_path(r["metadata"].get("file_path", ""))
            if fp:
                all_files.append(fp)

        for q in queries:
            if not q:
                continue
            try:
                emb = retriever.encode_queries([q])
                results = retriever.retrieve(emb, top_k=5)
                for r in results:
                    fp = self._normalize_file_path(r["metadata"].get("file_path", ""))
                    if fp:
                        all_files.append(fp)
            except Exception:
                continue

        for fp in all_files:
            d = str(Path(fp).parent)
            if d not in dir_to_files:
                dir_to_files[d] = []
            if fp not in dir_to_files[d]:
                dir_to_files[d].append(fp)

        # Prefer unvisited directories
        unvisited_dirs = [d for d in dir_to_files if d not in self.visited_dirs]
        return unvisited_dirs, dir_to_files

    def _select_directory(
        self,
        question: str,
        missing_analysis: dict,
        candidate_dirs: List[str],
        dir_to_files: Dict[str, List[str]],
    ) -> tuple[str, str]:
        """Ask LLM: which directory to explore next?"""
        if not candidate_dirs:
            # Fallback to any unvisited directory with files
            unvisited = sorted(self.all_dirs - self.visited_dirs)
            if not unvisited:
                return "", "no unvisited directories"
            candidate_dirs = unvisited

        # Provide directory summaries: top file names + file count
        dir_summaries = []
        for d in candidate_dirs[:15]:
            files = dir_to_files.get(d, self.dir_files.get(d, []))[:5]
            dir_summaries.append(f"- {d} ({len(self.dir_files.get(d, []))} files): {files}")

        prompt = self.planner_prompt.format(
            question=question,
            visited_dirs="\n".join(f"- {d}" for d in sorted(self.visited_dirs)) or "(无)",
            unvisited_dirs="\n".join(dir_summaries) or "(无)",
            findings=missing_analysis.get("supported_hypothesis", ""),
            missing_evidence=missing_analysis.get("missing_evidence", ""),
        )
        result = self.nav_llm.call_json(prompt, max_tokens=400)
        if "error" in result:
            # Fallback: pick the directory with most files
            d = max(candidate_dirs, key=lambda x: len(self.dir_files.get(x, [])))
            return d, "fallback to largest candidate directory"
        directory = result.get("directory", "")
        reason = result.get("reason", "")

        # Normalize directory path
        directory = directory.lstrip("./").lstrip("/")
        if directory and directory not in self.dir_files:
            # Try to find closest match
            for d in self.dir_files:
                if d.endswith(directory) or directory.endswith(d):
                    directory = d
                    break
        # If still not valid, pick first candidate
        if not directory or directory not in self.dir_files:
            directory = candidate_dirs[0]
            reason = (reason or "") + " [normalized to first candidate]"
        return directory, reason

    def _read_directory(self, directory: str, query_emb) -> List[str]:
        """Deterministically read top files in the selected directory."""
        files = self.dir_files.get(directory, [])
        if not files:
            return []

        # Score files in this directory by query embedding
        retriever = self._get_retriever()
        file_filter = set(files)
        results = retriever.retrieve(query_emb, top_k=self.files_per_directory, file_filter=file_filter)

        read_files = []
        for r in results:
            fp = self._normalize_file_path(r["metadata"].get("file_path", ""))
            if fp and fp not in self.state.files_content:
                self._read_file(fp)
                read_files.append(fp)
        return read_files

    def _counter_hypothesis_check(self, question: str, leading_hypothesis: str) -> dict:
        """Optional: ask LLM to find evidence that would falsify the hypothesis."""
        unvisited = sorted(self.all_dirs - self.visited_dirs)[:30]
        prompt = self.counter_prompt.format(
            question=question,
            leading_hypothesis=leading_hypothesis,
            file_summaries=self._summaries_text(),
            visited_dirs="\n".join(f"- {d}" for d in sorted(self.visited_dirs)) or "(无)",
            unvisited_dirs="\n".join(f"- {d}" for d in unvisited) or "(无)",
        )
        result = self.nav_llm.call_json(prompt, max_tokens=1500)
        return result

    def run(self, question: str, candidate_files: List[str]) -> dict:
        """Run exploration-aware investigation.

        Args:
            question: Original question.
            candidate_files: Ranked list of candidate files (e.g., Top20 pool).

        Returns:
            Dict with visited_files, log, and answer.
        """
        self.state = InvestigationState(
            question=question,
            entry_file=self._normalize_file_path(candidate_files[0]) if candidate_files else "",
            max_steps=self.max_exploration_rounds,
        )
        self.visited_files = []
        self.visited_dirs = set()
        self.log = []
        self._build_directory_map()

        # Phase 1: initial deterministic scan
        self._scan_candidates(candidate_files)

        query_emb = self._get_retriever().encode_queries([question])

        leading_hypothesis = ""

        # Phase 2: exploration rounds
        for round_num in range(1, self.max_exploration_rounds + 1):
            unvisited = self.all_dirs - self.visited_dirs
            if not unvisited:
                break

            # Missing evidence analysis
            missing = self._missing_evidence_analysis(question)
            leading_hypothesis = missing.get("supported_hypothesis", leading_hypothesis)

            # Ground directory options with suggested search queries + original query
            candidate_dirs, dir_to_files = self._get_query_grounded_directories(
                missing.get("suggested_search_queries", []), query_emb
            )

            # Optional counter-hypothesis check disabled by default for speed
            counter = {}

            # Directory planner: choose among grounded candidate directories
            directory, reason = self._select_directory(question, missing, candidate_dirs, dir_to_files)

            # Read top files in selected directory
            read_files = self._read_directory(directory, query_emb)

            self.log.append({
                "round": round_num,
                "missing_analysis": missing,
                "counter_analysis": counter,
                "selected_directory": directory,
                "selection_reason": reason,
                "read_files": read_files,
            })

        # Phase 3: answer generation (skip if coverage_only)
        if not self.coverage_only:
            for fp in self.state.visited_files:
                content = self.state.files_content.get(fp, "")
                if content and not content.startswith("["):
                    evidence = self.extract_evidence(fp, content)
                    self.state.evidence_log.append(evidence)
            answer = self.generate_answer()
        else:
            answer = ""

        return {
            "mode": "exploration_aware",
            "question": question,
            "visited_files": self.state.visited_files,
            "visited_dirs": sorted(self.visited_dirs),
            "files_content": self.state.files_content,
            "log": self.log,
            "answer": answer,
        }
