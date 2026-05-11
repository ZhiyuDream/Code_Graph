"""Tests for symbol_extractor.py"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.pipeline.symbol_extractor import (
    _determine_variable_kind,
    _find_function_containing_line,
    _resolve_flat_fields,
    extract_calls_for_function,
    extract_symbols_from_document,
    process_file,
)
from src.pipeline.models import FunctionSymbol, ClassSymbol, VariableSymbol, RawCall


class TestExtractSymbolsFromDocument:
    """测试从 documentSymbol 提取函数、类、变量。"""

    def test_hierarchical_tree_basic(self):
        """层级 tree：class 内部有 method 和 field。"""
        symbols = [
            {
                "name": "MyClass",
                "kind": 5,  # Class
                "range": {"start": {"line": 9, "character": 0}, "end": {"line": 29, "character": 1}},
                "children": [
                    {
                        "name": "myMethod",
                        "kind": 6,  # Method
                        "range": {"start": {"line": 10, "character": 4}, "end": {"line": 14, "character": 5}},
                        "detail": "void myMethod(int)",
                    },
                    {
                        "name": "myField",
                        "kind": 7,  # Field
                        "range": {"start": {"line": 16, "character": 4}, "end": {"line": 16, "character": 20}},
                    },
                ],
            },
            {
                "name": "globalFunc",
                "kind": 12,  # Function
                "range": {"start": {"line": 30, "character": 0}, "end": {"line": 34, "character": 1}},
            },
        ]
        funcs, classes, variables = extract_symbols_from_document(symbols, "test.cpp")

        assert len(funcs) == 2
        assert funcs[0].name == "myMethod"
        assert funcs[0].file_path == "test.cpp"
        assert funcs[0].start_line == 11  # LSP line 10 -> 1-based 11
        assert funcs[0].signature == "void myMethod(int)"

        assert len(classes) == 1
        assert classes[0].name == "MyClass"

        assert len(variables) == 1
        assert variables[0].name == "myField"
        assert variables[0].kind == "member"
        assert variables[0].scope_class_index == 0

    def test_flat_list_with_field_heuristic(self):
        """扁平 list：Field 与 Class 同级，children 为空。"""
        symbols = [
            {
                "name": "MyStruct",
                "kind": 23,  # Struct
                "range": {"start": {"line": 9, "character": 0}, "end": {"line": 14, "character": 1}},
                "children": [],
            },
            {
                "name": "fieldA",
                "kind": 7,  # Field
                "range": {"start": {"line": 10, "character": 4}, "end": {"line": 10, "character": 20}},
                "children": [],
            },
            {
                "name": "fieldB",
                "kind": 8,  # 某些 clangd 版本用 8 表示 Field
                "range": {"start": {"line": 11, "character": 4}, "end": {"line": 11, "character": 20}},
                "children": [],
            },
        ]
        funcs, classes, variables = extract_symbols_from_document(symbols, "test.h")

        assert len(classes) == 1
        assert len(variables) == 2
        for v in variables:
            assert v.kind == "member", f"Expected kind='member', got '{v.kind}'"
            assert v.scope_class_index == 0, f"Expected scope_class_index=0, got {v.scope_class_index}"

    def test_kind_8_should_not_be_param(self):
        """kind=8 在函数作用域内是 param，在 class 作用域内是 member。"""
        symbols = [
            {
                "name": "foo",
                "kind": 12,  # Function
                "range": {"start": {"line": 0, "character": 0}, "end": {"line": 4, "character": 1}},
                "children": [
                    {
                        "name": "x",
                        "kind": 8,  # Parameter (在 function children 中)
                        "range": {"start": {"line": 0, "character": 10}, "end": {"line": 0, "character": 15}},
                    },
                ],
            },
            {
                "name": "MyClass",
                "kind": 5,
                "range": {"start": {"line": 6, "character": 0}, "end": {"line": 11, "character": 1}},
                "children": [
                    {
                        "name": "y",
                        "kind": 8,  # Field (在 class children 中)
                        "range": {"start": {"line": 7, "character": 4}, "end": {"line": 7, "character": 15}},
                    },
                ],
            },
        ]
        funcs, classes, variables = extract_symbols_from_document(symbols, "test.cpp")

        param_vars = [v for v in variables if v.scope_function_index == 0]
        member_vars = [v for v in variables if v.scope_class_index == 0]

        assert len(param_vars) == 1
        assert param_vars[0].name == "x"
        assert param_vars[0].kind == "param"

        assert len(member_vars) == 1
        assert member_vars[0].name == "y"
        assert member_vars[0].kind == "member"

    def test_empty_symbols(self):
        funcs, classes, variables = extract_symbols_from_document([], "empty.cpp")
        assert funcs == []
        assert classes == []
        assert variables == []


class TestDetermineVariableKind:
    def test_param_in_function_scope(self):
        assert _determine_variable_kind(8, scope_func=0, scope_class=None) == "param"

    def test_field_in_class_scope(self):
        # kind=7 (Field) 在 class 内部
        assert _determine_variable_kind(7, scope_func=None, scope_class=0) == "member"
        # kind=8 在 class 内部也应为 member（修复原 bug）
        assert _determine_variable_kind(8, scope_func=None, scope_class=0) == "member"

    def test_local_in_function_scope(self):
        assert _determine_variable_kind(13, scope_func=0, scope_class=None) == "local"

    def test_global_no_scope(self):
        assert _determine_variable_kind(13, scope_func=None, scope_class=None) == "global"


class TestResolveFlatFields:
    def test_flat_field_assigned_to_nearest_class(self):
        classes = [
            ClassSymbol(name="ClassA", file_path="test.h", start_line=10, end_line=15),
            ClassSymbol(name="ClassB", file_path="test.h", start_line=20, end_line=25),
        ]
        variables = [
            VariableSymbol(id="v1", name="fieldA", file_path="test.h", start_line=12, kind="member"),
            VariableSymbol(id="v2", name="fieldB", file_path="test.h", start_line=22, kind="member"),
            VariableSymbol(id="v3", name="globalVar", file_path="test.h", start_line=30, kind="global"),
        ]
        resolved = _resolve_flat_fields([], classes, variables)
        assert resolved[0].scope_class_index == 0  # fieldA -> ClassA
        assert resolved[1].scope_class_index == 1  # fieldB -> ClassB
        assert resolved[2].scope_class_index is None  # globalVar 不变

    def test_no_classes_no_change(self):
        variables = [
            VariableSymbol(id="v1", name="x", file_path="test.cpp", start_line=5, kind="global"),
        ]
        resolved = _resolve_flat_fields([], [], variables)
        assert resolved[0].scope_class_index is None


class TestExtractCallsForFunction:
    def test_callee_line_preserved(self):
        """验证 RawCall 中 callee_line 被正确保留。"""
        def mock_request(method, params):
            if method == "textDocument/prepareCallHierarchy":
                return [{"name": "foo", "uri": "file:///repo/test.cpp", "range": {"start": {"line": 9, "character": 0}}}]
            if method == "callHierarchy/outgoingCalls":
                return [
                    {
                        "to": {
                            "name": "bar",
                            "uri": "file:///repo/other.cpp",
                            "range": {"start": {"line": 19, "character": 0}},
                        }
                    }
                ]
            return None

        func = FunctionSymbol(id="test.cpp:foo:10", name="foo", signature="", file_path="test.cpp", start_line=10, end_line=15)
        calls = extract_calls_for_function(mock_request, "file:///repo/test.cpp", 0, func, "test.cpp", repo_root=Path("/repo"), sleep_after=0)

        assert len(calls) == 1
        assert calls[0].callee_name == "bar"
        assert calls[0].callee_file_path == "other.cpp"  # 已归一化为相对路径
        assert calls[0].callee_line == 20  # 0-based -> 1-based

    def test_no_outgoing_calls(self):
        def mock_request(method, params):
            if method == "textDocument/prepareCallHierarchy":
                return []
            return None

        func = FunctionSymbol(id="test.cpp:foo:10", name="foo", signature="", file_path="test.cpp", start_line=10, end_line=15)
        calls = extract_calls_for_function(mock_request, "file:///repo/test.cpp", 0, func, "test.cpp", repo_root=Path("/repo"), sleep_after=0)
        assert calls == []

    def test_lsp_error_propagates(self):
        """LSP 异常应向上传播，不再静默。"""
        def mock_request(method, params):
            raise RuntimeError("clangd crashed")

        func = FunctionSymbol(name="foo", signature="", file_path="test.cpp", start_line=10, end_line=15)
        with pytest.raises(RuntimeError, match="clangd crashed"):
            extract_calls_for_function(mock_request, "file:///repo/test.cpp", 0, func, "test.cpp", sleep_after=0)


class TestProcessFile:
    def test_full_file_processing(self):
        """端到端测试：一个文件包含 function + class + call + variable reference。"""
        def mock_request(method, params):
            if method == "textDocument/documentSymbol":
                return [
                    {
                        "name": "main",
                        "kind": 12,
                        "range": {"start": {"line": 0, "character": 0}, "end": {"line": 4, "character": 1}},
                        "children": [
                            {
                                "name": "x",
                                "kind": 13,
                                "range": {"start": {"line": 1, "character": 8}, "end": {"line": 1, "character": 12}},
                            },
                        ],
                    },
                ]
            if method == "textDocument/prepareCallHierarchy":
                return [{"name": "main", "uri": "file:///repo/main.cpp", "range": {"start": {"line": 0, "character": 0}}}]
            if method == "callHierarchy/outgoingCalls":
                return [
                    {
                        "to": {
                            "name": "printf",
                            "uri": "file:///usr/include/stdio.h",
                            "range": {"start": {"line": 99, "character": 0}},
                        }
                    }
                ]
            if method == "textDocument/references":
                return [
                    {"uri": "file:///repo/main.cpp", "range": {"start": {"line": 2, "character": 4}}},
                ]
            return None

        result = process_file(
            mock_request,
            abs_path="/repo/main.cpp",
            file_path="main.cpp",
            repo_root=Path("/repo"),
            collect_calls=True,
            collect_var_refs=True,
            delay_between_calls=0,
        )

        assert result.file_path == "main.cpp"
        assert len(result.functions) == 1
        assert result.functions[0].name == "main"
        assert len(result.variables) == 1
        assert result.variables[0].name == "x"
        assert result.variables[0].kind == "local"
        assert len(result.calls) == 1
        assert result.calls[0].callee_name == "printf"
        assert result.calls[0].callee_file_path == "/usr/include/stdio.h"
        # var_refs 被收集到 raw 中
        assert len(result.raw["var_refs_global"]) == 1
        assert result.raw["var_refs_global"][0][1].endswith(":x")

    def test_exception_propagates(self):
        """process_file 中 LSP 异常应向上传播。"""
        def mock_request(method, params):
            raise ConnectionError("LSP connection lost")

        with pytest.raises(ConnectionError, match="LSP connection lost"):
            process_file(mock_request, "/repo/test.cpp", "test.cpp", delay_between_calls=0)


class TestFindFunctionContainingLine:
    def test_exact_match(self):
        funcs = [
            FunctionSymbol(id="a.cpp:foo:10", name="foo", signature="", file_path="a.cpp", start_line=10, end_line=20),
            FunctionSymbol(id="a.cpp:bar:25", name="bar", signature="", file_path="a.cpp", start_line=25, end_line=35),
        ]
        fid = _find_function_containing_line(funcs, "a.cpp", 15)
        assert fid is not None
        assert fid.endswith(":foo:10")

    def test_fallback_to_nearest(self):
        funcs = [
            FunctionSymbol(id="a.cpp:foo:10", name="foo", signature="", file_path="a.cpp", start_line=10, end_line=20),
        ]
        # line 30 在 foo 范围外，但 start_line <= 30，fallback 选 foo
        fid = _find_function_containing_line(funcs, "a.cpp", 30)
        assert fid is not None
        assert fid.endswith(":foo:10")

    def test_no_match_returns_none(self):
        funcs = [
            FunctionSymbol(name="foo", signature="", file_path="a.cpp", start_line=10, end_line=20),
        ]
        fid = _find_function_containing_line(funcs, "b.cpp", 15)
        assert fid is None
