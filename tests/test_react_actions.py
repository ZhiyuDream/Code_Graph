"""
ReAct action execute() 集成测试。

验证每个 action 都能正常返回非空结果。
需要：Neo4j 连接、embedding 索引、ripgrep、文件系统。
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import pytest

from src.qa.agent_loop import ReActLoop
from src.qa.retrievers.grep import GrepRetriever
from src.qa.retrievers.embedding import EmbeddingRetriever
from config import REPO_ROOT


@pytest.fixture(scope="module")
def loop() -> ReActLoop:
    """构造带真实检索器的 ReActLoop。"""
    rl = ReActLoop(repo_root=REPO_ROOT, max_steps=3)
    grep = GrepRetriever(REPO_ROOT, enabled=True)
    emb = EmbeddingRetriever(enabled=True)
    rl.set_retrievers(grep, emb)
    return rl


class TestGrepSearch:
    def test_returns_results(self, loop: ReActLoop):
        funcs, count = loop.execute("grep_search", "", "llama_model_default_params")
        assert count > 0, "grep_search 应返回至少 1 个结果"
        assert all(f.file_path for f in funcs), "每个结果应有 file_path"


class TestSemanticSearch:
    def test_returns_results(self, loop: ReActLoop):
        funcs, count = loop.execute("semantic_search", "", "llama model default params")
        assert count > 0, "semantic_search 应返回至少 1 个结果"
        assert all(f.file_path for f in funcs), "每个结果应有 file_path"


class TestExpandCallers:
    def test_returns_results(self, loop: ReActLoop):
        funcs, count = loop.execute("expand_callers", "llama_model_default_params", "")
        assert count >= 0, "expand_callers 不应报错"
        #  callers 可能为空（顶层函数没人调用），但不应报错


class TestExpandCallees:
    def test_returns_results(self, loop: ReActLoop):
        funcs, count = loop.execute("expand_callees", "llama_model_default_params", "")
        assert count >= 0, "expand_callees 不应报错"


class TestReadFullFile:
    def test_by_function_name(self, loop: ReActLoop):
        """传函数名，应自动解析到文件路径并返回完整文件内容。"""
        funcs, count = loop.execute("read_full_file", "llama_model_default_params", "")
        assert count == 1, "read_full_file 应恰好返回 1 个结果"
        f = funcs[0]
        assert f.file_path, "应有 file_path"
        assert len(f.body) > 1000, "文件内容应显著非空"
        assert not f.body.startswith("// 文件不存在"), "不应返回文件不存在"


class TestReadClass:
    def test_by_class_method(self, loop: ReActLoop):
        """传 Class::method 格式，应返回类完整实现。"""
        funcs, count = loop.execute("read_class", "llama_model::llama_model", "")
        assert count == 1, "read_class 应恰好返回 1 个结果"
        f = funcs[0]
        assert f.name == "llama_model", f"类名应为 llama_model,  got {f.name}"
        assert len(f.body) > 500, "类实现应显著非空"


class TestSufficient:
    def test_returns_empty(self, loop: ReActLoop):
        funcs, count = loop.execute("sufficient", "", "")
        assert count == 0, "sufficient 应返回 0 个结果"
        assert funcs == [], "sufficient 应返回空列表"
