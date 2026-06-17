"""
Symbol-Follow Investigation — 严格的符号线索驱动调查。

核心特点：
- 每步从证据中提取最关键的 1-2 个符号
- 用 grep 找到这些符号的所有出现位置
- 优先选择 definition / call site 文件读取
- 不依赖 LLM 猜测文件位置
"""
import json
import re
import sys
from pathlib import Path
from typing import Dict

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_ROOT))

from src.qa.investigation.base import BaseInvestigator, InvestigationState, SuspicionState, load_prompt


class SymbolFollowInvestigator(BaseInvestigator):
    """Symbol-Follow Investigator：只追具体符号线索。"""

    def __init__(self, max_steps: int = 10, repo_path: str = None):
        super().__init__(max_steps, repo_path)
        self.select_prompt = load_prompt("symbol_follow_select_next")

    def _init_suspicion(self, question: str, entry_file: str, entry_content: str) -> tuple[SuspicionState, Dict]:
        """初始化和 Dynamic 相同。"""
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

    def _select_symbol(self, latest_evidence: Dict, current_symbols: list[str]) -> Dict:
        """让 LLM 从当前符号中选择 1-2 个最值得追的符号。"""
        prompt = self.select_prompt.format(
            question=self.state.question,
            current_symbols=", ".join(current_symbols),
            latest_evidence=json.dumps({
                "file_path": latest_evidence.get("file_path", ""),
                "key_facts": latest_evidence.get("key_facts", []),
            }, ensure_ascii=False),
        )
        result = self.llm.call_json(prompt)
        return {
            "symbols": result.get("symbols", current_symbols[:2]),
            "decision_impact": result.get("decision_impact", ""),
            "raw": result,
        }

    def _rank_symbol_files(self, symbol: str, files: list[str]) -> list[str]:
        """对符号相关的文件排序：优先 definition，然后 call site。"""
        ranked = []
        # Simple heuristic: files with the symbol in their name are likely definitions
        for f in files:
            base = Path(f).stem.lower()
            sym = symbol.lower().replace("ggml_", "").replace("common_", "").replace("llama_", "")
            if sym in base or base in sym:
                ranked.append(f)
        # Then add remaining files
        for f in files:
            if f not in ranked:
                ranked.append(f)
        return ranked

    def run(self, question: str, entry_file: str) -> Dict:
        """运行 Symbol-Follow Investigation。"""
        self.state = InvestigationState(
            question=question,
            entry_file=entry_file,
            max_steps=self.max_steps,
        )

        entry_content = self.read_file(entry_file)
        self.state.visited_files.append(entry_file)
        self.state.suspicion, entry_evidence = self._init_suspicion(question, entry_file, entry_content)
        self.state.evidence_log.append(entry_evidence)

        trajectory = []
        latest_evidence = entry_evidence
        current_symbols = self.state.suspicion.suspicious_symbols

        for step_num in range(1, self.max_steps + 1):
            # 1. Select symbols to follow
            decision = self._select_symbol(latest_evidence, current_symbols)
            symbols_to_follow = decision.get("symbols", current_symbols[:2])
            if not symbols_to_follow:
                break

            # 2. Grep each symbol and collect files
            all_files = set()
            for symbol in symbols_to_follow:
                files = self.expand_frontier_from_symbols([symbol], limit=20)
                all_files.update(files)

            # 3. Rank and pick next file
            candidate_files = [f for f in all_files if f not in self.state.visited_files]
            if not candidate_files:
                break

            ranked = []
            for symbol in symbols_to_follow:
                ranked.extend(self._rank_symbol_files(symbol, candidate_files))
            # Deduplicate while preserving order
            seen = set()
            next_file = None
            for f in ranked:
                if f not in seen and f not in self.state.visited_files:
                    seen.add(f)
                    next_file = f
                    break
            if not next_file:
                next_file = candidate_files[0]

            trajectory.append({
                "step": step_num,
                "decision_impact": decision.get("decision_impact", ""),
                "symbols_followed": symbols_to_follow,
                "candidate_files": candidate_files[:10],
                "next_action": f"read_file:{next_file}",
            })

            # 4. Read file and extract evidence
            next_file = self._normalize_file_path(next_file)
            self.state.visited_files.append(next_file)
            content = self.read_file(next_file)
            latest_evidence = self.extract_evidence(next_file, content)
            self.state.evidence_log.append(latest_evidence)

            # 5. Update symbols from new evidence
            new_symbols = latest_evidence.get("suspicious_symbols", [])
            if new_symbols:
                # Combine new symbols, prioritize them
                current_symbols = new_symbols + [s for s in current_symbols if s not in new_symbols]
            else:
                # Remove current followed symbols to avoid loops
                current_symbols = [s for s in current_symbols if s not in symbols_to_follow]

        answer = self.generate_answer()

        return {
            "mode": "symbol_follow",
            "question": question,
            "entry_file": entry_file,
            "visited_files": self.state.visited_files,
            "frontier_files": [],
            "evidence_log": self.state.evidence_log,
            "trajectory": trajectory,
            "answer": answer,
        }
