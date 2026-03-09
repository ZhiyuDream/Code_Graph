"""
阶段 3：通过 GitHub API 拉取仓库的 Issue 与 Pull Request，以及 PR 的变更文件列表。
使用 .env 中的 GITHUB_TOKEN；需配置 GITHUB_REPO 或由 REPO_ROOT 的 git remote 推导。
"""
from __future__ import annotations

import re
from typing import Any

import requests

from config import GITHUB_TOKEN, get_github_repo

API_BASE = "https://api.github.com"


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }


def _get(url: str) -> list[dict[str, Any]] | dict[str, Any] | None:
    resp = requests.get(url, headers=_headers(), timeout=30)
    if resp.status_code != 200:
        return None
    return resp.json()


def _get_paginated(url: str, per_page: int = 100) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    page = 1
    while True:
        u = f"{url}&per_page={per_page}&page={page}" if "?" in url else f"{url}?per_page={per_page}&page={page}"
        data = _get(u)
        if data is None or not isinstance(data, list):
            break
        if not data:
            break
        out.extend(data)
        if len(data) < per_page:
            break
        page += 1
    return out


def fetch_issues(owner: str, repo: str) -> list[dict[str, Any]]:
    """
    拉取仓库所有 Issue（不含 PR）。GitHub API 的 /issues 会同时返回 PR，需过滤掉带 pull_request 的。
    返回 list of {number, title, body, state, created_at, closed_at, user.login, html_url}，以及 id 用 owner/repo#i{number}。
    """
    url = f"{API_BASE}/repos/{owner}/{repo}/issues?state=all"
    raw = _get_paginated(url)
    issues: list[dict[str, Any]] = []
    for item in raw:
        if item.get("pull_request") is not None:
            continue
        issues.append({
            "id": f"{owner}/{repo}#i{item['number']}",
            "number": item["number"],
            "title": item.get("title") or "",
            "body": item.get("body") or "",
            "state": item.get("state") or "open",
            "created_at": item.get("created_at") or "",
            "closed_at": item.get("closed_at") or "",
            "user": (item.get("user") or {}).get("login") or "",
            "html_url": item.get("html_url") or "",
        })
    return issues


def fetch_pulls_with_files(owner: str, repo: str) -> list[dict[str, Any]]:
    """
    拉取仓库所有 Pull Request，并为每个 PR 拉取变更文件列表（changed_paths）。
    返回 list of {id, number, title, body, state, created_at, closed_at, user, html_url, changed_paths: [path, ...], fixes_issues: [issue_number, ...]}。
    fixes_issues 从 body 中解析 "fixes #n" / "closes #n" 等。
    """
    url = f"{API_BASE}/repos/{owner}/{repo}/pulls?state=all"
    raw = _get_paginated(url)
    pulls: list[dict[str, Any]] = []
    for item in raw:
        number = item["number"]
        body = item.get("body") or ""
        fixes = _parse_fixes_from_body(body)
        files_url = f"{API_BASE}/repos/{owner}/{repo}/pulls/{number}/files"
        files_data = _get(files_url)
        changed_paths: list[str] = []
        if isinstance(files_data, list):
            for f in files_data:
                fn = f.get("filename")
                if fn:
                    changed_paths.append(fn)
        pulls.append({
            "id": f"{owner}/{repo}#p{number}",
            "number": number,
            "title": item.get("title") or "",
            "body": body,
            "state": item.get("state") or "open",
            "created_at": item.get("created_at") or "",
            "closed_at": item.get("closed_at") or "",
            "user": (item.get("user") or {}).get("login") or "",
            "html_url": item.get("html_url") or "",
            "changed_paths": changed_paths,
            "fixes_issues": fixes,
        })
    return pulls


def _parse_fixes_from_body(body: str) -> list[int]:
    """从 PR/Issue body 中解析 fixes #n / closes #n / resolve #n，返回 issue 编号列表。"""
    if not body:
        return []
    pattern = re.compile(r"(?i)(?:fixes?|closes?|resolves?)\s+#(\d+)")
    return [int(m.group(1)) for m in pattern.finditer(body)]


def fetch_all(owner: str | None = None, repo: str | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    拉取当前配置仓库的全部 Issue 与 PR（含 changed_paths、fixes_issues）。
    若未传 owner/repo，则使用 get_github_repo() 解析。
    返回 (issues, pulls)。
    """
    if owner and repo:
        repo_id = f"{owner}/{repo}"
    else:
        repo_id = get_github_repo()
        if not repo_id or "/" not in repo_id:
            return [], []
        owner, repo = repo_id.split("/", 1)
    if not GITHUB_TOKEN:
        return [], []
    issues = fetch_issues(owner, repo)
    pulls = fetch_pulls_with_files(owner, repo)
    return issues, pulls
