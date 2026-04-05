"""
阶段 2：从入口函数沿 CALLS 做 BFS 展开，得到每个入口对应的调用子图（节点 + 边）。
"""
from __future__ import annotations

from typing import Any

from neo4j import GraphDatabase

from config import NEO4J_DATABASE


def expand_from_entry(
    driver: GraphDatabase.driver,
    entry_id: str,
    depth_limit: int = 5,
    node_limit: int = 500,
    database: str = NEO4J_DATABASE,
) -> dict[str, Any]:
    """
    从单个入口 Function.id 沿 CALLS 出边 BFS，直到深度或节点数上限。
    返回 {"entry_id": str, "function_ids": list[str], "edges": list[(from_id, to_id)]}。
    """
    function_ids: set[str] = {entry_id}
    edges: list[tuple[str, str]] = []
    frontier = {entry_id}
    depth = 0

    while depth < depth_limit and len(function_ids) < node_limit and frontier:
        with driver.session(database=database) as session:
            r = session.run(
                """
                MATCH (a:Function)-[:CALLS]->(b:Function)
                WHERE a.id IN $frontier
                RETURN a.id AS from_id, b.id AS to_id
                """,
                frontier=list(frontier),
            )
            next_frontier: set[str] = set()
            for rec in r:
                from_id, to_id = rec["from_id"], rec["to_id"]
                edges.append((from_id, to_id))
                if to_id not in function_ids:
                    function_ids.add(to_id)
                    next_frontier.add(to_id)
                    if len(function_ids) >= node_limit:
                        break
        frontier = next_frontier
        depth += 1

    return {
        "entry_id": entry_id,
        "function_ids": sorted(function_ids),
        "edges": list(edges),
    }


def expand_all_entries(
    driver: GraphDatabase.driver,
    candidates: list[dict[str, Any]],
    depth_limit: int = 5,
    node_limit: int = 500,
    database: str = NEO4J_DATABASE,
) -> list[dict[str, Any]]:
    """
    对每个候选入口做 expand_from_entry，返回子图列表。
    candidates 每项需含 "id"（Function.id）。
    """
    out: list[dict[str, Any]] = []
    for c in candidates:
        entry_id = c.get("id")
        if not entry_id:
            continue
        sub = expand_from_entry(driver, entry_id, depth_limit, node_limit, database)
        out.append(sub)
    return out
