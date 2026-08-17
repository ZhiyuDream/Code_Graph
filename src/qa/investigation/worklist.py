"""Worklist Investigator — systematic scan + LLM-steered expansion.

Combines the strengths of deterministic traversal (no missed candidates) and
LLM-driven reasoning (targeted expansion to hard-to-retrieve files).
"""
import json
import sys
from pathlib import Path
from typing import Dict, List

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_ROOT))

from config import OPENAI_API_KEY, OPENAI_BASE_URL
from openai import OpenAI

from src.qa.investigation.base import BaseInvestigator, InvestigationState, load_prompt
from src.qa.retrievers.fast_embedding import FastEmbeddingRetriever
from src.search.code_reader import read_file_lines, read_full_file
from src.search.call_chain import get_callers, get_callees
from src.search.grep_search_v2 import grep_files


class CheapLLMClient:
    """Cheap LLM client for navigation decisions (gpt-4.1-mini)."""

    def __init__(self, model: str = "gpt-4.1-mini"):
        self.client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)
        self.model = model

    def call_json(self, prompt: str, temperature: float = 0.2, max_tokens: int = 2000) -> dict:
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
            text = resp.choices[0].message.content.strip()
            return json.loads(text)
        except Exception as e:
            return {"error": str(e), "action": "finish", "action_input": {"reason": f"LLM error: {e}"}}


class WorklistInvestigator(BaseInvestigator):
    """Worklist Investigator.

    1. Scan all candidate files (first N lines).
    2. Use a cheap LLM to decide expansion actions (read_full, find_callers,
       find_callees, search_symbol, search_files).
    3. Extract evidence from visited files and generate final answer.
    """

    def __init__(
        self,
        max_scan: int = 20,
        max_steps: int = 8,
        lines_per_scan: int = 100,
        repo_path: str = None,
        coverage_only: bool = False,
    ):
        super().__init__(max_steps, repo_path)
        self.max_scan = max_scan
        self.lines_per_scan = lines_per_scan
        self.coverage_only = coverage_only
        self.nav_llm = CheapLLMClient()
        self.select_prompt = load_prompt("worklist_select_next")
        self.retriever: FastEmbeddingRetriever | None = None
        self.expanded_symbols: set[str] = set()
        self.action_log: List[dict] = []

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

    def _scan_candidates(self, candidate_files: List[str]) -> Dict[str, str]:
        """Read first N lines of each candidate file."""
        summaries = {}
        for fp in candidate_files[: self.max_scan]:
            fp = self._normalize_file_path(fp)
            if not fp or fp in self.state.files_content:
                continue
            try:
                content = read_file_lines(fp, 1, self.lines_per_scan)
                summaries[fp] = content
                self.state.visited_files.append(fp)
                self.state.files_content[fp] = content
            except Exception as e:
                summaries[fp] = f"[读取失败: {e}]"
        return summaries

    def _execute_action(self, action: str, action_input: dict) -> tuple[str, List[str]]:
        """Execute one expansion action. Returns (observation, new_files)."""
        observation = ""
        new_files: List[str] = []

        if action == "read_full":
            fp = self._normalize_file_path(action_input.get("file_path", ""))
            if fp:
                content = self.read_file(fp)
                self.state.files_content[fp] = content
                observation = f"已读取 {fp} 完整内容（{len(content)} 字符）"
                new_files = [fp]

        elif action == "find_callers":
            symbol = action_input.get("symbol", "")
            if symbol:
                callers = get_callers(symbol, limit=10)
                files = sorted(set(c.get("file", "") for c in callers if c.get("file")))
                observation = f"找到 {len(callers)} 个 '{symbol}' 的调用者，涉及文件: {files}"
                new_files = files

        elif action == "find_callees":
            symbol = action_input.get("symbol", "")
            if symbol:
                callees = get_callees(symbol, limit=10)
                files = sorted(set(c.get("file", "") for c in callees if c.get("file")))
                observation = f"找到 {len(callees)} 个 '{symbol}' 的被调用者，涉及文件: {files}"
                new_files = files

        elif action == "search_symbol":
            symbol = action_input.get("symbol", "")
            if symbol:
                files = grep_files(symbol, self.repo_path, limit=10)
                observation = f"搜索符号 '{symbol}' 找到 {len(files)} 个文件: {files}"
                new_files = files

        elif action == "search_files":
            query = action_input.get("query", "")
            if query:
                retriever = self._get_retriever()
                emb = retriever.encode_queries([query])
                results = retriever.retrieve(emb, top_k=5)
                files = []
                seen = set()
                for r in results:
                    fp = self._normalize_file_path(r["metadata"].get("file_path", ""))
                    if fp and fp not in seen:
                        seen.add(fp)
                        files.append(fp)
                observation = f"查询 '{query}' 检索到 {len(files)} 个文件: {files}"
                new_files = files

        elif action == "finish":
            observation = action_input.get("reason", "结束调查")

        else:
            observation = f"未知工具: {action}"

        return observation, new_files

    def _read_new_files(self, new_files: List[str]) -> None:
        """Read first N lines of newly discovered files."""
        for fp in new_files:
            fp = self._normalize_file_path(fp)
            if not fp or fp in self.state.files_content:
                continue
            try:
                content = read_file_lines(fp, 1, self.lines_per_scan)
                self.state.visited_files.append(fp)
                self.state.files_content[fp] = content
            except Exception as e:
                self.state.files_content[fp] = f"[读取失败: {e}]"

    def _build_prompt(self, question: str, step: int) -> str:
        """Build the LLM decision prompt."""
        summaries = []
        for fp in self.state.visited_files:
            content = self.state.files_content.get(fp, "")
            snippet = content[:800].replace("\n", " ")
            summaries.append(f"=== {fp} ===\n{snippet}")

        return self.select_prompt.format(
            question=question,
            file_summaries="\n\n".join(summaries[:20]),
            visited_files="\n".join(f"- {f}" for f in self.state.visited_files) or "(无)",
            expanded_symbols="\n".join(f"- {s}" for s in sorted(self.expanded_symbols)) or "(无)",
            current_step=step,
            max_steps=self.max_steps,
        )

    def run(self, question: str, candidate_files: List[str]) -> dict:
        """Run worklist investigation.

        Args:
            question: Original question.
            candidate_files: Ranked list of candidate files (e.g., from Top20 pool).

        Returns:
            Dict with investigation result.
        """
        self.state = InvestigationState(
            question=question,
            entry_file=self._normalize_file_path(candidate_files[0]) if candidate_files else "",
            max_steps=self.max_steps,
        )
        self.expanded_symbols = set()
        self.action_log = []

        # Phase 1: scan candidates
        self._scan_candidates(candidate_files)

        # Phase 2: LLM-driven expansion
        for step_num in range(1, self.max_steps + 1):
            prompt = self._build_prompt(question, step_num)
            decision = self.nav_llm.call_json(prompt, max_tokens=1000)

            action = decision.get("action", "finish")
            action_input = decision.get("action_input", {})
            if isinstance(action_input, str):
                # Try to parse string input into dict
                if action == "read_full":
                    action_input = {"file_path": action_input}
                elif action in ("find_callers", "find_callees", "search_symbol"):
                    action_input = {"symbol": action_input}
                elif action == "search_files":
                    action_input = {"query": action_input}
                else:
                    action_input = {}

            observation, new_files = self._execute_action(action, action_input)
            self._read_new_files(new_files)

            if action in ("find_callers", "find_callees", "search_symbol"):
                symbol = action_input.get("symbol", "")
                if symbol:
                    self.expanded_symbols.add(symbol)

            self.action_log.append({
                "step": step_num,
                "thought": decision.get("thought", ""),
                "action": action,
                "action_input": action_input,
                "observation": observation,
                "new_files": new_files,
            })

            if action == "finish":
                break

        # Phase 3: extract evidence and generate answer (skip if coverage_only)
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
            "mode": "worklist",
            "question": question,
            "visited_files": self.state.visited_files,
            "files_content": self.state.files_content,
            "action_log": self.action_log,
            "answer": answer,
        }
