"""QA系统 — 组件化检索与问答Pipeline"""
from .models import (
    RetrievedFunction,
    StepTrace,
    QAResult,
    ExpandLevel,
)
from .trace import TraceRecorder
from .expansion import CodeExpander
from .agent_loop import ReActLoop
from .pipeline import QAPipeline
from .runner import QARunner
from .retrievers import (
    BaseRetriever,
    RetrievalResult,
    EmbeddingRetriever,
    GrepRetriever,
)

__all__ = [
    # 数据模型
    "RetrievedFunction",
    "StepTrace",
    "QAResult",
    "ExpandLevel",
    # 追踪与展开
    "TraceRecorder",
    "CodeExpander",
    # 核心编排
    "ReActLoop",
    "QAPipeline",
    "QARunner",
    # 检索器
    "BaseRetriever",
    "RetrievalResult",
    "EmbeddingRetriever",
    "GrepRetriever",
]
