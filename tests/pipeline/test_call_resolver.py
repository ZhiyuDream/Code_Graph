"""Tests for call_resolver.py"""
from __future__ import annotations

import pytest

from src.pipeline.call_resolver import (
    build_function_lookup,
    resolve_call,
    resolve_all_calls,
)
from src.pipeline.models import FunctionSymbol, RawCall, ResolvedCalls


class TestBuildFunctionLookup:
    def test_basic_indexing(self):
        funcs = [
            FunctionSymbol(id="a.cpp:foo:10", name="foo", file_path="a.cpp", start_line=10, end_line=20),
            FunctionSymbol(id="a.cpp:foo:30", name="foo", file_path="a.cpp", start_line=30, end_line=40),
            FunctionSymbol(id="b.cpp:bar:5", name="bar", file_path="b.cpp", start_line=5, end_line=15),
        ]
        lookup = build_function_lookup(funcs)

        assert lookup.by_location[("a.cpp", "foo", 10)] == "a.cpp:foo:10"
        assert lookup.by_location[("a.cpp", "foo", 30)] == "a.cpp:foo:30"
        assert len(lookup.by_name[("a.cpp", "foo")]) == 2
        assert lookup.by_id["a.cpp:foo:10"].name == "foo"

    def test_empty_list(self):
        lookup = build_function_lookup([])
        assert lookup.by_location == {}
        assert lookup.by_name == {}


class TestResolveCall:
    def test_resolve_by_callee_line_exact(self):
        """利用 callee_line 精确匹配重载函数。"""
        funcs = [
            FunctionSymbol(id="a.cpp:foo:10", name="foo", file_path="a.cpp", start_line=10, end_line=20),
            FunctionSymbol(id="a.cpp:foo:30", name="foo", file_path="a.cpp", start_line=30, end_line=40),
        ]
        lookup = build_function_lookup(funcs)

        # callee_line=35 应匹配到第二个 foo（line 30-40）
        raw = RawCall(caller_index=0, callee_name="foo", file_path="a.cpp", line=5, callee_file_path="a.cpp", callee_line=35)
        callee_id, status, candidates = resolve_call(lookup, raw, "b.cpp:main:1")
        assert status == "resolved"
        assert callee_id == "a.cpp:foo:30"
        assert candidates is None

    def test_resolve_by_unique_name(self):
        """只有一个候选时，直接匹配。"""
        funcs = [
            FunctionSymbol(id="a.cpp:foo:10", name="foo", file_path="a.cpp", start_line=10, end_line=20),
        ]
        lookup = build_function_lookup(funcs)

        raw = RawCall(caller_index=0, callee_name="foo", file_path="a.cpp", line=5)
        callee_id, status, candidates = resolve_call(lookup, raw, "b.cpp:main:1")
        assert status == "resolved"
        assert callee_id == "a.cpp:foo:10"

    def test_ambiguous_multiple_candidates(self):
        """多个候选且 callee_line 无法区分时，标记 AMBIGUOUS。"""
        funcs = [
            FunctionSymbol(id="a.cpp:foo:10", name="foo", file_path="a.cpp", start_line=10, end_line=20),
            FunctionSymbol(id="a.cpp:foo:30", name="foo", file_path="a.cpp", start_line=30, end_line=40),
        ]
        lookup = build_function_lookup(funcs)

        # callee_line=None，无法区分两个 foo
        raw = RawCall(caller_index=0, callee_name="foo", file_path="a.cpp", line=5, callee_file_path="a.cpp", callee_line=None)
        callee_id, status, candidates = resolve_call(lookup, raw, "b.cpp:main:1")
        assert status == "ambiguous"
        assert callee_id is None
        assert len(candidates) == 2
        assert "a.cpp:foo:10" in candidates
        assert "a.cpp:foo:30" in candidates

    def test_unresolved_no_candidates(self):
        """无候选时标记 UNRESOLVED。"""
        lookup = build_function_lookup([])

        raw = RawCall(caller_index=0, callee_name="foo", file_path="a.cpp", line=5)
        callee_id, status, candidates = resolve_call(lookup, raw, "b.cpp:main:1")
        assert status == "unresolved"
        assert callee_id is None
        assert candidates is None

    def test_cross_file_call(self):
        """跨文件调用：使用 callee_file_path 查找。"""
        funcs = [
            FunctionSymbol(id="a.cpp:foo:10", name="foo", file_path="a.cpp", start_line=10, end_line=20),
            FunctionSymbol(id="b.cpp:bar:5", name="bar", file_path="b.cpp", start_line=5, end_line=15),
        ]
        lookup = build_function_lookup(funcs)

        raw = RawCall(
            caller_index=0, callee_name="bar", file_path="a.cpp", line=12,
            callee_file_path="b.cpp", callee_line=8,
        )
        callee_id, status, candidates = resolve_call(lookup, raw, "a.cpp:foo:10")
        assert status == "resolved"
        assert callee_id == "b.cpp:bar:5"

    def test_fallback_to_same_file(self):
        """callee_file_path 中找不到时，fallback 到 caller 所在文件。"""
        funcs = [
            FunctionSymbol(id="a.cpp:foo:10", name="foo", file_path="a.cpp", start_line=10, end_line=20),
        ]
        lookup = build_function_lookup(funcs)

        # callee_file_path="b.cpp" 中没有 foo，fallback 到 "a.cpp"
        raw = RawCall(
            caller_index=0, callee_name="foo", file_path="a.cpp", line=12,
            callee_file_path="b.cpp", callee_line=None,
        )
        callee_id, status, candidates = resolve_call(lookup, raw, "a.cpp:main:1")
        assert status == "resolved"
        assert callee_id == "a.cpp:foo:10"


class TestResolveAllCalls:
    def test_batch_resolution(self):
        funcs = [
            FunctionSymbol(id="a.cpp:main:1", name="main", file_path="a.cpp", start_line=1, end_line=10),
            FunctionSymbol(id="a.cpp:foo:10", name="foo", file_path="a.cpp", start_line=10, end_line=20),
            FunctionSymbol(id="a.cpp:foo:30", name="foo", file_path="a.cpp", start_line=30, end_line=40),
            FunctionSymbol(id="a.cpp:bar:50", name="bar", file_path="a.cpp", start_line=50, end_line=60),
        ]
        raw_calls = [
            # main -> foo（callee_line=35，精确匹配第二个 foo）
            RawCall(caller_index=0, callee_name="foo", file_path="a.cpp", line=5, callee_file_path="a.cpp", callee_line=35),
            # main -> bar（唯一候选）
            RawCall(caller_index=0, callee_name="bar", file_path="a.cpp", line=6, callee_file_path="a.cpp", callee_line=55),
            # foo -> foo（自调用，应被过滤）
            RawCall(caller_index=1, callee_name="foo", file_path="a.cpp", line=15, callee_file_path="a.cpp", callee_line=35),
            # main -> baz（不存在）
            RawCall(caller_index=0, callee_name="baz", file_path="a.cpp", line=7),
        ]

        result = resolve_all_calls(funcs, raw_calls)

        # main->foo(第二个), main->bar, foo(第一个)->foo(第二个)
        # 注意：foo(第一个) 调用 foo(第二个) 是重载间调用，不是自调用，应保留
        assert len(result.calls) == 3
        assert ("a.cpp:main:1", "a.cpp:foo:30") in result.calls
        assert ("a.cpp:main:1", "a.cpp:bar:50") in result.calls
        assert ("a.cpp:foo:10", "a.cpp:foo:30") in result.calls

        assert len(result.unresolved) == 1
        assert result.unresolved[0] == ("a.cpp:main:1", "baz")

    def test_self_call_filtered(self):
        """真正的自调用（caller_id == callee_id）应被过滤。"""
        funcs = [
            FunctionSymbol(id="a.cpp:foo:10", name="foo", file_path="a.cpp", start_line=10, end_line=20),
        ]
        raw_calls = [
            # foo -> foo（真正的自调用：caller 和 callee 是同一个函数）
            RawCall(caller_index=0, callee_name="foo", file_path="a.cpp", line=15, callee_file_path="a.cpp", callee_line=15),
        ]
        result = resolve_all_calls(funcs, raw_calls)
        # 自调用被过滤
        assert result.calls == []
        # 也不算 unresolved，直接跳过
        assert result.unresolved == []

    def test_invalid_caller_index(self):
        funcs = [
            FunctionSymbol(id="a.cpp:foo:10", name="foo", file_path="a.cpp", start_line=10, end_line=20),
        ]
        raw_calls = [
            RawCall(caller_index=99, callee_name="bar", file_path="a.cpp", line=5),
        ]
        result = resolve_all_calls(funcs, raw_calls)
        assert result.calls == []
        assert result.ambiguous == []
        assert result.unresolved == []
