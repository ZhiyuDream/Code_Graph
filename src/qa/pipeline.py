"""QA Pipeline — 统一编排初始召回→ReAct循环→渐进展开→答案生成"""
from __future__ import annotations

import logging
from typing import Any

from .models import QAResult, RetrievedFunction, StepTrace
from .trace import TraceRecorder
from .expansion import CodeExpander
from .agent_loop import ReActLoop
from .prompts import PromptBuilder
from .retrievers.base import BaseRetriever
from src.core.llm_client import call_llm, get_usage_stats, reset_usage_stats
from src.core.model_config import ModelRegistry

logger = logging.getLogger(__name__)


class QAPipeline:
    """
    QA系统主Pipeline。

    流程:
    1. initial_search  — 并行调用所有 enabled retrievers
    2. react_loop      — LLM决策→执行action→循环（可选）
    3. expansion       — 渐进式代码展开（签名→实现→类→文件）
    4. generate        — 构建上下文，调用LLM生成答案
    """

    def __init__(
        self,
        retrievers: list[BaseRetriever],
        expander: CodeExpander | None = None,
        max_react_steps: int = 5,
        enable_react: bool = True,
        model: str | None = None,
        repo_root: str = "",
    ):
        self.retrievers = retrievers
        self.expander = expander or CodeExpander()
        self.max_react_steps = max_react_steps
        self.enable_react = enable_react
        self.model = model
        self.repo_root = repo_root
        self.react_loop = ReActLoop(
            repo_root=repo_root,
            max_steps=max_react_steps,
            model=model,
        )
        # 向 ReActLoop 注入检索器
        grep = next((r for r in retrievers if r.name == "grep"), None)
        emb = next((r for r in retrievers if r.name == "embedding"), None)
        self.react_loop.set_retrievers(grep, emb)

    def run(self, question: str) -> QAResult:
        """运行完整pipeline，返回QAResult"""
        result = QAResult(question=question)
        tracer = TraceRecorder()
        tracer.start_pipeline()
        usage_sink: list = []  # 隔离的 token 记录器（避免并行竞争）
        reset_usage_stats()

        try:
            # -------- 1. Initial Search --------
            tracer.start_phase()
            functions, issues = self._initial_search(question)
            tracer.record(
                phase="initial_search",
                action="retrieve",
                query=question,
                retrieved=[f.name for f in functions],
                info_gain=len(functions),
            )

            # -------- 2. ReAct Loop (optional) --------
            if self.enable_react and functions:
                functions, issues = self._react_loop(
                    question, functions, issues, tracer, usage_sink
                )

            # -------- 3. Expansion --------
            tracer.start_phase()
            functions = self._expansion(question, functions, usage_sink)
            tracer.record(
                phase="expansion",
                retrieved=[f.name for f in functions if f.is_enriched],
                info_gain=sum(1 for f in functions if f.is_enriched),
            )

            # -------- 4. Generate Answer --------
            tracer.start_phase()
            answer = self._generate(question, functions, issues, usage_sink)
            tracer.record(
                phase="generate",
                action="call_llm",
                token_usage=self._sum_usage(usage_sink),
            )

            result.answer = answer
            result.retrieved_functions = functions

        except Exception as e:
            logger.exception("Pipeline error: %s", e)
            tracer.record_error(phase="pipeline", error=str(e))
            result.error = str(e)

        tracer.finalize(result)
        return result

    def _initial_search(self, question: str) -> tuple[list[RetrievedFunction], list]:
        """初始召回：调用所有 enabled retrievers，合并去重"""
        all_results = []
        for retriever in self.retrievers:
            if not retriever.is_available():
                continue
            try:
                results = retriever.retrieve(question, top_k=10)
                logger.debug("%s retrieved %d results", retriever.name, len(results))
                all_results.extend(results)
            except Exception as e:
                logger.warning("Retriever %s failed: %s", retriever.name, e)

        # 去重：按 name + file_path
        seen = set()
        unique = []
        for r in all_results:
            key = f"{r.id}:{r.metadata.get('file_path', '')}"
            if key not in seen:
                seen.add(key)
                unique.append(r)

        # 排序：按 score 降序
        unique.sort(key=lambda x: x.score, reverse=True)

        # 分离 Issue 和 Function
        functions = []
        issues = []
        for r in unique:
            if r.type == "issue":
                issues.append(r)
            else:
                functions.append(self.expander.from_retrieval_result(r))

        return functions, issues

    def _react_loop(
        self,
        question: str,
        functions: list[RetrievedFunction],
        issues: list,
        tracer: TraceRecorder,
        usage_sink: list,
    ) -> tuple[list[RetrievedFunction], list]:
        """ReAct 多轮决策与执行"""
        chains: list[dict] = []
        info_gain_history: list[int] = []
        expanded_targets: set[str] = set()  # 记录已扩展的目标，防止重复

        for step in range(1, self.max_react_steps + 1):
            # 标记已扩展的函数（用于 prompt 显示）
            for c in chains:
                t = c.get("from", "")
                for f in functions:
                    if f.name == t:
                        f.metadata["expanded"] = True

            decision = self.react_loop.decide(
                question, functions, issues, chains, step, usage_sink
            )

            if decision.get("sufficient"):
                break

            action = decision.get("action", "")
            target = decision.get("target", "")
            query = decision.get("query", "") or target

            # 防止重复扩展同一目标（所有非搜索类 action，包括 read_full_file/read_class）
            if action not in ("grep_search", "semantic_search"):
                dedup_key = f"{action}:{target}"
                if dedup_key in expanded_targets:
                    logger.debug("Skip duplicate expansion: %s", dedup_key)
                    continue
                expanded_targets.add(dedup_key)

            tracer.start_phase()
            new_funcs, found = self.react_loop.execute(action, target, query)

            # 去重合并
            existing_names = {f.name for f in functions}
            added = 0
            for f in new_funcs:
                if f.name not in existing_names:
                    functions.append(f)
                    existing_names.add(f.name)
                    added += 1

            chains.append({
                "from": target or query,
                "direction": action,
                "found": found,
                "new": added,
            })
            info_gain_history.append(added)

            tracer.record(
                phase="react_search",
                action=action,
                query=query or target,
                retrieved=[f.name for f in new_funcs],
                info_gain=added,
            )

            # 早期停止：连续2步新增<2
            if step >= 3 and len(info_gain_history) >= 2:
                if all(g <= 1 for g in info_gain_history[-2:]):
                    logger.debug("Early stop: info gain too low")
                    break

        return functions, issues

    def _expansion(self, question: str, functions: list[RetrievedFunction], usage_sink: list | None = None) -> list[RetrievedFunction]:
        """渐进式代码展开：先让LLM判断哪些值得展开，再执行展开"""
        if not functions:
            return functions

        # 第一步：尝试让 LLM 判断哪些函数值得展开
        # 只有当有足够多函数带签名时才启用（否则LLM判断质量差）
        sig_count = sum(1 for f in functions if f.signature)
        use_llm_decide = sig_count >= 3 and len(functions) > 5

        targets: set[str] = set()
        if use_llm_decide:
            try:
                from src.core.llm_client import call_llm_json
                decide_prompt = PromptBuilder.expansion_decide(question, functions)
                result = call_llm_json(
                    messages=[{"role": "user", "content": decide_prompt}],
                    max_tokens=500,
                    model=self.model,
                    _usage_sink=usage_sink,
                )
                if result:
                    targets = set(result.get("relevant_functions", []))
            except Exception:
                targets = set()

        # 第二步：执行展开
        # 如果LLM没选出任何目标，或解析失败，fallback 到无条件展开 top 10
        if targets:
            expanded = 0
            for f in functions:
                if f.name in targets:
                    self.expander.expand(f)
                    expanded += 1
                    if expanded >= 10:
                        break
        else:
            top_k = min(10, len(functions))
            for f in functions[:top_k]:
                self.expander.expand(f)

        return functions

    def _generate(
        self,
        question: str,
        functions: list[RetrievedFunction],
        issues: list,
        usage_sink: list,
    ) -> str:
        """生成答案。 reasoning 模型给更多 token 预算，让它充分思考。"""
        # 从问题中提取核心函数名（反引号包裹的），确保这些函数在 context 中最先展示
        import re
        priority_names = re.findall(r'`([^`]+?)`', question)

        context = self.expander.build_full_context(
            functions,
            issues=issues,
            budget_chars=100000,
            priority_names=priority_names,
        )
        prompt = PromptBuilder.answer_generation(question, context)
        # reasoning 模型（如 DeepSeek）需要更多 token 预算
        cfg = ModelRegistry.resolve(self.model)
        max_tokens = 8000 if cfg.reasoning_support else 2000
        return call_llm(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            model=self.model,
            _usage_sink=usage_sink,
        )

    @staticmethod
    def _sum_usage(usage_sink: list) -> dict[str, int]:
        """汇总 usage_sink 中的 token 消耗"""
        total = {"prompt": 0, "completion": 0, "total": 0, "reasoning": 0}
        for usage in usage_sink:
            total["prompt"] += getattr(usage, "prompt_tokens", 0)
            total["completion"] += getattr(usage, "completion_tokens", 0)
            total["total"] += getattr(usage, "total_tokens", 0)
            total["reasoning"] += getattr(
                getattr(usage, "completion_tokens_details", None),
                "reasoning_tokens", 0
            ) or 0
        return total
