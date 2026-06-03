"""ReAct 决策循环 — LLM 驱动的行动规划与执行"""
from __future__ import annotations

import logging
from typing import Any

from .models import RetrievedFunction, ExpandLevel
from .prompts import PromptBuilder
from .tools import (
    expand_callers, expand_callees
)
from .tools.class_reader import expand_class
from .tools.file_reader import read_full_file
from .retrievers.grep import GrepRetriever
from .retrievers.embedding import EmbeddingRetriever
from src.core.llm_client import call_llm_json

logger = logging.getLogger(__name__)

_ACTIONS = {
    "grep_search": "用新的关键词进行grep代码搜索",
    "semantic_search": "用新的查询进行embedding语义搜索",
    "expand_callers": "扩展目标函数的调用者（上游）",
    "expand_callees": "扩展目标函数的被调用者（下游）",
    "read_class": "读取目标函数所在类/文件的完整实现",
    # "read_full_file": "读取目标函数所在文件的完整源代码",  # 禁用：文件太大，挤占预算
    "sufficient": "信息已足够，可以生成答案",
}

_SEARCH_ACTIONS = {"grep_search", "semantic_search"}


class ReActLoop:
    """ReAct 行动循环"""

    def __init__(
        self,
        repo_root: str = "",
        max_steps: int = 5,
        model: str | None = None,
    ):
        self.repo_root = repo_root
        self.max_steps = max_steps
        self.model = model
        self.grep_retriever: GrepRetriever | None = None
        self.embedding_retriever: EmbeddingRetriever | None = None

    def set_retrievers(self, grep: GrepRetriever | None, embedding: EmbeddingRetriever | None):
        """注入检索器实例（用于搜索类action）"""
        self.grep_retriever = grep
        self.embedding_retriever = embedding

    def decide(
        self,
        question: str,
        functions: list[RetrievedFunction],
        issues: list,
        chains: list[dict],
        step: int,
        usage_sink: list | None = None,
    ) -> dict[str, Any]:
        """
        LLM 决策下一步行动。
        返回: {sufficient, action, target, query, thought}
        """
        if step >= self.max_steps:
            return {"sufficient": True, "action": "sufficient", "target": "", "query": "", "thought": "达到最大步数"}

        prompt = PromptBuilder.react_decide(
            question=question,
            functions=functions,
            issues=issues,
            chains=chains,
            action_descriptions=_ACTIONS,
        )

        result = call_llm_json(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=400,
            model=self.model,
            _usage_sink=usage_sink,
        )

        if result is None or not isinstance(result, dict):
            logger.warning("ReAct decision JSON parse failed or returned non-dict, forcing sufficient")
            return {"sufficient": True, "action": "sufficient", "target": "", "query": "", "thought": "JSON解析失败"}

        if result.get("sufficient") or result.get("action") == "sufficient":
            return {"sufficient": True, "action": "sufficient", "target": "", "query": "", "thought": result.get("thought", "")}

        action = result.get("action", "")
        if action not in _ACTIONS:
            action = "expand_callees"

        return {
            "sufficient": False,
            "action": action,
            "target": result.get("target", ""),
            "query": result.get("query", ""),
            "thought": result.get("thought", ""),
        }

    def execute(
        self,
        action: str,
        target: str,
        query: str,
    ) -> tuple[list[RetrievedFunction], int]:
        """
        执行 action，返回 (新发现的函数列表, 数量)。
        """
        new_functions: list[RetrievedFunction] = []

        if action == "grep_search":
            if self.grep_retriever:
                results = self.grep_retriever.retrieve(query or target, top_k=5)
                for r in results:
                    from .expansion import CodeExpander
                    new_functions.append(CodeExpander.from_retrieval_result(r))

        elif action == "semantic_search":
            if self.embedding_retriever:
                results = self.embedding_retriever.retrieve(query or target, top_k=5)
                for r in results:
                    from .expansion import CodeExpander
                    new_functions.append(CodeExpander.from_retrieval_result(r))

        elif action == "expand_callers":
            new_functions = expand_callers(target, limit=5)

        elif action == "expand_callees":
            new_functions = expand_callees(target, limit=5)

        elif action == "read_class":
            cls = expand_class(target)
            if cls and cls.body and not cls.body.startswith("// 文件不存在"):
                new_functions = [cls]

        # read_full_file 已禁用：文件太大，会挤占上下文预算
        # elif action == "read_full_file":
        #     ...

        return new_functions, len(new_functions)

    def get_action_names(self) -> list[str]:
        return list(_ACTIONS.keys())
