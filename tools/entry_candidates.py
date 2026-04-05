import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT / "src"))   # 核心库
sys.path.insert(0, str(_ROOT))             # 根目录（config.py）

"""
阶段 2：入口候选发现。从 Neo4j 通过图结构（无 CALLS 入边的 Function）得到入口函数列表。
不定死规则，后续可接入 agent（读文档、grep 等）再筛或增补。
"""
from __future__ import annotations

from typing import Any

from neo4j import GraphDatabase

from config import NEO4J_DATABASE


def get_entry_candidates(
    driver: GraphDatabase.driver,
    database: str = NEO4J_DATABASE,
) -> list[dict[str, Any]]:
    """
    从 Neo4j 获取入口候选：图中没有任何 CALLS 指向的 Function（图上的根）。
    返回 list of {"id", "name", "file_path", "source"}，source 固定为 "graph"。
    """
    out: list[dict[str, Any]] = []
    with driver.session(database=database) as session:
        r = session.run(
            """
            MATCH (f:Function)
            OPTIONAL MATCH (caller:Function)-[:CALLS]->(f)
            WITH f, caller
            WHERE caller IS NULL
            RETURN f.id AS id, f.name AS name, f.file_path AS file_path
            """
        )
        for rec in r:
            out.append({
                "id": rec["id"],
                "name": rec["name"] or "",
                "file_path": rec["file_path"] or "",
                "source": "graph",
            })
    return out
