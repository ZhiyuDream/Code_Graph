"""semantic_search 组件测试"""
import pytest
from unittest.mock import patch, MagicMock
import numpy as np


class TestSearchFunctionsByText:
    """search_functions_by_text: 基于 embedding 的语义搜索"""

    @patch('src.search.semantic_search._load_rag_index')
    @patch('src.search.semantic_search.get_embedding')
    def test_returns_source_field(self, mock_embed, mock_index):
        """返回结果必须包含 source='embedding' 字段"""
        from src.search.semantic_search import search_functions_by_text

        # Mock embedding
        mock_embed.return_value = [0.1] * 1536

        # Mock RAG index with one function chunk
        mock_index.return_value = {
            "chunks": [{
                "type": "function",
                "text": "void test_func() {}",
                "meta": {"name": "test_func", "file": "test.cpp", "start_line": 1, "end_line": 5}
            }],
            "embeddings": [[0.1] * 1536]
        }

        results = search_functions_by_text("test query", top_k=1)
        assert len(results) >= 1
        assert results[0]['source'] == 'embedding'

    @patch('src.search.semantic_search._load_rag_index')
    @patch('src.search.semantic_search.get_embedding')
    def test_returns_name_file_score(self, mock_embed, mock_index):
        """返回结果必须包含 name, file, score 字段"""
        from src.search.semantic_search import search_functions_by_text

        mock_embed.return_value = [0.1] * 1536
        mock_index.return_value = {
            "chunks": [{
                "type": "function",
                "text": "void my_func() {}",
                "meta": {"name": "my_func", "file": "src/my.cpp", "start_line": 10, "end_line": 20}
            }],
            "embeddings": [[0.1] * 1536]
        }

        results = search_functions_by_text("query", top_k=1)
        assert len(results) >= 1
        r = results[0]
        assert 'name' in r
        assert 'file' in r
        assert 'score' in r
        assert r['name'] == 'my_func'
        assert r['file'] == 'src/my.cpp'
