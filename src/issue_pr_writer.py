"""
阶段 3：将 GitHub Issue 与 Pull Request 写入 Neo4j。
节点：Issue、PullRequest。边：FIXES（PullRequest -> Issue，当 PR body 含 fixes #n 时）。
PR 的 changed_paths 存为节点属性，查询「该 PR 涉及哪些函数」时用 Function.file_path 与 changed_paths 匹配。
"""
from __future__ import annotations

from typing import Any

from neo4j import GraphDatabase

from config import NEO4J_DATABASE


def ensure_issue_pr_constraints(driver: GraphDatabase.driver, database: str = NEO4J_DATABASE) -> None:
    """创建 Issue、PullRequest 的 id 唯一约束。"""
    with driver.session(database=database) as s:
        for label in ("Issue", "PullRequest"):
            try:
                s.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.id IS UNIQUE")
            except Exception:
                pass


def clear_issues_and_pulls(driver: GraphDatabase.driver, database: str = NEO4J_DATABASE) -> None:
    """删除所有 Issue、PullRequest 节点及其 FIXES 边。"""
    with driver.session(database=database) as s:
        s.run("MATCH (n:Issue) DETACH DELETE n")
        s.run("MATCH (n:PullRequest) DETACH DELETE n")


def write_issues(driver: GraphDatabase.driver, issues: list[dict[str, Any]], database: str = NEO4J_DATABASE) -> None:
    """写入 Issue 节点。issues 每项含 id, number, title, body, state, created_at, closed_at, user, html_url。"""
    with driver.session(database=database) as session:
        for i in issues:
            session.run(
                """
                MERGE (n:Issue {id: $id})
                SET n.number = $number, n.title = $title, n.body = $body,
                    n.state = $state, n.created_at = $created_at, n.closed_at = $closed_at,
                    n.user = $user, n.html_url = $html_url
                """,
                id=i["id"],
                number=i.get("number", 0),
                title=i.get("title", "")[:4096],
                body=(i.get("body") or "")[:65535],
                state=i.get("state", ""),
                created_at=i.get("created_at", ""),
                closed_at=i.get("closed_at", ""),
                user=i.get("user", ""),
                html_url=i.get("html_url", ""),
            )


def write_pulls_and_fixes(
    driver: GraphDatabase.driver,
    pulls: list[dict[str, Any]],
    repo_id: str,
    database: str = NEO4J_DATABASE,
) -> None:
    """
    写入 PullRequest 节点（含 changed_paths）及 FIXES 边。
    pulls 每项含 id, number, title, body, state, created_at, closed_at, user, html_url, changed_paths, fixes_issues。
    fixes_issues 为 issue 编号列表，对应 Issue 的 id 为 repo_id#i{number}。
    """
    with driver.session(database=database) as session:
        for p in pulls:
            session.run(
                """
                MERGE (n:PullRequest {id: $id})
                SET n.number = $number, n.title = $title, n.body = $body,
                    n.state = $state, n.created_at = $created_at, n.closed_at = $closed_at,
                    n.user = $user, n.html_url = $html_url, n.changed_paths = $changed_paths
                """,
                id=p["id"],
                number=p.get("number", 0),
                title=(p.get("title") or "")[:4096],
                body=(p.get("body") or "")[:65535],
                state=p.get("state", ""),
                created_at=p.get("created_at", ""),
                closed_at=p.get("closed_at", ""),
                user=p.get("user", ""),
                html_url=p.get("html_url", ""),
                changed_paths=p.get("changed_paths") or [],
            )
            for issue_num in p.get("fixes_issues") or []:
                issue_id = f"{repo_id}#i{issue_num}"
                session.run(
                    """
                    MATCH (pr:PullRequest {id: $pr_id}), (i:Issue {id: $issue_id})
                    MERGE (pr)-[:FIXES]->(i)
                    """,
                    pr_id=p["id"],
                    issue_id=issue_id,
                )
