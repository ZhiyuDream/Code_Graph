"""answer_generator 组件测试"""
import pytest
from src.core.answer_generator import build_context


class TestBuildContext:
    """build_context: 输入 collected dict，输出格式化上下文字符串"""

    def test_embedding_funcs_included(self):
        """embedding 搜索结果应包含在上下文中，含完整代码"""
        funcs = [
            {'name': 'foo', 'file': 'a.cpp', 'text': 'void foo() { return; }',
             'score': 0.7, 'source': 'embedding'},
        ]
        collected = {'functions': funcs, 'issues': [], 'steps': []}
        ctx = build_context(collected)
        assert 'foo' in ctx
        assert 'void foo()' in ctx
        assert '高相关函数' in ctx

    def test_low_score_embedding_funcs_included(self):
        """低分 embedding 结果（<0.5）也应包含在上下文中"""
        funcs = [
            {'name': 'bar', 'file': 'b.cpp', 'text': 'int bar(int x) { return x; }',
             'score': 0.3, 'source': 'embedding'},
        ]
        collected = {'functions': funcs, 'issues': [], 'steps': []}
        ctx = build_context(collected)
        assert 'bar' in ctx
        assert 'int bar' in ctx

    def test_grep_funcs_included(self):
        """grep 搜索结果应包含在上下文中"""
        funcs = [
            {'name': 'baz', 'file': 'c.cpp', 'text': 'void baz() {}',
             'score': 0.5, 'source': 'grep_fallback'},
        ]
        collected = {'functions': funcs, 'issues': [], 'steps': []}
        ctx = build_context(collected)
        assert 'baz' in ctx
        assert 'Grep' in ctx

    def test_chain_funcs_with_code(self):
        """调用链扩展的函数应包含代码"""
        funcs = [
            {'name': 'caller1', 'file': 'd.cpp', 'text': 'void caller1() { baz(); }',
             'score': 0.5, 'source': 'callers_of_baz'},
        ]
        collected = {'functions': funcs, 'issues': [], 'steps': []}
        ctx = build_context(collected)
        assert 'caller1' in ctx
        assert 'caller1()' in ctx

    def test_issues_included(self):
        """Issue 信息应包含在上下文中"""
        issues = [{'number': 123, 'title': 'Fix bug', 'body': 'Description'}]
        collected = {'functions': [], 'issues': issues, 'steps': []}
        ctx = build_context(collected)
        assert '#123' in ctx
        assert 'Fix bug' in ctx

    def test_empty_collected(self):
        """空 collected 不应崩溃"""
        collected = {'functions': [], 'issues': [], 'steps': []}
        ctx = build_context(collected)
        assert isinstance(ctx, str)

    def test_no_code_truncation(self):
        """代码不应被截断"""
        long_code = 'void func() {\n' + '  int x = 1;\n' * 100 + '}'
        funcs = [
            {'name': 'func', 'file': 'e.cpp', 'text': long_code,
             'score': 0.7, 'source': 'embedding'},
        ]
        collected = {'functions': funcs, 'issues': [], 'steps': []}
        ctx = build_context(collected)
        # 完整代码应该在上下文中，不应有截断标记
        assert '...(截断)' not in ctx
        assert 'int x = 1;' in ctx
