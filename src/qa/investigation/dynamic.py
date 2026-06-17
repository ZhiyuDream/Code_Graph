"""
Dynamic Investigation — 证据驱动的动态调查。

核心特点：
- 每读一个文件，记录新证据如何改变下一步搜索方向
- 重点不是"文件导航"，而是"Evidence → Decision Impact → Next Action"
- 输出 trajectory，用于 case study 分析
"""
import json
import re
import sys
from pathlib import Path
from typing import Dict

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_ROOT))

from src.qa.investigation.base import BaseInvestigator, InvestigationState, SuspicionState, load_prompt


class DynamicInvestigator(BaseInvestigator):
    """Dynamic Investigator：以信念状态更新为核心的调查。"""

    def __init__(self, max_steps: int = 10, repo_path: str = None):
        super().__init__(max_steps, repo_path)
        self.select_prompt = load_prompt("dynamic_select_next")

    def _init_suspicion(self, question: str, entry_file: str, entry_content: str) -> tuple[SuspicionState, Dict]:
        """基于问题和入口文件初始化调查状态。返回 (SuspicionState, entry_evidence)。"""
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

    def _parse_action(self, action: str) -> tuple[str, str]:
        """解析 action 字符串，如 'read_file:path/to/file.cpp'。"""
        if not action:
            return "", ""
        if ":" in action:
            action_type, target = action.split(":", 1)
            return action_type.strip(), target.strip()
        # Fallback: treat as plain file path
        return "read_file", action.strip()

    def _select_next_action(self, latest_evidence: Dict = None, current_target: str = "") -> Dict:
        """基于当前信念状态选择下一步动作。"""
        if not self.state.frontier_files:
            return {
                "new_evidence": "frontier 为空",
                "decision_impact": "没有更多候选文件可以调查",
                "next_search_target": current_target,
                "next_action": "",
                "raw": {},
            }

        compact_evidence = {}
        if latest_evidence:
            compact_evidence = {
                "file_path": latest_evidence.get("file_path", ""),
                "key_facts": latest_evidence.get("key_facts", []),
                "new_hypothesis": latest_evidence.get("new_hypothesis", ""),
                "suspicious_symbols": latest_evidence.get("suspicious_symbols", []),
            }

        prompt = self.select_prompt.format(
            question=self.state.question,
            next_search_target=current_target or self.state.suspicion.current_question,
            visited_files="\n".join(f"- {f}" for f in self.state.visited_files) or "(无)",
            frontier_files="\n".join(f"- {f}" for f in self.state.frontier_files[:20]),
            latest_evidence=json.dumps(compact_evidence, ensure_ascii=False, indent=2) if compact_evidence else "(无)",
        )
        result = self.llm.call_json(prompt)

        if "error" in result and self.state.frontier_files:
            fallback_file = self.state.frontier_files[0]
            return {
                "new_evidence": "",
                "decision_impact": f"LLM 返回异常，fallback 选择 frontier 第一个文件",
                "next_search_target": current_target,
                "next_action": f"read_file:{fallback_file}",
                "raw": result,
            }

        return {
            "new_evidence": result.get("new_evidence", ""),
            "decision_impact": result.get("decision_impact", ""),
            "next_search_target": result.get("next_search_target", current_target),
            "next_action": result.get("next_action", ""),
            "raw": result,
        }

    def run(self, question: str, entry_file: str) -> Dict:
        """运行 Dynamic Investigation。"""
        self.state = InvestigationState(
            question=question,
            entry_file=entry_file,
            max_steps=self.max_steps,
        )

        # Read entry file and initialize in one LLM call
        entry_content = self.read_file(entry_file)
        self.state.visited_files.append(entry_file)
        self.state.suspicion, entry_evidence = self._init_suspicion(question, entry_file, entry_content)
        self.state.evidence_log.append(entry_evidence)

        # Initial frontier expansion: comprehensive search for top symbols
        self.bootstrap_frontier(self.state.suspicion.suspicious_symbols, top_k_symbols=2)

        trajectory = []
        latest_evidence = entry_evidence
        current_target = self.state.suspicion.current_question

        for step_num in range(1, self.max_steps + 1):
            decision = self._select_next_action(latest_evidence, current_target)
            current_target = decision.get("next_search_target") or current_target

            action_type, action_target = self._parse_action(decision.get("next_action", ""))

            trajectory.append({
                "step": step_num,
                "new_evidence": decision.get("new_evidence", ""),
                "decision_impact": decision.get("decision_impact", ""),
                "next_search_target": current_target,
                "next_action": decision.get("next_action", ""),
                "frontier_before": list(self.state.frontier_files),
            })

            if not action_target:
                break

            if action_type == "read_file":
                next_file = self._normalize_file_path(action_target)
                if next_file in self.state.visited_files:
                    if next_file in self.state.frontier_files:
                        self.state.frontier_files.remove(next_file)
                    continue
                if next_file not in self.state.frontier_files:
                    self.state.frontier_files.append(next_file)
                self.state.frontier_files.remove(next_file)
                self.state.visited_files.append(next_file)

                content = self.read_file(next_file)
                latest_evidence = self.extract_evidence(next_file, content)
                self.state.evidence_log.append(latest_evidence)

                # Expand frontier based on new evidence
                self.update_frontier(latest_evidence)
            else:
                # For now, only read_file is fully supported
                # Fallback: pick first frontier file
                if self.state.frontier_files:
                    next_file = self.state.frontier_files.pop(0)
                    self.state.visited_files.append(next_file)
                    content = self.read_file(next_file)
                    latest_evidence = self.extract_evidence(next_file, content)
                    self.state.evidence_log.append(latest_evidence)
                    self.update_frontier(latest_evidence)

        answer = self.generate_answer()

        return {
            "mode": "dynamic",
            "question": question,
            "entry_file": entry_file,
            "visited_files": self.state.visited_files,
            "frontier_files": self.state.frontier_files,
            "evidence_log": self.state.evidence_log,
            "trajectory": trajectory,
            "answer": answer,
        }
