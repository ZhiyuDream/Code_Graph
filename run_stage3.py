#!/usr/bin/env python3
"""
阶段 3：Issue/PR 采集与入库。
使用 .env 中的 GITHUB_TOKEN 调用 GitHub API，拉取当前仓库的 Issue 与 Pull Request，
写入 Neo4j（Issue、PullRequest 节点；PR 含 changed_paths；FIXES 边：PR -> Issue）。

前置：.env 中配置 GITHUB_TOKEN；GITHUB_REPO（owner/repo）或 REPO_ROOT 为 GitHub 仓库 clone，以便推导 repo。
"""
from __future__ import annotations

import sys
from pathlib import Path

_CODE_GRAPH = Path(__file__).resolve().parent
if str(_CODE_GRAPH) not in sys.path:
    sys.path.insert(0, str(_CODE_GRAPH))

from config import NEO4J_DATABASE, get_github_repo
from github_fetcher import fetch_all
from issue_pr_writer import clear_issues_and_pulls, ensure_issue_pr_constraints, write_issues, write_pulls_and_fixes
from neo4j_writer import get_driver


def main() -> int:
    print("阶段 3：Issue/PR 采集与入库。")
    repo_id = get_github_repo()
    if not repo_id or "/" not in repo_id:
        print("未配置 GITHUB_REPO，且无法从 REPO_ROOT 的 git remote 推导。请设置 GITHUB_REPO=owner/repo 或确保 REPO_ROOT 指向 GitHub 仓库。")
        return 1

    print(f"仓库: {repo_id}")
    print("拉取 Issue 与 PR…")
    issues, pulls = fetch_all()
    print(f"  Issue 数: {len(issues)}, PR 数: {len(pulls)}")
    if not issues and not pulls:
        print("未拉取到任何 Issue/PR，请检查 GITHUB_TOKEN 与仓库权限。")
        return 0

    driver = get_driver()
    try:
        driver.verify_connectivity()
    except Exception as e:
        print(f"Neo4j 连接失败: {e}")
        return 1

    ensure_issue_pr_constraints(driver, database=NEO4J_DATABASE)
    print("清空已有 Issue/PullRequest…")
    clear_issues_and_pulls(driver, database=NEO4J_DATABASE)
    if issues:
        print("写入 Issue…")
        write_issues(driver, issues, database=NEO4J_DATABASE)
    if pulls:
        print("写入 PullRequest 与 FIXES 边…")
        write_pulls_and_fixes(driver, pulls, repo_id, database=NEO4J_DATABASE)
    driver.close()
    print("阶段 3 完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
