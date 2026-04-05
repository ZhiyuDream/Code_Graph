#!/usr/bin/env python3
"""
从 GitHub API 拉取 llama.cpp 的 PR/Issue 数据，存到本地 JSON。
无需 token（公开仓库），但每小时限 60 次请求，需高效利用。

按 issue_filtering_strategy.md 设计：
  - 只拉 closed issues
  - 按 P0/P1 标签分别拉取，保证候选池够大

用法：
  python fetch_github_data.py [--issue-pages 20] [--pr-pages 3]
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import requests

# 读取 .env
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")

OWNER = "ggml-org"
REPO = "llama.cpp"
API = "https://api.github.com"
OUTPUT_DIR = Path(__file__).resolve().parent / "experiments"

# GitHub Search API 每次最多返回 1000 条
SEARCH_API = "https://api.github.com/search"

_HEADERS = {}
if GITHUB_TOKEN:
    _HEADERS["Authorization"] = f"token {GITHUB_TOKEN}"


def _get(url: str) -> list | dict | None:
    resp = requests.get(url, headers=_HEADERS, timeout=30)
    if resp.status_code == 403:
        print(f"  Rate limited! Remaining: {resp.headers.get('X-RateLimit-Remaining', '?')}")
        return None
    if resp.status_code != 200:
        print(f"  HTTP {resp.status_code}: {resp.text[:100]}")
        return None
    remaining = resp.headers.get("X-RateLimit-Remaining", "?")
    print(f"  [rate remaining: {remaining}]")
    return resp.json()


# ---------------------------------------------------------------------------
# P0/P1 标签列表（来自 design/issue_filtering_strategy.md）
# ---------------------------------------------------------------------------
P0_LABELS = {
    "bug", "bugfix", "regression", "performance",
    "critical severity", "high severity", "high priority"
}
P1_LABELS = {
    # GPU/加速器 backend
    "CUDA", "Nvidia GPU", "AMD GPU", "AMD ZenDNN", "Intel GPU", "Vulkan", "SYCL",
    "WebGPU", "RoCM", "OpenCL", "Kompute",
    "Qualcomm NPU", "Qualcomm QNN", "Hexagon", "Ascend NPU", "OpenVINO",
    # 平台
    "android", "Apple Metal", "Riscv",
    # 服务端/模块
    "server", "server/api", "server/webui",
    "ggml", "llava", "model", "build", "CI / packaging",
    "grammar", "tool calling",
}
P2_LABELS = {
    "scheduler", "threading", "chat parser", "jinja parser",
    "low severity", "medium severity", "breaking change", "rpc",
}
P3_LABELS = {
    "stale", "duplicate", "invalid", "wontfix",
    "need more info", "need feedback", "good first issue", "help wanted",
    "demo", "vibe-coded", "obsolete?", "merge ready", "roadmap",
}


def fetch_by_label(label: str, max_items: int = 500) -> list[dict]:
    """用 Search API 按标签拉 closed issues，按 comments 排序。"""
    # q=label:xxx+is:issue+state:closed 按 comments 排序
    url = (
        f"{SEARCH_API}/issues"
        f"?q=repo:{OWNER}/{REPO}+label:{label}+is:issue+state:closed"
        f"&sort=comments&order=desc&per_page=100"
    )
    all_items = []
    page = 1
    while len(all_items) < max_items:
        print(f"  [{label}] 第 {page} 页 (累计 {len(all_items)})...")
        data = _get(f"{url}&page={page}")
        if not data or "items" not in data:
            break
        items = data["items"]
        if not items:
            break
        # 排除 PR
        pure_issues = [i for i in items if "pull_request" not in i]
        all_items.extend(pure_issues)
        print(f"    本页 {len(items)} 条，纯 Issue {len(pure_issues)} 条")
        if len(items) < 100:
            break
        page += 1
        time.sleep(1)
    return all_items[:max_items]


def fetch_prs(pages: int = 3) -> list[dict]:
    """拉取 merged PRs，按 updated 排序。"""
    all_prs = []
    for page in range(1, pages + 1):
        print(f"拉取 PRs 第 {page} 页...")
        data = _get(
            f"{API}/repos/{OWNER}/{REPO}/pulls"
            f"?state=closed&sort=updated&direction=desc&per_page=100&page={page}"
        )
        if not data:
            break
        merged = [p for p in data if p.get("merged_at")]
        all_prs.extend(merged)
        print(f"  本页 {len(data)} 条，其中 merged {len(merged)} 条")
        if len(data) < 100:
            break
        time.sleep(1)
    return all_prs


def fetch_issues_by_number(numbers: list[int]) -> list[dict]:
    """按 Issue 编号批量拉取（绕过标签搜索，用于补齐 benchmark 高编号 Issues）。"""
    results = []
    for num in numbers:
        print(f"  拉取 Issue #{num}...")
        data = _get(f"{API}/repos/{OWNER}/{REPO}/issues/{num}")
        if not data or "number" not in data:
            print(f"    Issue #{num} 不存在或无法访问")
            time.sleep(1)
            continue
        if "pull_request" in data:
            print(f"    Issue #{num} 是 PR，跳过")
            time.sleep(1)
            continue
        results.append(simplify_issue(data))
        print(f"    OK: #{num} - {data.get('title', '')[:50]}")
        time.sleep(1)
    return results


def fetch_pr_files(pr_number: int) -> list[str]:
    """拉取单个 PR 的变更文件列表。"""
    data = _get(f"{API}/repos/{OWNER}/{REPO}/pulls/{pr_number}/files?per_page=100")
    if not data or not isinstance(data, list):
        return []
    return [f["filename"] for f in data]


def simplify_pr(pr: dict) -> dict:
    """精简 PR 数据。"""
    return {
        "number": pr["number"],
        "title": pr["title"],
        "body": (pr.get("body") or "")[:2000],
        "labels": [l["name"] for l in pr.get("labels", [])],
        "merged_at": pr.get("merged_at"),
        "comments": pr.get("comments", 0),
        "user": pr.get("user", {}).get("login", ""),
    }


def simplify_issue(issue: dict) -> dict:
    """精简 Issue 数据（包含 created_at 用于过滤）。"""
    return {
        "number": issue["number"],
        "title": issue["title"],
        "body": (issue.get("body") or "")[:2000],
        "labels": [l["name"] for l in issue.get("labels", [])],
        "comments": issue.get("comments", 0),
        "user": issue.get("user", {}).get("login", ""),
        "state_reason": issue.get("state_reason", ""),
        "created_at": issue.get("created_at", ""),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue-pages", type=int, default=5,
                        help="每个 P0/P1 标签拉取页数，每页100条（默认5）")
    parser.add_argument("--pr-pages", type=int, default=3, help="PR 页数（默认3）")
    parser.add_argument("--pr-files-limit", type=int, default=30,
                        help="为前 N 个 PR 拉取变更文件（默认30）")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 1. 拉 P0/P1 标签的 closed issues（Search API）
    all_issues_raw = {}
    for label in sorted(P0_LABELS | P1_LABELS | P2_LABELS):
        items = fetch_by_label(label, max_items=args.issue_pages * 100)
        for item in items:
            num = item["number"]
            if num not in all_issues_raw:
                all_issues_raw[num] = item
        print(f"  [{label}] 新增 {len(items)} 条，去重后累计 {len(all_issues_raw)} 条")
        time.sleep(1)

    issues = [simplify_issue(i) for i in all_issues_raw.values()]
    print(f"\n共获取 {len(issues)} 个去重 closed Issue")

    # 2. 拉 PRs
    prs_raw = fetch_prs(args.pr_pages)
    prs = [simplify_pr(p) for p in prs_raw]
    print(f"共获取 {len(prs)} 个 merged PR")

    # 3. 为 top PRs 拉变更文件
    prs_sorted = sorted(prs, key=lambda x: x["comments"], reverse=True)
    print(f"\n为 top {args.pr_files_limit} 个 PR 拉取变更文件...")
    for i, pr in enumerate(prs_sorted[:args.pr_files_limit]):
        files = fetch_pr_files(pr["number"])
        pr["changed_files"] = files
        print(f"  [{i+1}/{args.pr_files_limit}] PR #{pr['number']}: {len(files)} files - {pr['title'][:50]}")
        time.sleep(1)

    # 4. 保存
    output = {
        "prs": prs,
        "issues": issues,
        "fetch_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "fetch_args": {
            "issue_pages_per_label": args.issue_pages,
            "pr_pages": args.pr_pages,
            "pr_files_limit": args.pr_files_limit,
        },
    }
    out_path = OUTPUT_DIR / "github_pr_issue_data.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n数据已保存: {out_path}")
    print(f"  PRs: {len(prs)} 条（其中 {sum(1 for p in prs if 'changed_files' in p)} 条有变更文件）")
    print(f"  Issues: {len(issues)} 条（去重后）")


if __name__ == "__main__":
    main()
