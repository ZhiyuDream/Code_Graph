"""Prompt 构建器。

所有 prompt 模板已迁移到 prompts/ 目录下：
- react_decide.txt        → ReAct 决策
- answer_generation.txt   → 答案生成
- expansion_decide.txt    → 展开决策（让LLM判断哪些函数值得展开）

如需修改分支逻辑或 prompt 措辞，直接编辑对应 txt 文件即可。
"""
from __future__ import annotations

from typing import Any

from src.core.prompt_loader import load_prompt


class PromptBuilder:
    """Prompt 构建器——负责从 prompts/ 目录加载模板并填入动态数据。"""

    @staticmethod
    def react_decide(
        question: str,
        functions: list[Any],
        issues: list[Any],
        chains: list[Any],
        action_descriptions: dict[str, str],
    ) -> str:
        func_lines = []
        for i, f in enumerate(functions[:10]):
            marker = " [已扩展]" if f.metadata.get("expanded") else ""
            sig = f.signature or ""
            sig_display = f" 签名: {sig[:180]}" if sig else ""
            func_lines.append(
                f"{i+1}. {f.name} ({f.file_path}, score={f.score:.3f}, source={f.source}){marker}{sig_display}"
            )
        if len(functions) > 10:
            func_lines.append(f"   ... 还有 {len(functions)-10} 个函数")

        issue_lines = [f"{i+1}. {issue.id}" for i, issue in enumerate(issues[:3])]
        if not issues:
            issue_lines = ["无"]

        chain_lines = []
        for c in chains[-5:]:
            chain_lines.append(f"  - {c.get('from', '')}: {c.get('direction', '')} (新增{c.get('new', 0)}个)")
        if not chains:
            chain_lines = ["无"]

        # 提取已用过的搜索关键词（用于避免重复）
        used_queries = [c.get("from", "") for c in chains if c.get("direction") in ("grep_search", "semantic_search")]
        used_queries = [q for q in used_queries if q]
        used_queries_text = ", ".join(used_queries) if used_queries else "无"

        actions_text = "\n".join(f"- {name}: {desc}" for name, desc in action_descriptions.items())
        action_choices = "|".join(action_descriptions.keys())

        return load_prompt(
            "react_decide",
            question=question,
            function_count=len(functions),
            function_list="\n".join(func_lines) or "无",
            issue_count=len(issues),
            issue_list="\n".join(issue_lines),
            chain_count=len(chains),
            chain_list="\n".join(chain_lines),
            used_queries=used_queries_text,
            actions=actions_text,
            action_choices=action_choices,
        )

    @staticmethod
    def answer_generation(question: str, context: str) -> str:
        return load_prompt(
            "answer_generation",
            context=context,
            question=question,
        )

    @staticmethod
    def expansion_decide(question: str, functions: list[Any]) -> str:
        func_lines = []
        for i, f in enumerate(functions[:15]):
            sig = f.signature or f.name
            func_lines.append(f"{i+1}. {f.name} @ {f.file_path}\n   签名: {sig[:150]}")
        return load_prompt(
            "expansion_decide",
            question=question,
            function_list="\n".join(func_lines),
        )
