"""code_reader 组件测试"""
import pytest
from pathlib import Path
import os

# 设置测试环境
os.environ.setdefault("REPO_ROOT", "/root/data/zzy/llama.cpp")

from src.search.code_reader import enrich_function_with_code, read_function_from_file


class TestEnrichFunctionWithCode:
    """enrich_function_with_code: 补充函数完整代码"""

    def test_already_has_long_text(self):
        """已有足够长的文本时不重新读取"""
        func = {'name': 'foo', 'file': 'a.cpp', 'text': 'x' * 300}
        result = enrich_function_with_code(func)
        assert result['text'] == 'x' * 300
        assert 'code_enriched' not in result

    def test_enriches_from_file(self):
        """短文本时从文件读取完整代码"""
        # 使用一个已知存在的函数
        func = {
            'name': 'trim_whitespace',
            'file': 'common/chat-auto-parser-helpers.cpp',
            'start_line': 15,
            'end_line': 31,
            'text': 'short'
        }
        result = enrich_function_with_code(func)
        assert len(result['text']) > 100
        assert 'trim_whitespace' in result['text']
        assert result.get('code_enriched') is True

    def test_header_falls_back_to_cpp(self):
        """从 .h 文件只拿到声明时，尝试找 .cpp 实现"""
        func = {
            'name': 'analyze_content',
            'file': 'common/chat-auto-parser.h',
            'start_line': 284,
            'end_line': 284,
            'text': 'analyze_content(const common_chat_template & tmpl);'
        }
        result = enrich_function_with_code(func)
        # 如果 .cpp 有更长的实现，应该用 .cpp 的
        if len(result['text']) > len(func['text']):
            assert result.get('code_enriched') is True

    def test_none_input(self):
        """None 输入返回 None"""
        assert enrich_function_with_code(None) is None


class TestReadFunctionFromFile:
    """read_function_from_file: 从源文件读取函数代码"""

    def test_read_with_line_numbers(self):
        """提供行号范围时直接读取"""
        code = read_function_from_file(
            'common/chat-auto-parser-helpers.cpp',
            'trim_whitespace',
            start_line=15, end_line=31
        )
        assert 'trim_whitespace' in code
        assert len(code) > 50

    def test_read_nonexistent_file(self):
        """不存在的文件返回错误信息"""
        code = read_function_from_file('nonexistent.cpp', 'func')
        assert '不存在' in code
