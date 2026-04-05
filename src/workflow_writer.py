"""
阶段 2：将 Workflow 子图写入 Neo4j。创建 Workflow 节点、WORKFLOW_ENTRY、PART_OF_WORKFLOW。
"""
from __future__ import annotations

from typing import Any

from neo4j import GraphDatabase

from config import NEO4J_DATABASE


def ensure_workflow_constraint(driver: GraphDatabase.driver, database: str = NEO4J_DATABASE) -> None:
    """创建 Workflow 的 id 唯一约束（若已存在会忽略）。"""
    with driver.session(database=database) as s:
        try:
            s.run("CREATE CONSTRAINT IF NOT EXISTS FOR (n:Workflow) REQUIRE n.id IS UNIQUE")
        except Exception:
            pass


def clear_workflows(driver: GraphDatabase.driver, database: str = NEO4J_DATABASE) -> None:
    """删除所有 Workflow 节点及其 WORKFLOW_ENTRY、PART_OF_WORKFLOW 边。"""
    with driver.session(database=database) as s:
        s.run("MATCH (w:Workflow) DETACH DELETE w")


def write_workflows(
    driver: GraphDatabase.driver,
    subgraphs: list[dict[str, Any]],
    depth_limit: int = 5,
    node_limit: int = 500,
    database: str = NEO4J_DATABASE,
) -> None:
    """
    将展开得到的子图列表写入 Neo4j。
    subgraphs 每项含 entry_id, function_ids, edges（edges 仅用于信息，关系由 function_ids 决定）。
    为每个子图创建 Workflow 节点，并建立 WORKFLOW_ENTRY、PART_OF_WORKFLOW。
    """
    with driver.session(database=database) as session:
        for sub in subgraphs:
            entry_id = sub.get("entry_id")
            function_ids = sub.get("function_ids") or []
            if not entry_id:
                continue
            workflow_id = f"workflow_{entry_id}"
            node_count = len(function_ids)
            summary_text = sub.get("summary_text") or ""

            session.run(
                """
                MERGE (w:Workflow {id: $workflow_id})
                SET w.entry_function_id = $entry_id, w.summary_text = $summary_text,
                    w.node_count = $node_count, w.depth_limit = $depth_limit, w.node_limit = $node_limit
                """,
                workflow_id=workflow_id,
                entry_id=entry_id,
                summary_text=summary_text,
                node_count=node_count,
                depth_limit=depth_limit,
                node_limit=node_limit,
            )
            # WORKFLOW_ENTRY: 入口 Function -> Workflow
            session.run(
                """
                MATCH (f:Function {id: $entry_id}), (w:Workflow {id: $workflow_id})
                MERGE (f)-[:WORKFLOW_ENTRY]->(w)
                """,
                entry_id=entry_id,
                workflow_id=workflow_id,
            )
            # PART_OF_WORKFLOW: 每个参与函数 -> Workflow
            for fid in function_ids:
                session.run(
                    """
                    MATCH (f:Function {id: $fid}), (w:Workflow {id: $workflow_id})
                    MERGE (f)-[:PART_OF_WORKFLOW]->(w)
                    """,
                    fid=fid,
                    workflow_id=workflow_id,
                )
