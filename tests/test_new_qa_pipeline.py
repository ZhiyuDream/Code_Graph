"""新 QA Pipeline 的集成测试

不依赖外部服务（Neo4j/OpenAI），纯本地验证数据流正确性。
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.qa.models import RetrievedFunction, ExpandLevel
from src.qa.trace import TraceRecorder
from src.qa.expansion import CodeExpander
from src.qa.prompts import PromptBuilder
from src.qa.retrievers.base import BaseRetriever, RetrievalResult


class MockRetriever(BaseRetriever):
    """Mock 检索器，用于测试"""

    def __init__(self, name: str, results: list[RetrievalResult]):
        super().__init__(name, enabled=True)
        self._results = results

    def retrieve(self, question: str, top_k: int = 5) -> list[RetrievalResult]:
        return self._results[:top_k]


def test_models():
    f = RetrievedFunction(
        name="test_func",
        file_path="src/test.cpp",
        start_line=10,
        end_line=20,
        signature="void test_func(int x)",
        score=0.9,
        source="mock",
    )
    assert f.name == "test_func"
    assert f.expand_level == ExpandLevel.SIGNATURE
    assert not f.is_enriched
    print("✓ models OK")


def test_trace_recorder():
    tr = TraceRecorder()
    tr.start_pipeline()
    tr.start_phase()
    tr.record(phase="initial_search", action="mock", query="test", retrieved=["a", "b"], info_gain=2)
    tr.start_phase()
    tr.record(phase="generate", action="llm", token_usage={"prompt": 100, "completion": 50})

    summary = tr.get_summary()
    assert summary["total_steps"] == 2
    assert summary["phases"]["initial_search"]["count"] == 1
    print("✓ trace_recorder OK")


def test_expander():
    ex = CodeExpander()
    r = RetrievalResult(
        id="func:test",
        type="function",
        content="void test() { return; }",
        metadata={"name": "test", "file_path": "src/test.cpp", "start_line": 1, "end_line": 3},
        score=0.8,
        source="mock",
    )
    f = ex.from_retrieval_result(r)
    assert f.name == "test"
    assert f.file_path == "src/test.cpp"
    assert f.score == 0.8

    # build_signature_context 不应报错
    ctx = ex.build_signature_context([f])
    assert "test" in ctx
    print("✓ expander OK")


def test_prompt_builder():
    f = RetrievedFunction(name="foo", file_path="a.cpp", score=0.8, source="mock")
    prompt = PromptBuilder.react_decide(
        question="How does foo work?",
        functions=[f],
        issues=[],
        chains=[],
        action_descriptions={"expand_callers": "expand callers"},
    )
    assert "foo" in prompt
    assert "expand_callers" in prompt
    print("✓ prompt_builder OK")


def test_pipeline_initial_search():
    """测试 Pipeline 的初始召回逻辑（不依赖外部服务）"""
    from src.qa.pipeline import QAPipeline

    mock_results = [
        RetrievalResult(
            id="func:foo", type="function", content="void foo() {}",
            metadata={"name": "foo", "file_path": "a.cpp", "start_line": 1, "end_line": 2},
            score=0.9, source="mock",
        ),
        RetrievalResult(
            id="func:bar", type="function", content="void bar() {}",
            metadata={"name": "bar", "file_path": "b.cpp", "start_line": 1, "end_line": 2},
            score=0.8, source="mock",
        ),
    ]

    retrievers = [MockRetriever("mock", mock_results)]
    pipeline = QAPipeline(
        retrievers=retrievers,
        enable_react=False,  # 关闭 ReAct，避免调用 LLM
    )

    # 只测试内部 _initial_search
    functions, issues = pipeline._initial_search("test question")
    assert len(functions) == 2
    assert functions[0].name == "foo"  # score 0.9 排第一
    print("✓ pipeline initial_search OK")


def test_runner():
    """测试 Runner 的数据结构"""
    from src.qa.runner import QARunner
    from src.qa.pipeline import QAPipeline

    retrievers = [MockRetriever("mock", [])]
    pipeline = QAPipeline(retrievers=retrievers, enable_react=False)
    runner = QARunner(pipeline, output_dir="/tmp/qa_test")

    # 测试空 benchmark
    results = runner.run_benchmark([], workers=1)
    assert results == []
    print("✓ runner OK")


if __name__ == "__main__":
    test_models()
    test_trace_recorder()
    test_expander()
    test_prompt_builder()
    test_pipeline_initial_search()
    test_runner()
    print("\n✅ All tests passed!")
