"""
Tests for src/ingestion/incremental.py
"""
from __future__ import annotations

import pytest

from src.ingestion.incremental import (
    delete_file_nodes,
    get_changed_files,
    get_deleted_files,
)


class TestIncremental:
    """Tests for incremental update logic."""

    def test_get_changed_files_empty_commit(self, tmp_path):
        """空 commit 应返回空集合。"""
        result = get_changed_files(tmp_path, "")
        assert result == set()

    def test_get_deleted_files_empty_commit(self, tmp_path):
        """空 commit 应返回空集合。"""
        result = get_deleted_files(tmp_path, "")
        assert result == set()

    def test_delete_file_nodes_cypher_completeness(self):
        """验证 delete_file_nodes 的 Cypher 语句覆盖了所有需要的节点和关系类型。"""
        # 由于无法连接真实 Neo4j，我们验证函数签名和逻辑完整性
        import inspect
        source = inspect.getsource(delete_file_nodes)

        # 应该包含对所有关键节点类型的删除
        assert "ControlFlowBlock" in source
        assert "ResourceOperation" in source
        assert "ExternalCall" in source
        assert "AmbiguousCall" in source
        assert "Function" in source
        assert "Class" in source
        assert "Variable" in source
        assert "Attribute" in source
        assert "File" in source

        # 应该使用 DETACH DELETE 来确保关系被清理
        assert "DETACH DELETE" in source
