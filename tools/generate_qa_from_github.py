import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT / "src"))   # 核心库
sys.path.insert(0, str(_ROOT))             # 根目录（config.py）

#!/usr/bin/env python3
"""
基于 GitHub PR/Issue + 代码图数据生成高质量 QA 题目。
PR/Issue 代表人类真实关心的问题，答案从 PR body + 图中验证。

用法：
  python generate_qa_from_github.py [--count 100] [--output PATH]
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import re
from collections import Counter, defaultdict
from pathlib import Path

from config import NEO4J_DATABASE
from neo4j_writer import get_driver

random.seed(42)

DATA_PATH = Path(__file__).resolve().parent / "experiments" / "github_pr_issue_data.json"
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "llama_cpp_QA_v2.csv"

INTENTS = [
    "Architecture exploration",
    "Concept / Definition",
    "Dependency tracing",
    "Design rationale",
    "Purpose Exploration",
    "Performance",
    "Data / Control-flow",
    "Feature Location",
    "Identifier Location",
    "System Design",
    "Algorithm Implementation",
    "API / Framework Support",
]

# PR 标签 -> 适合的意图类型映射
LABEL_INTENT_MAP = {
    "ggml": ["Algorithm Implementation", "Architecture exploration", "Performance"],
    "server": ["API / Framework Support", "System Design", "Feature Location"],
    "examples": ["API / Framework Support", "Feature Location"],
    "model": ["Algorithm Implementation", "Concept / Definition"],
    "testing": ["Data / Control-flow", "Dependency tracing"],
    "documentation": ["Concept / Definition", "Purpose Exploration"],
    "Nvidia GPU": ["Performance", "Algorithm Implementation"],
    "Vulkan": ["Performance", "Algorithm Implementation"],
    "Apple Metal": ["Performance", "Algorithm Implementation"],
    "python": ["API / Framework Support", "Feature Location"],
    "build": ["System Design", "Dependency tracing"],
    "devops": ["System Design", "Dependency tracing"],
}


def _load_github_data() -> dict:
    with open(DATA_PATH, encoding="utf-8") as f:
        return json.load(f)


def _query_functions_in_files(driver, file_paths: list[str], database: str) -> dict[str, list[str]]:
    """查询文件中包含的函数名。"""
    if not file_paths:
        return {}
    with driver.session(database=database) as s:
        r = s.run("""
            MATCH (f:Function)
            WHERE f.file_path IN $paths
            RETURN f.file_path AS file, collect(f.name) AS funcs
        """, paths=file_paths)
        return {rec["file"]: rec["funcs"] for rec in r}


def _query_callers_of(driver, func_names: list[str], database: str) -> list[tuple[str, str]]:
    """查询函数的 caller。"""
    if not func_names:
        return []
    with driver.session(database=database) as s:
        r = s.run("""
            MATCH (caller:Function)-[:CALLS]->(callee:Function)
            WHERE callee.name IN $names
            RETURN caller.name AS caller, callee.name AS callee
            LIMIT 30
        """, names=func_names)
        return [(rec["caller"], rec["callee"]) for rec in r]


def _query_annotation(driver, func_name: str, database: str) -> str:
    """查询函数注释摘要。"""
    with driver.session(database=database) as s:
        r = s.run("""
            MATCH (f:Function {name: $name})
            WHERE f.annotation_json IS NOT NULL
            RETURN f.annotation_json AS ann
        """, name=func_name)
        rec = r.single()
        if not rec:
            return ""
        try:
            ann = json.loads(rec["ann"]) if isinstance(rec["ann"], str) else rec["ann"]
            return ann.get("summary", "")
        except Exception:
            return ""


def _extract_func_names_from_text(text: str) -> list[str]:
    """从 PR body 中提取可能的函数名。"""
    # 匹配 backtick 包裹的标识符
    backtick = re.findall(r'`([a-zA-Z_][a-zA-Z0-9_]{2,})`', text)
    # 匹配 xxx() 形式
    func_call = re.findall(r'\b([a-zA-Z_][a-zA-Z0-9_]{2,})\s*\(', text)
    return list(set(backtick + func_call))


def _classify_pr(pr: dict) -> str:
    """根据 PR 标签和标题推断最佳意图类型。"""
    labels = pr.get("labels", [])
    title = pr.get("title", "").lower()

    # 标签匹配
    for label in labels:
        if label in LABEL_INTENT_MAP:
            return random.choice(LABEL_INTENT_MAP[label])

    # 标题关键词匹配
    if any(w in title for w in ["fix", "bug", "crash", "error", "oob", "overflow"]):
        return "Data / Control-flow"
    if any(w in title for w in ["perf", "optim", "speed", "fast"]):
        return "Performance"
    if any(w in title for w in ["add", "support", "implement", "new"]):
        return "Feature Location"
    if any(w in title for w in ["refactor", "clean", "rename", "move"]):
        return "Architecture exploration"
    if any(w in title for w in ["doc", "readme", "comment"]):
        return "Concept / Definition"
    if any(w in title for w in ["api", "endpoint", "server"]):
        return "API / Framework Support"
    if any(w in title for w in ["depend", "build", "cmake"]):
        return "Dependency tracing"

    return random.choice(INTENTS)


def _make_row(cat1, cat2, qtype, intent, entity, question, answer, evidence):
    return {
        "一级分类": cat1,
        "二级分类": cat2,
        "问题类型": qtype,
        "意图": intent,
        "实体名称": entity,
        "具体问题": question,
        "答案": answer,
        "Evidence": evidence,
    }


def _generate_pr_questions(prs: list[dict], driver, database: str) -> list[dict]:
    """基于 PR 生成题目。"""
    questions = []

    for pr in prs:
        title = pr["title"]
        body = pr["body"]
        number = pr["number"]
        labels = pr["labels"]
        changed_files = pr.get("changed_files", [])
        intent = _classify_pr(pr)

        if not body or len(body) < 50:
            continue

        # 从 body 提取函数名
        mentioned_funcs = _extract_func_names_from_text(body)

        # 查图中对应文件的函数
        file_funcs = {}
        if changed_files:
            file_funcs = _query_functions_in_files(driver, changed_files, database)

        all_funcs_in_files = []
        for funcs in file_funcs.values():
            all_funcs_in_files.extend(funcs)

        evidence = ", ".join(changed_files) if changed_files else ""

        # 构造实体名
        entity = changed_files[0].split("/")[-1] if changed_files else title.split(":")[0].strip()

        # 根据 PR 类型生成不同问题
        title_lower = title.lower()

        # Bug fix PR -> Data/Control-flow 或 Design rationale
        if any(w in title_lower for w in ["fix", "bug", "crash", "oob", "overflow", "underflow", "deadlock"]):
            # 问题1：这个 bug 是什么
            answer = f"PR #{number} ({title}) 修复了以下问题：{body[:500]}"
            if all_funcs_in_files:
                answer += f"\n涉及的函数：{', '.join(all_funcs_in_files[:10])}"
            questions.append(_make_row(
                "函数级", "Function", "what", "Data / Control-flow",
                entity,
                f"PR #{number} 修复了什么 bug？涉及哪些函数和文件？根本原因是什么？",
                answer, evidence,
            ))

            # 问题2：为什么会出现这个问题
            if len(body) > 100:
                questions.append(_make_row(
                    "函数级", "Function", "why", "Design rationale",
                    entity,
                    f"为什么 {entity} 中会出现 PR #{number} 描述的问题（{title}）？这反映了什么设计缺陷？",
                    f"根据 PR #{number} 的描述：{body[:500]}",
                    evidence,
                ))

        # Feature/Support PR -> Feature Location 或 Algorithm Implementation
        elif any(w in title_lower for w in ["add", "support", "implement", "new", "enable"]):
            answer = f"PR #{number} ({title}) 新增了以下功能：{body[:500]}"
            if changed_files:
                answer += f"\n修改的文件：{', '.join(changed_files[:5])}"
            if all_funcs_in_files:
                answer += f"\n涉及的函数：{', '.join(all_funcs_in_files[:10])}"
            questions.append(_make_row(
                "文件级", "File", "how", "Feature Location",
                entity,
                f"PR #{number}（{title}）新增的功能是如何实现的？修改了哪些文件和函数？",
                answer, evidence,
            ))

            if all_funcs_in_files:
                questions.append(_make_row(
                    "函数级", "Function", "how", "Algorithm Implementation",
                    entity,
                    f"在实现 {title} 时，具体修改了哪些函数？每个函数的改动目的是什么？",
                    f"PR #{number} 修改了以下函数：{', '.join(all_funcs_in_files[:10])}。{body[:300]}",
                    evidence,
                ))

        # Performance PR
        elif any(w in title_lower for w in ["perf", "optim", "speed", "fast", "rdna", "bf16", "fp16"]):
            questions.append(_make_row(
                "函数级", "Function", "how", "Performance",
                entity,
                f"PR #{number}（{title}）做了什么性能优化？优化的原理和效果是什么？",
                f"PR #{number} 的优化内容：{body[:500]}",
                evidence,
            ))

        # Refactor PR -> Architecture exploration
        elif any(w in title_lower for w in ["refactor", "clean", "rework", "reorganize"]):
            questions.append(_make_row(
                "文件级", "File", "why", "Architecture exploration",
                entity,
                f"PR #{number}（{title}）为什么要重构？重构前后的架构有什么变化？",
                f"PR #{number} 的重构说明：{body[:500]}",
                evidence,
            ))

        # 通用：任何有 changed_files 的 PR 都可以出依赖追踪题
        if changed_files and len(changed_files) >= 2:
            questions.append(_make_row(
                "文件级", "File", "what", "Dependency tracing",
                entity,
                f"PR #{number}（{title}）同时修改了 {len(changed_files)} 个文件，这些文件之间有什么依赖关系？为什么需要一起修改？",
                f"PR #{number} 修改了：{', '.join(changed_files[:10])}。{body[:300]}",
                evidence,
            ))

        # 有函数的 PR 可以出 caller 追踪题
        if all_funcs_in_files:
            callers = _query_callers_of(driver, all_funcs_in_files[:5], database)
            if callers:
                caller_str = "; ".join(f"{c[0]}->{c[1]}" for c in callers[:10])
                questions.append(_make_row(
                    "函数级", "Function", "what", "Purpose Exploration",
                    all_funcs_in_files[0],
                    f"PR #{number} 修改的函数 {all_funcs_in_files[0]} 被哪些其他函数调用？修改它会影响哪些上游功能？",
                    f"调用关系：{caller_str}。PR 描述：{body[:200]}",
                    evidence,
                ))

    return questions


def _generate_issue_questions(issues: list[dict]) -> list[dict]:
    """基于 Issue 生成题目。"""
    questions = []

    for issue in issues:
        title = issue["title"]
        body = issue["body"]
        number = issue["number"]

        if not body or len(body) < 50:
            continue

        title_lower = title.lower()

        # Feature Request
        if "feature" in title_lower or "request" in title_lower or "support" in title_lower:
            questions.append(_make_row(
                "项目级", "Project", "how", "System Design",
                title.split(":")[0].strip() if ":" in title else title[:30],
                f"Issue #{number}（{title}）提出的功能需求如何在现有架构中实现？需要修改哪些模块？",
                f"Issue #{number} 的需求描述：{body[:500]}",
                "",
            ))

        # Bug report
        elif any(w in title_lower for w in ["bug", "error", "crash", "fail", "broken"]):
            questions.append(_make_row(
                "项目级", "Project", "why", "Data / Control-flow",
                title.split(":")[0].strip() if ":" in title else title[:30],
                f"Issue #{number}（{title}）报告的问题可能涉及哪些代码路径？如何定位根本原因？",
                f"Issue #{number} 的问题描述：{body[:500]}",
                "",
            ))

        # 通用
        else:
            questions.append(_make_row(
                "项目级", "Project", "what", "Concept / Definition",
                title.split(":")[0].strip() if ":" in title else title[:30],
                f"Issue #{number}（{title}）讨论的核心问题是什么？涉及哪些技术概念？",
                f"Issue #{number} 的描述：{body[:500]}",
                "",
            ))

    return questions


def main():
    parser = argparse.ArgumentParser(description="基于 GitHub PR/Issue 生成高质量 QA")
    parser.add_argument("--count", type=int, default=100, help="目标题数")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    if not DATA_PATH.exists():
        print(f"请先运行 fetch_github_data.py 拉取数据: {DATA_PATH}")
        return 1

    data = _load_github_data()
    prs = data["prs"]
    issues = data["issues"]
    print(f"加载 {len(prs)} 个 PR, {len(issues)} 个 Issue")

    driver = get_driver()
    try:
        driver.verify_connectivity()
        print("从 PR 生成题目...")
        pr_questions = _generate_pr_questions(prs, driver, NEO4J_DATABASE)
        print(f"  PR 题目: {len(pr_questions)} 道")

        print("从 Issue 生成题目...")
        issue_questions = _generate_issue_questions(issues)
        print(f"  Issue 题目: {len(issue_questions)} 道")
    finally:
        driver.close()

    all_questions = pr_questions + issue_questions
    random.shuffle(all_questions)

    # 按意图类型均衡采样
    by_intent = defaultdict(list)
    for q in all_questions:
        by_intent[q["意图"]].append(q)

    per_intent = max(args.count // 12, 1)
    selected = []
    for intent in INTENTS:
        pool = by_intent.get(intent, [])
        k = min(per_intent, len(pool))
        selected.extend(random.sample(pool, k) if k > 0 else [])

    # 补齐
    remaining = [q for q in all_questions if q not in selected]
    need = args.count - len(selected)
    if need > 0 and remaining:
        selected.extend(random.sample(remaining, min(need, len(remaining))))

    selected = selected[:args.count]

    # 统计
    intent_counts = Counter(q["意图"] for q in selected)
    print(f"\n最终选取: {len(selected)} 题")
    for intent in INTENTS:
        print(f"  {intent}: {intent_counts.get(intent, 0)} 题")

    qtype_counts = Counter(q["问题类型"] for q in selected)
    print(f"\n问题类型分布: {dict(qtype_counts)}")

    # 写 CSV
    fieldnames = ["一级分类", "二级分类", "问题类型", "意图", "实体名称", "具体问题", "答案", "Evidence"]
    with open(args.output, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(selected)
    print(f"\n题目已写入: {args.output}")


if __name__ == "__main__":
    main()
