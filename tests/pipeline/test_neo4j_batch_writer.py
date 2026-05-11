"""Tests for neo4j_batch_writer.py"""
from __future__ import annotations

from unittest.mock import MagicMock, call

import pytest

from src.pipeline.neo4j_batch_writer import (
    BATCH_SIZE,
    clear_code_graph,
    ensure_constraints,
    write_graph,
)


class MockSession:
    """模拟 Neo4j Session。"""
    def __init__(self):
        self.runs = []

    def run(self, query, **kwargs):
        self.runs.append((query, kwargs))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


class MockDriver:
    """模拟 Neo4j Driver。"""
    def __init__(self):
        self._session = MockSession()

    def session(self, database: str = ""):
        return self._session


class TestEnsureConstraints:
    def test_creates_constraints(self):
        driver = MockDriver()
        ensure_constraints(driver, "neo4j")

        queries = [r[0] for r in driver._session.runs]
        assert any("Repository" in q for q in queries)
        assert any("Function" in q for q in queries)
        assert all("CREATE CONSTRAINT IF NOT EXISTS" in q for q in queries)


class TestClearCodeGraph:
    def test_deletes_in_order(self):
        driver = MockDriver()
        clear_code_graph(driver, "neo4j")

        queries = [r[0] for r in driver._session.runs]
        expected_labels = ["Variable", "Function", "Class", "Attribute", "File", "Directory", "Repository"]
        for label in expected_labels:
            assert any(f"MATCH (n:{label}) DETACH DELETE n" in q for q in queries)


class TestWriteGraph:
    def test_batch_nodes_and_edges(self):
        driver = MockDriver()
        graph = {
            "nodes": {
                "Function": [
                    {"id": f"f{i}", "name": f"func{i}"}
                    for i in range(3)
                ],
                "Class": [
                    {"id": "c1", "name": "MyClass"}
                ],
            },
            "edges": {
                "CALLS": [
                    ("f0", "f1", {}),
                    ("f1", "f2", {}),
                ],
                "HAS_MEMBER": [
                    ("c1", "m1", {}),
                ],
            },
        }
        write_graph(driver, graph, "neo4j")

        runs = driver._session.runs
        # 验证节点写入使用了 UNWIND
        function_query = next(r[0] for r in runs if "Function" in r[0] and "UNWIND" in r[0])
        assert "MERGE (n:Function {id: node.id})" in function_query

        # 验证边写入使用了 UNWIND
        calls_query = next(r[0] for r in runs if "CALLS" in r[0] and "UNWIND" in r[0])
        assert "MERGE (a)-[r:CALLS]->(b)" in calls_query

        # 验证 batch 参数
        batch_run = next(r for r in runs if "Function" in r[0] and "UNWIND" in r[0])
        assert len(batch_run[1]["batch"]) == 3

    def test_large_batch_split(self):
        """大量节点应被拆分为多个 batch。"""
        driver = MockDriver()
        graph = {
            "nodes": {
                "Function": [
                    {"id": f"f{i}", "name": f"func{i}"}
                    for i in range(BATCH_SIZE + 100)
                ],
            },
            "edges": {"CALLS": []},
        }
        write_graph(driver, graph, "neo4j")

        function_runs = [
            r for r in driver._session.runs
            if "Function" in r[0] and "UNWIND" in r[0]
        ]
        assert len(function_runs) == 2
        assert len(function_runs[0][1]["batch"]) == BATCH_SIZE
        assert len(function_runs[1][1]["batch"]) == 100

    def test_empty_graph(self):
        driver = MockDriver()
        write_graph(driver, {"nodes": {}, "edges": {}}, "neo4j")
        # 没有 UNWIND 查询（因为没有节点/边）
        unwind_runs = [r for r in driver._session.runs if "UNWIND" in r[0]]
        assert len(unwind_runs) == 0

    def test_calls_ambiguous_edge_skipped(self):
        """CALLS_AMBIGUOUS 边若 to 节点不存在于图中，应被跳过。"""
        driver = MockDriver()
        graph = {
            "nodes": {"Function": [{"id": "f1", "name": "main"}]},
            "edges": {
                "CALLS_AMBIGUOUS": [
                    ("f1", "ambiguous:foo", {"callee_name": "foo", "candidates": ["a", "b"]}),
                ],
            },
        }
        write_graph(driver, graph, "neo4j")

        # ambiguous:foo 不是真实节点，边应被跳过
        calls_queries = [r for r in driver._session.runs if "CALLS_AMBIGUOUS" in r[0]]
        assert len(calls_queries) == 0
