#!/usr/bin/env python3
"""
将本地已拉取的 PR/Issue 数据写入 Neo4j 图。
按 design/issue_filtering_strategy.md 过滤入库：
  Step 1: 基础过滤（state=open, P3标签, <2022, 无代码引用）
  Step 2: P1 条件确认（commit/file path + comments>=1）
  Step 3: TF-IDF 相似度去重（>0.85 跳过）
  Step 4: 计算 ranking_score 后入库

入库节点额外字段：
  Issue.ranking_score: 0.0~1.0，反映代码证据质量

用法：
  python import_github_to_graph.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from collections import Counter

from config import NEO4J_DATABASE
from neo4j_writer import get_driver
from issue_pr_writer import ensure_issue_pr_constraints

DATA_PATH = Path(__file__).resolve().parent / "experiments" / "github_pr_issue_data.json"
REPO_ID = "ggml-org/llama.cpp"

# ---------------------------------------------------------------------------
# 标签分类（来自 design/issue_filtering_strategy.md）
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

# ---------------------------------------------------------------------------
# 代码引用检测正则
# ---------------------------------------------------------------------------
RE_COMMIT_SHA = re.compile(r'\b[0-9a-f]{7,40}\b')
RE_FILE_PATH = re.compile(r'\b[\w/.-]+\.(c|cpp|h|cc|cxx|hpp)(\b|:\d+)', re.IGNORECASE)
RE_FUNC_CALL = re.compile(r'\b([a-zA-Z_][a-zA-Z0-9_]{2,30})\s*\(', re.MULTILINE)
RE_TITLE_WORDS = re.compile(r'\b\w+\b')


def _label_class(labels: list[str]) -> tuple[str, float]:
    """返回最高标签等级(P0/P1/P2/P3)和label_weight。"""
    label_set = set(labels)
    if label_set & P0_LABELS:
        return "P0", 1.0
    if label_set & P1_LABELS:
        return "P1", 0.7
    return "P2", 0.3


def _has_code_reference(body: str) -> tuple[bool, bool, bool]:
    """返回 (has_commit, has_file, has_func)。"""
    has_commit = bool(RE_COMMIT_SHA.search(body)) if body else False
    has_file = bool(RE_FILE_PATH.search(body)) if body else False
    has_func = bool(RE_FUNC_CALL.search(body)) if body else False
    return has_commit, has_file, has_func


def _title_token_set(title: str) -> set[str]:
    """返回标题的词集合（去停用词）。"""
    STOPWORDS = {"a", "an", "the", "in", "on", "at", "to", "for", "of", "and", "or", "but", "is", "are", "was", "were", "be", "with", "by", "from"}
    words = RE_TITLE_WORDS.findall(title.lower())
    return {w for w in words if w not in STOPWORDS and len(w) > 2}


def _tfidf_similarity(title1: str, title2: str) -> float:
    """简化的 TF-IDF 相似度（Jaccard on word sets）。"""
    set1 = _title_token_set(title1)
    set2 = _title_token_set(title2)
    if not set1 or not set2:
        return 0.0
    intersection = len(set1 & set2)
    union = len(set1 | set2)
    return intersection / union if union > 0 else 0.0


def _compute_ranking_score(issue: dict, label_class: str, has_commit: bool, has_file: bool, has_func: bool) -> float:
    """
    按 design/issue_filtering_strategy.md 计算 ranking_score。
    score = label_weight × age_penalty × staleness_penalty × code_signal_bonus
    """
    labels = set(issue.get("labels", []))
    created_at = issue.get("created_at", "")

    # label_weight
    lw = {"P0": 1.0, "P1": 0.7, "P2": 0.3}[label_class]

    # age_penalty
    if created_at and created_at >= "2024-01-01":
        age_penalty = 1.0
    elif created_at and created_at >= "2023-01-01":
        age_penalty = 0.7
    elif created_at:
        age_penalty = 0.3
    else:
        age_penalty = 0.5

    # staleness_penalty
    staleness_penalty = 0.2 if "stale" in labels else 1.0

    # code_signal_bonus（commit SHA 最强）
    code_bonus = 0.0
    if has_commit:
        code_bonus = 0.3
    elif has_file:
        code_bonus = 0.15
    if has_func:
        code_bonus += 0.1

    score = lw * age_penalty * staleness_penalty * (1.0 + code_bonus)
    return min(score, 1.0)


def _filter_issues(issues: list[dict]) -> tuple[list[dict], dict]:
    """
    按 design/issue_filtering_strategy.md 过滤 Issue。
    返回 (filtered_issues, stats_dict)。
    """
    # Step 1+2: 逐条过滤，同时记录原因
    candidates = []
    skip_stats = Counter()

    for issue in issues:
        labels = set(issue.get("labels", []))
        body = issue.get("body") or ""
        created_at = issue.get("created_at", "")
        comments = issue.get("comments", 0)
        state_reason = issue.get("state_reason", "")

        # Step 1a: 跳过 open
        if state_reason not in ("completed", "not_planned", ""):
            skip_stats["not_closed"] += 1
            continue

        # Step 1b: 跳过 P3 标签
        if labels & P3_LABELS:
            skip_stats["P3_label"] += 1
            continue

        # Step 1c: 跳过 2022 以前
        if created_at and created_at < "2022-01-01":
            skip_stats["too_old"] += 1
            continue

        # 代码引用检测
        has_commit, has_file, has_func = _has_code_reference(body)
        tier, lw = _label_class(issue.get("labels", []))

        # Step 1d: P0 须含 commit 或 file
        if tier == "P0":
            if not (has_commit or has_file):
                skip_stats["P0_no_code_ref"] += 1
                continue

        # Step 2: P1 须 (commit 或 file) + comments >= 1
        if tier == "P1":
            if not (has_commit or has_file):
                skip_stats["P1_no_code_ref"] += 1
                continue
            if comments < 1:
                skip_stats["P1_no_comments"] += 1
                continue

        # P2: 降分但不禁入（按现有逻辑）

        candidates.append(issue)

    # Step 3: TF-IDF 去重（>0.85 跳过）
    # 按 comments 降序排列，保留信息量更大的
    candidates.sort(key=lambda x: x.get("comments", 0), reverse=True)
    seen_titles = []  # (title_words, issue)
    filtered = []
    dup_count = 0

    for issue in candidates:
        title = issue.get("title", "")
        title_set = _title_token_set(title)
        is_dup = False
        dup_idx = None
        for idx, (seen_token_set, seen_issue) in enumerate(seen_titles):
            if _tfidf_similarity(title, seen_issue.get("title", "")) > 0.85:
                dup_idx = idx
                # 但如果当前 issue 的 comments 显著更多（2x），则替换
                if issue.get("comments", 0) > seen_issue.get("comments", 0) * 2:
                    filtered.pop(dup_idx)  # 移除被替代的
                    seen_titles.pop(dup_idx)
                else:
                    is_dup = True
                    dup_count += 1
                break
        if not is_dup:
            filtered.append(issue)
            seen_titles.append((title_set, issue))

    skip_stats["duplicate"] = dup_count
    skip_stats["passed"] = len(filtered)

    return filtered, dict(skip_stats)


def _extract_fixes(body: str) -> list[int]:
    """从 PR body 中提取 fixes #N / closes #N 的 issue 编号。"""
    if not body:
        return []
    pattern = r'(?:fix(?:es)?|close[sd]?|resolve[sd]?)\s+#(\d+)'
    return [int(m) for m in re.findall(pattern, body, re.IGNORECASE)]


def _extract_func_names(text: str) -> set[str]:
    """从文本中提取可能的函数名（backtick 包裹或 xxx() 形式）。"""
    backtick = set(re.findall(r'`([a-zA-Z_][a-zA-Z0-9_]{3,})`', text))
    func_call = set(re.findall(r'\b([a-zA-Z_][a-zA-Z0-9_]{3,})\s*\(', text))
    return backtick | func_call


def import_data(driver, database: str):
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    prs = data["prs"]
    raw_issues = data["issues"]
    print(f"加载 {len(prs)} 个 PR, {len(raw_issues)} 个原始 Issue")

    # 确保约束
    ensure_issue_pr_constraints(driver, database)

    # 额外约束
    with driver.session(database=database) as s:
        try:
            s.run("CREATE CONSTRAINT IF NOT EXISTS FOR (n:Issue) REQUIRE n.number IS UNIQUE")
            s.run("CREATE CONSTRAINT IF NOT EXISTS FOR (n:PullRequest) REQUIRE n.number IS UNIQUE")
        except Exception:
            pass

    # Step 1-3: 过滤
    print("\n=== Issue 过滤（按 design/issue_filtering_strategy.md）===")
    filtered_issues, filter_stats = _filter_issues(raw_issues)
    print("过滤统计:")
    for k, v in sorted(filter_stats.items()):
        print(f"  {k:25s}: {v}")
    print(f"过滤后: {len(filtered_issues)} 个 Issue 准备入库")

    # Step 4: 计算 ranking_score 并写入
    # 获取图中所有函数名（用于匹配 MENTIONS）
    with driver.session(database=database) as s:
        r = s.run("MATCH (f:Function) RETURN f.name AS name")
        all_func_names = {rec["name"] for rec in r}
    print(f"图中函数名: {len(all_func_names)} 个")

    # 写入 Issue 节点（含 ranking_score）
    print("\n写入 Issue 节点...")
    with driver.session(database=database) as s:
        for issue in filtered_issues:
            labels = issue.get("labels", [])
            tier, lw = _label_class(labels)
            body = issue.get("body") or ""
            has_commit, has_file, has_func = _has_code_reference(body)
            ranking_score = _compute_ranking_score(issue, tier, has_commit, has_file, has_func)

            s.run("""
                MERGE (n:Issue {number: $number})
                SET n.title = $title, n.body = $body,
                    n.labels = $labels, n.comments = $comments,
                    n.user = $user, n.state_reason = $state_reason,
                    n.created_at = $created_at,
                    n.tier = $tier,
                    n.ranking_score = $ranking_score
            """,
                number=issue["number"],
                title=issue["title"][:4096],
                body=body[:8000],
                labels=labels,
                comments=issue.get("comments", 0),
                user=issue.get("user", ""),
                state_reason=issue.get("state_reason", ""),
                created_at=issue.get("created_at", ""),
                tier=tier,
                ranking_score=round(ranking_score, 4),
            )
    print(f"  写入 {len(filtered_issues)} 个 Issue（含 ranking_score）")

    # 写入 PR 节点
    print("写入 PullRequest 节点...")
    pr_count = 0
    fixes_count = 0
    modifies_count = 0
    touches_count = 0
    mentions_count = 0

    with driver.session(database=database) as s:
        for pr in prs:
            changed_files = pr.get("changed_files", [])
            s.run("""
                MERGE (n:PullRequest {number: $number})
                SET n.title = $title, n.body = $body,
                    n.labels = $labels, n.comments = $comments,
                    n.user = $user, n.merged_at = $merged_at,
                    n.changed_paths = $changed_paths
            """,
                number=pr["number"],
                title=pr["title"][:4096],
                body=(pr.get("body") or "")[:8000],
                labels=pr.get("labels", []),
                comments=pr.get("comments", 0),
                user=pr.get("user", ""),
                merged_at=pr.get("merged_at", ""),
                changed_paths=changed_files,
            )
            pr_count += 1

            # FIXES 边：PR -> Issue
            fixes_nums = _extract_fixes(pr.get("body", ""))
            for issue_num in fixes_nums:
                result = s.run("""
                    MATCH (pr:PullRequest {number: $pr_num}), (i:Issue {number: $issue_num})
                    MERGE (pr)-[:FIXES]->(i)
                    RETURN count(*) AS cnt
                """, pr_num=pr["number"], issue_num=issue_num)
                if result.single()["cnt"] > 0:
                    fixes_count += 1

            # MODIFIES 边：PR -> File
            for fp in changed_files:
                result = s.run("""
                    MATCH (pr:PullRequest {number: $pr_num}), (f:File)
                    WHERE f.id ENDS WITH $fp OR f.file_path = $fp
                    MERGE (pr)-[:MODIFIES]->(f)
                    RETURN count(*) AS cnt
                """, pr_num=pr["number"], fp=fp)
                cnt = result.single()["cnt"]
                modifies_count += cnt

            # TOUCHES 边：PR -> Function（通过 changed_files 匹配）
            if changed_files:
                result = s.run("""
                    MATCH (pr:PullRequest {number: $pr_num}), (func:Function)
                    WHERE func.file_path IN $paths
                    MERGE (pr)-[:TOUCHES]->(func)
                    RETURN count(*) AS cnt
                """, pr_num=pr["number"], paths=changed_files)
                touches_count += result.single()["cnt"]

            # MENTIONS 边：PR body 中提到的函数名
            body = pr.get("body", "") or ""
            mentioned = _extract_func_names(body) & all_func_names
            if mentioned:
                result = s.run("""
                    MATCH (pr:PullRequest {number: $pr_num}), (func:Function)
                    WHERE func.name IN $names
                    MERGE (pr)-[:MENTIONS]->(func)
                    RETURN count(*) AS cnt
                """, pr_num=pr["number"], names=list(mentioned))
                mentions_count += result.single()["cnt"]

        # Issue MENTIONS 边（仅过滤后的 Issue）
        for issue in filtered_issues:
            body = issue.get("body", "") or ""
            mentioned = _extract_func_names(body) & all_func_names
            if mentioned:
                result = s.run("""
                    MATCH (i:Issue {number: $num}), (func:Function)
                    WHERE func.name IN $names
                    MERGE (i)-[:MENTIONS]->(func)
                    RETURN count(*) AS cnt
                """, num=issue["number"], names=list(mentioned))
                mentions_count += result.single()["cnt"]

    print(f"\n--- 写入统计 ---")
    print(f"PullRequest 节点: {pr_count}")
    print(f"Issue 节点: {len(filtered_issues)}")
    print(f"FIXES 边 (PR->Issue): {fixes_count}")
    print(f"MODIFIES 边 (PR->File): {modifies_count}")
    print(f"TOUCHES 边 (PR->Function): {touches_count}")
    print(f"MENTIONS 边 (PR/Issue->Function): {mentions_count}")

    # 验证
    with driver.session(database=database) as s:
        r = s.run("MATCH (n:PullRequest) RETURN count(n) AS cnt")
        print(f"\n验证 - PullRequest 节点: {r.single()['cnt']}")
        r = s.run("MATCH (n:Issue) RETURN count(n) AS cnt")
        print(f"验证 - Issue 节点: {r.single()['cnt']}")
        r = s.run("MATCH ()-[r:FIXES]->() RETURN count(r) AS cnt")
        print(f"验证 - FIXES 边: {r.single()['cnt']}")
        r = s.run("MATCH ()-[r:MODIFIES]->() RETURN count(r) AS cnt")
        print(f"验证 - MODIFIES 边: {r.single()['cnt']}")
        r = s.run("MATCH ()-[r:TOUCHES]->() RETURN count(r) AS cnt")
        print(f"验证 - TOUCHES 边: {r.single()['cnt']}")
        r = s.run("MATCH ()-[r:MENTIONS]->() RETURN count(r) AS cnt")
        print(f"验证 - MENTIONS 边: {r.single()['cnt']}")
        # Tier/ranking_score 分布
        r = s.run("MATCH (i:Issue) RETURN i.tier AS tier, count(*) AS cnt ORDER BY tier")
        print(f"验证 - Issue tier 分布: { {rec['tier']: rec['cnt'] for rec in r} }")
        r = s.run("MATCH (i:Issue) RETURN min(i.ranking_score) AS min_score, max(i.ranking_score) AS max_score, avg(i.ranking_score) AS avg_score")
        row = r.single()
        print(f"验证 - ranking_score: min={row['min_score']:.3f} max={row['max_score']:.3f} avg={row['avg_score']:.3f}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--clear-issues", action="store_true",
                        help="导入前先清除所有 Issue 节点（保留 PR）")
    args = parser.parse_args()

    driver = get_driver()
    try:
        driver.verify_connectivity()
        if args.clear_issues:
            print("清除现有 Issue 节点...")
            with driver.session(database=NEO4J_DATABASE) as s:
                s.run("MATCH (i:Issue) DETACH DELETE i")
                s.run("MATCH ()-[r:MENTIONS]->() DELETE r")
                s.run("MATCH ()-[r:FIXES]->() DELETE r")
            print("已清除")
        import_data(driver, NEO4J_DATABASE)
    finally:
        driver.close()
