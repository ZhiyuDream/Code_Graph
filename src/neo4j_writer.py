"""
将 graph_builder 输出的图写入 Neo4j。
节点：Repository, Directory, File, Function, Class, Variable, Attribute。
边：CONTAINS（含 Class→Attribute 等）, CALLS（Function→Function）, REFERENCES_VAR（Function→Variable，属性 lines），HAS_MEMBER（Class→Attribute）。
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from neo4j import GraphDatabase

from config import NEO4J_DATABASE, NEO4J_PASSWORD, NEO4J_URI, NEO4J_USERNAME


def get_driver():
    return GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD))


def ensure_constraints(driver, database: str = NEO4J_DATABASE):
    """创建唯一性约束便于 MERGE（若已存在会忽略）。"""
    with driver.session(database=database) as s:
        for label, key in [
            ("Repository", "id"),
            ("Directory", "id"),
            ("File", "id"),
            ("Function", "id"),
            ("Class", "id"),
            ("Variable", "id"),
            ("Attribute", "id"),
        ]:
            try:
                s.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE")
            except Exception:
                pass


def clear_code_graph(driver, database: str = NEO4J_DATABASE):
    """删除当前代码图相关节点与边（保留 Workflow/Issue/PullRequest 若后续有）。"""
    with driver.session(database=database) as s:
        s.run("MATCH (n:Variable) DETACH DELETE n")
        s.run("MATCH (n:Function) DETACH DELETE n")
        s.run("MATCH (n:Class) DETACH DELETE n")
        s.run("MATCH (n:Attribute) DETACH DELETE n")
        s.run("MATCH (n:File) DETACH DELETE n")
        s.run("MATCH (n:Directory) DETACH DELETE n")
        s.run("MATCH (n:Repository) DETACH DELETE n")


def write_graph(driver, graph: dict[str, Any], database: str = NEO4J_DATABASE) -> None:
    nodes = graph["nodes"]
    edges = graph["edges"]

    with driver.session(database=database) as session:
        # Repository
        for r in nodes.get("Repository", []):
            session.run(
                "MERGE (r:Repository {id: $id}) SET r.root_path = $root_path, r.last_processed_commit = $last_processed_commit",
                id=r["id"],
                root_path=r.get("root_path", ""),
                last_processed_commit=r.get("last_processed_commit", ""),
            )
        # Directory
        for d in nodes.get("Directory", []):
            session.run(
                "MERGE (n:Directory {id: $id}) SET n.path = $path, n.name = $name",
                id=d["id"],
                path=d.get("path", ""),
                name=d.get("name", ""),
            )
        # File
        for f in nodes.get("File", []):
            session.run(
                "MERGE (n:File {id: $id}) SET n.path = $path, n.name = $name, n.language = $language",
                id=f["id"],
                path=f.get("path", ""),
                name=f.get("name", ""),
                language=f.get("language", ""),
            )
        # Function
        for f in nodes.get("Function", []):
            session.run(
                """MERGE (n:Function {id: $id})
                   SET n.name = $name, n.signature = $signature, n.file_path = $file_path,
                       n.start_line = $start_line, n.end_line = $end_line""",
                id=f["id"],
                name=f.get("name", ""),
                signature=f.get("signature", ""),
                file_path=f.get("file_path", ""),
                start_line=f.get("start_line", 0),
                end_line=f.get("end_line", 0),
            )
        # Class
        for c in nodes.get("Class", []):
            session.run(
                """MERGE (n:Class {id: $id})
                   SET n.name = $name, n.file_path = $file_path, n.start_line = $start_line, n.end_line = $end_line""",
                id=c["id"],
                name=c.get("name", ""),
                file_path=c.get("file_path", ""),
                start_line=c.get("start_line", 0),
                end_line=c.get("end_line", 0),
            )
        # Variable
        for v in nodes.get("Variable", []):
            session.run(
                """MERGE (n:Variable {id: $id})
                   SET n.name = $name, n.file_path = $file_path, n.start_line = $start_line, n.kind = $kind""",
                id=v["id"],
                name=v.get("name", ""),
                file_path=v.get("file_path", ""),
                start_line=v.get("start_line", 0),
                kind=v.get("kind", "global"),
            )
        # Attribute（Class 成员）
        for a in nodes.get("Attribute", []):
            session.run(
                """MERGE (n:Attribute {id: $id})
                   SET n.name = $name, n.file_path = $file_path, n.start_line = $start_line,
                       n.member_of_class = $member_of_class""",
                id=a["id"],
                name=a.get("name", ""),
                file_path=a.get("file_path", ""),
                start_line=a.get("start_line", 0),
                member_of_class=a.get("member_of_class", ""),
            )
        # CONTAINS
        for from_id, to_id, _ in edges.get("CONTAINS", []):
            session.run(
                """
                MATCH (a) WHERE a.id = $from_id
                MATCH (b) WHERE b.id = $to_id
                MERGE (a)-[:CONTAINS]->(b)
                """,
                from_id=from_id,
                to_id=to_id,
            )
        # CALLS
        for from_id, to_id, _ in edges.get("CALLS", []):
            session.run(
                """
                MATCH (a:Function {id: $from_id})
                MATCH (b:Function {id: $to_id})
                MERGE (a)-[:CALLS]->(b)
                """,
                from_id=from_id,
                to_id=to_id,
            )
        # REFERENCES_VAR (Function -> Variable, 属性 lines)
        for from_id, to_id, props in edges.get("REFERENCES_VAR", []):
            lines = props.get("lines", [])
            session.run(
                """
                MATCH (a:Function {id: $from_id})
                MATCH (b:Variable {id: $to_id})
                MERGE (a)-[r:REFERENCES_VAR]->(b)
                SET r.lines = $lines
                """,
                from_id=from_id,
                to_id=to_id,
                lines=lines,
            )
        # HAS_MEMBER (Class -> Attribute)
        for from_id, to_id, _ in edges.get("HAS_MEMBER", []):
            session.run(
                """
                MATCH (a:Class {id: $from_id})
                MATCH (b:Attribute {id: $to_id})
                MERGE (a)-[:HAS_MEMBER]->(b)
                """,
                from_id=from_id,
                to_id=to_id,
            )


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


def update_repository_commit(driver, repo_id: str, commit_sha: str, database: str = NEO4J_DATABASE) -> None:
    with driver.session(database=database) as session:
        session.run(
            "MATCH (r:Repository {id: $id}) SET r.last_processed_commit = $sha",
            id=repo_id,
            sha=commit_sha,
        )
