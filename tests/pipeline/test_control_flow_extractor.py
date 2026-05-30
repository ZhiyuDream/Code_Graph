"""
Tests for src/pipeline/control_flow_extractor.py
"""
from __future__ import annotations

import pytest

from src.pipeline.control_flow_extractor import (
    ControlFlowBlock,
    extract_control_flow_for_function,
)


class TestExtractControlFlow:
    """Tests for control flow extraction."""

    def test_if_statement(self):
        """提取 if 语句。"""
        lines = [
            "void foo() {",
            "    if (ctx == nullptr) {",
            "        return nullptr;",
            "    }",
            "}",
        ]
        blocks = extract_control_flow_for_function(
            "f:foo", "a.cpp", 1, 5, lines,
        )

        assert len(blocks) == 2
        assert blocks[0].type == "if"
        assert blocks[0].condition == "ctx == nullptr"
        assert blocks[0].line == 2
        assert blocks[1].type == "return"
        assert blocks[1].is_error_path is True
        assert blocks[1].line == 3

    def test_else_if_and_else(self):
        """提取 else if 和 else。"""
        lines = [
            "void bar() {",
            "    if (x > 0) {",
            "        do_a();",
            "    } else if (x < 0) {",
            "        do_b();",
            "    } else {",
            "        do_c();",
            "    }",
            "}",
        ]
        blocks = extract_control_flow_for_function(
            "f:bar", "a.cpp", 1, 9, lines,
        )

        types = [b.type for b in blocks]
        assert "if" in types
        assert "else_if" in types
        assert "else" in types

    def test_switch_and_case(self):
        """提取 switch/case。"""
        lines = [
            "void baz(int x) {",
            "    switch (x) {",
            "        case 1: break;",
            "        case 2: break;",
            "        default: break;",
            "    }",
            "}",
        ]
        blocks = extract_control_flow_for_function(
            "f:baz", "a.cpp", 1, 7, lines,
        )

        types = [b.type for b in blocks]
        assert "switch" in types
        assert "case" in types
        assert "default" in types

    def test_try_catch(self):
        """提取 try/catch。"""
        lines = [
            "void qux() {",
            "    try {",
            "        risky();",
            "    } catch (const std::exception & e) {",
            "        handle(e);",
            "    }",
            "}",
        ]
        blocks = extract_control_flow_for_function(
            "f:qux", "a.cpp", 1, 7, lines,
        )

        types = [b.type for b in blocks]
        assert "try" in types
        assert "catch" in types

    def test_error_returns(self):
        """识别错误返回路径。"""
        lines = [
            "int alloc() {",
            "    void * p = malloc(10);",
            "    if (!p) return nullptr;",
            "    if (errno) return NULL;",
            "    if (failed) return false;",
            "    if (err) return -1;",
            "    if (bad) return {};",
            "    if (oops) return 0;",
            "    if (ex) throw std::bad_alloc();",
            "    return 1;",
            "}",
        ]
        blocks = extract_control_flow_for_function(
            "f:alloc", "a.cpp", 1, 11, lines,
        )

        error_returns = [b for b in blocks if b.is_error_path]
        # nullptr, NULL, false, -1, {}, 0, throw
        assert len(error_returns) == 7

    def test_skip_comments_and_empty(self):
        """跳过注释和空行。"""
        lines = [
            "void foo() {",
            "    // check null",
            "",
            "    if (x) {",
            "        return;",
            "    }",
            "}",
        ]
        blocks = extract_control_flow_for_function(
            "f:foo", "a.cpp", 1, 7, lines,
        )

        # 只应有 if 和 return（return true 不算错误路径，但 return; 也不匹配）
        assert any(b.type == "if" for b in blocks)

    def test_no_control_flow(self):
        """无控制流的函数——简单返回被过滤，但表达式返回被保留。"""
        lines = [
            "int add(int a, int b) {",
            "    return a + b;",
            "}",
        ]
        blocks = extract_control_flow_for_function(
            "f:add", "a.cpp", 1, 3, lines,
        )

        # return a + b 包含运算符，不是简单表达式，会被记录
        assert len(blocks) == 1
        assert blocks[0].type == "return"
        assert blocks[0].condition == "a + b"
        assert blocks[0].is_error_path is False

    def test_multiline_condition_truncated(self):
        """跨行条件应被截断。"""
        lines = [
            "void foo() {",
            "    if (very_long_condition_that_goes_on_and_on_and_on_and_on",
            "        && another_part) {",
            "        return;",
            "    }",
            "}",
        ]
        blocks = extract_control_flow_for_function(
            "f:foo", "a.cpp", 1, 6, lines,
        )

        # 第一行匹配到 if，条件被截断
        if_block = [b for b in blocks if b.type == "if"]
        assert len(if_block) == 1
        assert len(if_block[0].condition) <= 123  # 截断到 120 + "..."

    def test_function_id_assigned(self):
        """ControlFlowBlock 应关联正确的 function_id。"""
        lines = [
            "void foo() {",
            "    if (x) { return; }",
            "}",
        ]
        blocks = extract_control_flow_for_function(
            "my_func_id", "a.cpp", 1, 3, lines,
        )

        for b in blocks:
            assert b.function_id == "my_func_id"
            assert b.file_path == "a.cpp"
