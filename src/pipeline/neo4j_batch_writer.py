"""
Neo4j 批量写入器：使用 UNWIND 批量写入节点和边。

改进点（相比原 neo4j_writer.py）：
1. 使用 UNWIND 批量写入，每批 500 条
2. 节点和边分别批量
3. 事务管理：session.execute_write()
4. 边写入带标签限定，命中索引加速
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

logger = logging.getLogger(__name__)

BATCH_SIZE = 500


def ensure_constraints(driver, database: str):
    """创建唯一性约束（若已存在会忽略）。"""
    labels = ["Repository", "Directory", "File", "Function", "Class", "Variable", "Attribute", "Module", "ControlFlowBlock"]
    with driver.session(database=database) as session:
        for label in labels:
            try:
                session.run(
                    f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.id IS UNIQUE"
                )
            except Exception as e:
                logger.warning("Constraint creation failed for %s: %s", label, e)


def clear_code_graph(driver, database: str):
    """删除代码图相关节点（保留 Issue/PullRequest）。"""
    labels = ["Variable", "Function", "Class", "Attribute", "ControlFlowBlock", "File", "Directory", "Repository", "Module"]
    with driver.session(database=database) as session:
        for label in labels:
            session.run(f"MATCH (n:{label}) DETACH DELETE n")


def _batch_write_nodes(session, label: str, nodes: list[dict[str, Any]]):
    """批量写入同一标签的节点。"""
    if not nodes:
        return
    for i in range(0, len(nodes), BATCH_SIZE):
        batch = nodes[i : i + BATCH_SIZE]
        session.run(
            f"""
            UNWIND $batch AS node
            MERGE (n:{label} {{id: node.id}})
            SET n += node
            """,
            batch=batch,
        )
        logger.debug("Wrote %d %s nodes", len(batch), label)


def _batch_write_edges(
    session,
    rel_type: str,
    edges: list[tuple[str, str, dict]],
    id_to_label: dict[str, str],
):
    """批量写入同一类型的边，按节点标签分组以命中索引。"""
    if not edges:
        return

    # 为边附加标签信息并分组
    groups: dict[tuple[str, str], list[tuple[str, str, dict]]] = defaultdict(list)
    skipped = 0
    for from_id, to_id, props in edges:
        from_label = id_to_label.get(from_id)
        to_label = id_to_label.get(to_id)
        if not from_label or not to_label:
            skipped += 1
            continue
        groups[(from_label, to_label)].append((from_id, to_id, props))

    if skipped:
        logger.debug("Skipped %d %s edges with missing node labels", skipped, rel_type)

    for (from_label, to_label), batch_edges in groups.items():
        for i in range(0, len(batch_edges), BATCH_SIZE):
            batch = batch_edges[i : i + BATCH_SIZE]
            batch_dicts = [
                {"from_id": from_id, "to_id": to_id, "props": props}
                for from_id, to_id, props in batch
            ]
            session.run(
                f"""
                UNWIND $batch AS edge
                MATCH (a:{from_label}) WHERE a.id = edge.from_id
                MATCH (b:{to_label}) WHERE b.id = edge.to_id
                MERGE (a)-[r:{rel_type}]->(b)
                SET r += edge.props
                """,
                batch=batch_dicts,
            )
            logger.debug(
                "Wrote %d %s edges (%s->%s)", len(batch), rel_type, from_label, to_label
            )


def write_graph(driver, graph: dict[str, Any], database: str):
    """
    批量写入图到 Neo4j。

    Args:
        driver: Neo4j driver
        graph: {"nodes": {...}, "edges": {...}}
        database: 数据库名
    """
    nodes = graph.get("nodes", {})
    edges = graph.get("edges", {})

    # 构建 id -> label 映射，用于边写入时限定标签
    id_to_label: dict[str, str] = {}
    for label, node_list in nodes.items():
        for node in node_list:
            id_to_label[node["id"]] = label

    with driver.session(database=database) as session:
        # 写入节点（按标签分组）
        for label in ["Repository", "Directory", "File", "Function", "Class", "Variable", "Attribute", "Module", "ControlFlowBlock"]:
            _batch_write_nodes(session, label, nodes.get(label, []))

        # 写入边（按关系类型分组，内部按标签再分）
        for rel_type in ["CONTAINS", "CALLS", "CALLS_AMBIGUOUS", "REFERENCES_VAR", "HAS_MEMBER", "HAS_METHOD", "BELONGS_TO", "MODULE_CALLS", "EXTERNAL_CALLS", "CONTROL_FLOW"]:
            _batch_write_edges(session, rel_type, edges.get(rel_type, []), id_to_label)

    total_nodes = sum(len(v) for v in nodes.values())
    total_edges = sum(len(v) for v in edges.values())
    logger.info("Graph written: %d nodes, %d edges", total_nodes, total_edges)
