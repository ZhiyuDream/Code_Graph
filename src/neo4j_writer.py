"""
Neo4j 基础设施：Driver 工厂 + 仓库 Git 状态追踪。

批量写入逻辑已迁移到 src.ingestion.neo4j_writer（UNWIND 批量写入）。
此模块仅保留被多模块共享的基础函数。
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from neo4j import GraphDatabase

from config import NEO4J_DATABASE, NEO4J_PASSWORD, NEO4J_URI, NEO4J_USERNAME


def get_driver():
    """获取 Neo4j driver 实例。"""
    return GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD))


def get_head_commit(repo_root: Path) -> str:
    """返回仓库 HEAD 的 commit sha；失败返回空字符串。"""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0 and out.stdout:
            return out.stdout.strip()
    except Exception:
        pass
    return ""


def update_repository_commit(
    driver, repo_id: str, commit_sha: str, database: str = NEO4J_DATABASE
) -> None:
    with driver.session(database=database) as session:
        session.run(
            "MATCH (r:Repository {id: $id}) SET r.last_processed_commit = $sha",
            id=repo_id,
            sha=commit_sha,
        )
