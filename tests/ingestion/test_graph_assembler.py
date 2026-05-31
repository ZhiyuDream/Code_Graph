"""Tests for graph_builder.py"""
from __future__ import annotations

import pytest

from src.ingestion.graph_builder import assemble_graph
from src.ingestion.models import (
    ClassSymbol,
    FileResult,
    FunctionSymbol,
    RawCall,
    ResolvedCalls,
    VariableSymbol,
)


class TestAssembleGraph:
    def test_basic_structure(self):
        """验证基本节点和边结构。"""
        file_results = [
            FileResult(
                file_path="src/main.cpp",
                functions=[
                    FunctionSymbol(id="src/main.cpp:main:1", name="main", signature="int main()", file_path="src/main.cpp", start_line=1, end_line=10),
                ],
                classes=[],
                variables=[],
                calls=[],
            ),
        ]
        resolved = ResolvedCalls()
        graph = assemble_graph(file_results, resolved, repo_root="/repo")

        assert len(graph["nodes"]["Repository"]) == 1
        assert len(graph["nodes"]["Directory"]) == 1  # src/
        assert len(graph["nodes"]["File"]) == 1  # src/main.cpp
        assert len(graph["nodes"]["Function"]) == 1

        # CONTAINS 边: repo -> dir, dir -> file, file -> function
        contains = graph["edges"]["CONTAINS"]
        assert any(e[0] == "repo:1" and e[1] == "dir:src" for e in contains)
        assert any(e[0] == "dir:src" and e[1] == "src/main.cpp" for e in contains)
        assert any(e[0] == "src/main.cpp" and e[1] == "src/main.cpp:main:1" for e in contains)

    def test_calls_from_resolved(self):
        """CALLS 边应来自 ResolvedCalls，而非 file_result.calls。"""
        file_results = [
            FileResult(
                file_path="a.cpp",
                functions=[
                    FunctionSymbol(id="a.cpp:main:1", name="main", file_path="a.cpp", start_line=1, end_line=10),
                    FunctionSymbol(id="a.cpp:foo:20", name="foo", file_path="a.cpp", start_line=20, end_line=30),
                ],
            ),
        ]
        resolved = ResolvedCalls(calls=[("a.cpp:main:1", "a.cpp:foo:20")])
        graph = assemble_graph(file_results, resolved)

        calls = graph["edges"]["CALLS"]
        assert len(calls) == 1
        assert calls[0] == ("a.cpp:main:1", "a.cpp:foo:20", {})

    def test_ambiguous_calls(self):
        """AMBIGUOUS 调用应创建 CALLS_AMBIGUOUS 边。"""
        file_results = [
            FileResult(
                file_path="a.cpp",
                functions=[
                    FunctionSymbol(id="a.cpp:main:1", name="main", file_path="a.cpp", start_line=1, end_line=10),
                ],
            ),
        ]
        resolved = ResolvedCalls(
            ambiguous=[("a.cpp:main:1", "foo", ["a.cpp:foo:20", "a.cpp:foo:40"])]
        )
        graph = assemble_graph(file_results, resolved)

        ambig = graph["edges"]["CALLS_AMBIGUOUS"]
        assert len(ambig) == 1
        assert ambig[0][0] == "a.cpp:main:1"
        assert ambig[0][2]["callee_name"] == "foo"
        assert len(ambig[0][2]["candidates"]) == 2

    def test_attribute_and_has_member(self):
        """Class member 变量应创建 Attribute 节点和 HAS_MEMBER 边。"""
        file_results = [
            FileResult(
                file_path="test.h",
                functions=[],
                classes=[
                    ClassSymbol(name="MyClass", file_path="test.h", start_line=1, end_line=10),
                ],
                variables=[
                    VariableSymbol(
                        id="test.h:field:3", name="field", file_path="test.h", start_line=3, kind="member",
                        scope_class_index=0,
                    ),
                ],
            ),
        ]
        resolved = ResolvedCalls()
        graph = assemble_graph(file_results, resolved)

        attrs = graph["nodes"]["Attribute"]
        assert len(attrs) == 1
        assert attrs[0]["name"] == "field"
        assert attrs[0]["member_of_class"] == "test.h:MyClass:1"

        has_member = graph["edges"]["HAS_MEMBER"]
        assert len(has_member) == 1
        assert has_member[0] == ("test.h:MyClass:1", "test.h:field:3", {})

    def test_references_var_aggregation(self):
        """同一 (func_id, var_id) 的多次引用应聚合 lines。"""
        file_results = [
            FileResult(
                file_path="a.cpp",
                functions=[
                    FunctionSymbol(id="a.cpp:main:1", name="main", file_path="a.cpp", start_line=1, end_line=10),
                ],
                variables=[
                    VariableSymbol(id="a.cpp:x:2", name="x", file_path="a.cpp", start_line=2, kind="local"),
                ],
                var_refs=[(0, "a.cpp:x:2", 5), (0, "a.cpp:x:2", 7), (0, "a.cpp:x:2", 5)],
            ),
        ]
        resolved = ResolvedCalls()
        graph = assemble_graph(file_results, resolved)

        refs = graph["edges"]["REFERENCES_VAR"]
        assert len(refs) == 1
        assert refs[0][0] == "a.cpp:main:1"
        assert refs[0][1] == "a.cpp:x:2"
        assert refs[0][2]["lines"] == [5, 7]  # 去重并排序

    def test_var_refs_global(self):
        """跨文件 var_refs_global 应被正确处理。"""
        file_results = [
            FileResult(
                file_path="a.cpp",
                functions=[
                    FunctionSymbol(id="a.cpp:main:1", name="main", file_path="a.cpp", start_line=1, end_line=10),
                ],
                variables=[
                    VariableSymbol(id="global:x:5", name="x", file_path="a.cpp", start_line=5, kind="global"),
                ],
            ),
        ]
        resolved = ResolvedCalls()
        var_refs_global = [("a.cpp:main:1", "global:x:5", 3)]
        graph = assemble_graph(file_results, resolved, var_refs_global=var_refs_global)

        refs = graph["edges"]["REFERENCES_VAR"]
        assert len(refs) == 1
        assert refs[0] == ("a.cpp:main:1", "global:x:5", {"lines": [3]})

    def test_python_has_method(self):
        """Python class method 应创建 HAS_METHOD 边。"""
        file_results = [
            FileResult(
                file_path="app.py",
                functions=[
                    FunctionSymbol(
                        id="app.py:method:5", name="method", signature="", file_path="app.py",
                        start_line=5, end_line=10, parent_class="MyClass",
                    ),
                ],
                classes=[
                    ClassSymbol(name="MyClass", file_path="app.py", start_line=1, end_line=15),
                ],
            ),
        ]
        resolved = ResolvedCalls()
        graph = assemble_graph(file_results, resolved)

        has_method = graph["edges"]["HAS_METHOD"]
        assert len(has_method) == 1
        assert has_method[0] == ("app.py:MyClass:1", "app.py:method:5", {})

    def test_two_stage_var_refs_no_blindspot(self):
        """两阶段变量引用：即使 var_ref 指向的函数在后面的文件中，也能正确归属。"""
        file_results = [
            FileResult(
                file_path="a.cpp",
                functions=[
                    FunctionSymbol(id="a.cpp:foo:1", name="foo", file_path="a.cpp", start_line=1, end_line=5),
                ],
                variables=[
                    VariableSymbol(id="a.cpp:x:2", name="x", file_path="a.cpp", start_line=2, kind="local"),
                ],
                var_refs=[(0, "a.cpp:x:2", 3)],
            ),
            FileResult(
                file_path="b.cpp",
                functions=[
                    FunctionSymbol(id="b.cpp:bar:1", name="bar", file_path="b.cpp", start_line=1, end_line=5),
                ],
            ),
        ]
        # 模拟一个跨文件引用：b.cpp:bar 引用了 a.cpp:x
        var_refs_global = [("b.cpp:bar:1", "a.cpp:x:2", 2)]
        resolved = ResolvedCalls()
        graph = assemble_graph(file_results, resolved, var_refs_global=var_refs_global)

        refs = graph["edges"]["REFERENCES_VAR"]
        assert len(refs) == 2
        # 同文件引用
        assert any(r[0] == "a.cpp:foo:1" and r[1] == "a.cpp:x:2" for r in refs)
        # 跨文件引用
        assert any(r[0] == "b.cpp:bar:1" and r[1] == "a.cpp:x:2" for r in refs)

    def test_empty_results(self):
        graph = assemble_graph([], ResolvedCalls())
        assert graph["nodes"]["Function"] == []
        assert graph["nodes"]["Class"] == []
        assert graph["edges"]["CALLS"] == []
