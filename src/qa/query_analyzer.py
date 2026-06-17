"""Query Analyzer — LLM-based query understanding and signal extraction"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from src.core.llm_client import call_llm_json
from src.core.prompt_loader import load_prompt

logger = logging.getLogger(__name__)


@dataclass
class QueryAnalysis:
    """查询分析结果"""
    query_type: str           # symbol_centric | component_centric | architecture_centric | unknown
    symbols: list[str]        # 提取的函数名/类名/文件名
    components: list[str]     # 提取的组件名/模块名
    confidence: float         # 0-1
    reasoning: str            # 判断理由


class QueryAnalyzer:
    """
    查询分析器。

    用 LLM 分析用户查询，判断查询类型并提取关键信号（函数名、组件名等）。
    LLM 失败时 fallback 到轻量级规则。
    """

    def __init__(self, model: str | None = None):
        self.model = model

    def analyze(self, question: str) -> QueryAnalysis:
        """
        分析查询。

        Args:
            question: 用户问题

        Returns:
            QueryAnalysis
        """
        # 尝试 LLM 分析
        try:
            analysis = self._analyze_with_llm(question)
            logger.info(
                "[QueryAnalyzer] LLM: type=%s, symbols=%s, confidence=%.2f",
                analysis.query_type, analysis.symbols, analysis.confidence,
            )
            return analysis
        except Exception as e:
            logger.warning("[QueryAnalyzer] LLM failed: %s, fallback to regex", e)

        # Fallback: 正则提取
        return self._analyze_with_regex(question)

    def _analyze_with_llm(self, question: str) -> QueryAnalysis:
        """用 LLM 分析查询。"""
        prompt = load_prompt("query_analysis", question=question)

        result = call_llm_json(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500,
            model=self.model,
        )

        if not result or not isinstance(result, dict):
            raise ValueError("LLM returned invalid result")

        query_type = result.get("query_type", "unknown")
        symbols = result.get("symbols") or []
        components = result.get("components") or []
        confidence = result.get("confidence", 0.0)
        reasoning = result.get("reasoning", "")

        # 数据清洗
        symbols = [s.strip() for s in symbols if s and isinstance(s, str)]
        components = [c.strip() for c in components if c and isinstance(c, str)]
        confidence = max(0.0, min(1.0, float(confidence)))

        return QueryAnalysis(
            query_type=query_type,
            symbols=symbols,
            components=components,
            confidence=confidence,
            reasoning=reasoning,
        )

    def _analyze_with_regex(self, question: str) -> QueryAnalysis:
        """LLM 失败时的 fallback：正则提取反引号函数名。"""
        symbols = re.findall(r'`([^`]+?)`', question)

        # 也尝试提取下划线风格的函数名（如 llama_decode）
        bare_symbols = re.findall(r'\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b', question)
        for sym in bare_symbols:
            if sym not in symbols and len(sym) > 5:  # 过滤短词
                symbols.append(sym)

        # 去重
        symbols = list(dict.fromkeys(symbols))

        return QueryAnalysis(
            query_type="symbol_centric" if symbols else "unknown",
            symbols=symbols,
            components=[],
            confidence=0.5 if symbols else 0.0,
            reasoning="LLM analysis failed, fallback to regex extraction",
        )
