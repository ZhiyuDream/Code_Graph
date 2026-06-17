"""
Static Investigation — 固定怀疑状态的调查。

核心特点：
- SuspicionState 初始化后固定不变
- 每步可以重新选择 frontier 中的文件
- 但选择只能基于 Question + Entry File，不能利用新证据更新 suspicion
- 这模拟了"有计划但不够灵活"的调查方式
"""
import json
import sys
from pathlib import Path
from typing import Dict, List

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_ROOT))

from src.qa.investigation.base import BaseInvestigator, InvestigationState, SuspicionState, load_prompt


class StaticInvestigator(BaseInvestigator):
    """Static Investigator。"""

    def __init__(self, max_steps: int = 10, repo_path: str = None):
        super().__init__(max_steps, repo_path)
        self.rank_prompt = load_prompt("static_rank_files")
        self._cached_ranking: List[str] = []

    def _init_suspicion(self, question: str, entry_file: str, entry_content: str) -> tuple[SuspicionState, Dict]:
        """基于问题和入口文件初始化固定怀疑状态。返回 (SuspicionState, entry_evidence)。"""
        from src.qa.investigation.base import load_prompt

        # If entry is a header, also read the corresponding implementation file
        combined_content = entry_content
        impl_file = ""
        if entry_file.endswith((".h", ".hpp")):
            for ext in (".cpp", ".c", ".cc"):
                impl_file = entry_file.rsplit(".", 1)[0] + ext
                impl_content = self.read_file(impl_file)
                if not impl_content.startswith("// 文件不存在"):
                    combined_content += f"\n\n=== {impl_file} ===\n" + impl_content
                    break

        candidate_symbols = self.extract_symbols(entry_content)
        if impl_file and not impl_content.startswith("// 文件不存在"):
            candidate_symbols += self.extract_symbols(impl_content)
        candidate_symbols = sorted(set(candidate_symbols))

        prompt = load_prompt("init_suspicion").format(
            question=question,
            file_path=entry_file,
            candidate_symbols=", ".join(candidate_symbols[:50]),
            content=self._prepare_content(entry_file, combined_content),
        )
        result = self.llm.call_json(prompt)

        symbols = result.get("suspicious_symbols", [])
        # Filter to symbols that actually appear in the content
        valid_symbols = [s for s in symbols if s in combined_content]
        if not valid_symbols:
            valid_symbols = candidate_symbols[:10]

        files = result.get("suspicious_files", [])
        if entry_file not in files:
            files = [entry_file] + files

        suspicion = SuspicionState(
            current_question=result.get("current_question", question),
            suspicious_symbols=valid_symbols[:15],
            suspicious_files=files[:10],
        )
        entry_evidence = {
            "file_path": entry_file,
            "key_facts": result.get("key_facts", []),
            "new_hypothesis": result.get("current_question", question),
            "suspicious_symbols": valid_symbols[:15],
            "suspicious_files": files[:10],
            "raw": result,
        }
        return suspicion, entry_evidence

    def _rank_frontier(self) -> List[str]:
        """基于固定 suspicion state 对 frontier 排序。"""
        if not self.state.frontier_files:
            return []

        prompt = self.rank_prompt.format(
            question=self.state.question,
            entry_file=self.state.entry_file,
            entry_symbols=", ".join(self.state.suspicion.suspicious_symbols),
            frontier_files="\n".join(f"- {f}" for f in self.state.frontier_files[:20]),
            max_files=min(5, len(self.state.frontier_files)),
        )
        result = self.llm.call_json(prompt)
        if "error" in result:
            return list(self.state.frontier_files)
        ranked = result.get("ranked_files", [])
        # Filter to only files in frontier
        return [f for f in ranked if f in self.state.frontier_files]

    def _select_next_file(self) -> str:
        """从 frontier 中选择下一个文件。Static 只基于初始 suspicion ranking 一次。"""
        if not self.state.frontier_files:
            return ""
        if not self._cached_ranking:
            self._cached_ranking = self._rank_frontier()
        # Pick first ranked file that is still in frontier
        for f in self._cached_ranking:
            if f in self.state.frontier_files:
                return f
        return self.state.frontier_files[0]

    def run(self, question: str, entry_file: str) -> Dict:
        """运行 Static Investigation。Static 只读文件、不每步做证据提取，减少 LLM 调用。"""
        self.state = InvestigationState(
            question=question,
            entry_file=entry_file,
            max_steps=self.max_steps,
        )

        # Read entry file and initialize suspicion in one LLM call
        entry_content = self.read_file(entry_file)
        self.state.visited_files.append(entry_file)
        self.state.suspicion, entry_evidence = self._init_suspicion(question, entry_file, entry_content)
        self.state.evidence_log.append(entry_evidence)

        # Initial frontier expansion: comprehensive search for top symbols
        self.bootstrap_frontier(self.state.suspicion.suspicious_symbols, top_k_symbols=2)

        # Pre-rank frontier once based on fixed suspicion
        self._cached_ranking = self._rank_frontier()

        steps = [{
            "step": 0,
            "file": entry_file,
            "suspicion": {
                "current_question": self.state.suspicion.current_question,
                "suspicious_symbols": self.state.suspicion.suspicious_symbols,
                "suspicious_files": self.state.suspicion.suspicious_files,
            },
            "new_frontier_files": list(self.state.frontier_files),
        }]

        for step_num in range(1, self.max_steps + 1):
            next_file = self._select_next_file()
            if not next_file:
                break
            next_file = self._normalize_file_path(next_file)
            if next_file in self.state.visited_files:
                if next_file in self.state.frontier_files:
                    self.state.frontier_files.remove(next_file)
                continue

            self.state.frontier_files.remove(next_file)
            self.state.visited_files.append(next_file)

            # Static mode: just read the file, no per-step evidence extraction
            self.read_file(next_file)

            # Expand frontier based on fixed initial symbols
            static_evidence = {
                "suspicious_symbols": self.state.suspicion.suspicious_symbols,
                "suspicious_files": self.state.suspicion.suspicious_files,
            }
            new_files = self.update_frontier(static_evidence, limit=3)

            steps.append({
                "step": step_num,
                "file": next_file,
                "suspicion": {
                    "current_question": self.state.suspicion.current_question,
                    "suspicious_symbols": self.state.suspicion.suspicious_symbols,
                    "suspicious_files": self.state.suspicion.suspicious_files,
                },
                "new_frontier_files": new_files,
            })

        answer = self.generate_answer()

        return {
            "mode": "static",
            "question": question,
            "entry_file": entry_file,
            "visited_files": self.state.visited_files,
            "frontier_files": self.state.frontier_files,
            "evidence_log": self.state.evidence_log,
            "steps": steps,
            "answer": answer,
        }
