"""检索器基类 — 所有检索器必须实现 retrieve(question, top_k)"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class RetrievalResult:
    """统一检索结果格式"""

    def __init__(
        self,
        id: str,
        type: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        score: float = 0.0,
        source: str = "",
    ):
        self.id = id
        self.type = type          # "function", "file", "issue", "class", "chunk"
        self.content = content
        self.metadata = metadata or {}
        self.score = score
        self.source = source      # 哪个检索器产生的

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "content": self.content,
            "metadata": self.metadata,
            "score": self.score,
            "source": self.source,
        }

    def __repr__(self) -> str:
        return f"RetrievalResult({self.id}, {self.type}, score={self.score:.3f}, source={self.source})"


class BaseRetriever(ABC):
    """检索器基类"""

    def __init__(self, name: str, enabled: bool = True):
        self.name = name
        self.enabled = enabled

    @abstractmethod
    def retrieve(self, question: str, top_k: int = 5) -> list[RetrievalResult]:
        """
        根据问题检索相关内容。

        Args:
            question: 用户问题
            top_k: 返回结果数量上限

        Returns:
            RetrievalResult 列表，按 relevance 降序
        """
        ...

    def is_available(self) -> bool:
        """检查检索器是否可用（依赖是否就绪）"""
        return self.enabled
