"""QA系统统一数据模型"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any
from datetime import datetime


class ExpandLevel(Enum):
    """代码展开级别，用于渐进式加载以节省token"""
    SIGNATURE = auto()      # 仅函数签名 (~50 tokens)
    BODY = auto()           # 完整函数实现
    CLASS = auto()          # 函数所在类的完整实现
    FULL_FILE = auto()      # 整个文件


@dataclass
class RetrievedFunction:
    """召回的函数/代码片段"""
    name: str
    file_path: str
    start_line: int = 0
    end_line: int = 0
    signature: str = ""           # 函数签名（用于第一层上下文）
    body: str = ""                # 完整实现（动态加载）
    score: float = 0.0
    source: str = ""              # "grep" / "embedding" / "graph" / "call_chain" / ...
    metadata: dict[str, Any] = field(default_factory=dict)
    expand_level: ExpandLevel = ExpandLevel.SIGNATURE

    @property
    def is_enriched(self) -> bool:
        """是否已加载完整实现"""
        return self.expand_level.value >= ExpandLevel.BODY.value

    @property
    def display_text(self) -> str:
        """根据展开级别返回应展示的文本"""
        if self.expand_level == ExpandLevel.SIGNATURE:
            return self.signature or self.body[:200]
        return self.body or self.signature

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "file_path": self.file_path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "signature": self.signature,
            "score": round(self.score, 4),
            "source": self.source,
            "expand_level": self.expand_level.name,
            "metadata": self.metadata,
        }


@dataclass
class StepTrace:
    """单步追踪记录"""
    step: int
    phase: str                    # "initial_search" / "react_decide" / "react_search" / "expand" / "generate"
    action: str = ""              # 具体action名
    query: str = ""               # 搜索关键字
    retrieved: list[str] = field(default_factory=list)   # 本步召回的函数名列表
    latency_ms: float = 0.0
    token_usage: dict[str, int] = field(default_factory=dict)
    info_gain: int = 0            # 新增函数数量
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "phase": self.phase,
            "action": self.action,
            "query": self.query,
            "retrieved": self.retrieved,
            "latency_ms": round(self.latency_ms, 2),
            "token_usage": self.token_usage,
            "info_gain": self.info_gain,
            "timestamp": self.timestamp.isoformat(),
        }


@dataclass
class QAResult:
    """QA系统单次运行结果"""
    question: str
    answer: str = ""
    retrieved_functions: list[RetrievedFunction] = field(default_factory=list)
    steps: list[StepTrace] = field(default_factory=list)
    total_latency_ms: float = 0.0
    total_tokens: dict[str, int] = field(default_factory=dict)
    error: str = ""

    @property
    def all_function_names(self) -> list[str]:
        """所有召回的函数名（去重）"""
        seen = set()
        result = []
        for f in self.retrieved_functions:
            if f.name and f.name not in seen:
                seen.add(f.name)
                result.append(f.name)
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "answer": self.answer,
            "retrieved_functions": [f.to_dict() for f in self.retrieved_functions],
            "steps": [s.to_dict() for s in self.steps],
            "total_latency_ms": round(self.total_latency_ms, 2),
            "total_tokens": self.total_tokens,
            "error": self.error,
        }
