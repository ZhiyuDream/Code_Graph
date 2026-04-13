#!/usr/bin/env python3
"""
基于代码图真实数据自动生成高质量 QA 题目。
答案直接从图中查询得到，Evidence 为实际涉及的文件路径。

用法：
  python generate_qa.py [--count 100] [--output PATH]

相关脚本说明：
  - generate_qa_v3.py: 三类问题混合生成 (Issue驱动40% + 代码理解30% + 工作流30%)
  - generate_qa_from_github.py (已合并): 从 GitHub PR/Issue 生成问题
  - generate_qa_from_source.py (已合并): 直接从源码生成问题（不依赖图谱）
"""
from __future__ import annotations

import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT / "src"))   # 核心库
sys.path.insert(0, str(_ROOT))             # 根目录（config.py）

import argparse
import csv
import json
import random
import sys
from pathlib import Path

from config import NEO4J_DATABASE
from neo4j_writer import get_driver

DEFAULT_OUTPUT = Path(__file__).resolve().parent / "llama_cpp_QA_v2.csv"

random.seed(42)

# 12 种意图类型
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


def _fetch_materials(driver, database: str) -> dict:
    """从图中提取出题素材。"""
    mat = {}
    with driver.session(database=database) as s:
        # 枢纽函数（fan_in>0 且 fan_out>0）
        r = s.run("""
            MATCH (f:Function)
            WHERE f.fan_in > 0 AND f.fan_out > 0 AND f.file_path IS NOT NULL
            RETURN f.name AS name, f.file_path AS file,
                   f.fan_in AS fan_in, f.fan_out AS fan_out,
                   f.annotation_json AS ann
            ORDER BY f.fan_in * f.fan_out DESC LIMIT 80
        """)
        mat["hubs"] = [dict(rec) for rec in r]

        # 高 fan_in 函数
        r = s.run("""
            MATCH (f:Function)
            WHERE f.fan_in >= 5 AND f.file_path IS NOT NULL
            RETURN f.name AS name, f.file_path AS file, f.fan_in AS fan_in,
                   f.fan_out AS fan_out, f.annotation_json AS ann
            ORDER BY f.fan_in DESC LIMIT 60
        """)
        mat["high_fan_in"] = [dict(rec) for rec in r]

        # 高 fan_out 函数
        r = s.run("""
            MATCH (f:Function)
            WHERE f.fan_out >= 5 AND f.file_path IS NOT NULL
            RETURN f.name AS name, f.file_path AS file, f.fan_out AS fan_out,
                   f.fan_in AS fan_in, f.annotation_json AS ann
            ORDER BY f.fan_out DESC LIMIT 60
        """)
        mat["high_fan_out"] = [dict(rec) for rec in r]

        # 跨文件调用对
        r = s.run("""
            MATCH (a:Function)-[:CALLS]->(b:Function)
            WHERE a.file_path <> b.file_path
              AND a.file_path IS NOT NULL AND b.file_path IS NOT NULL
            RETURN a.name AS caller, a.file_path AS caller_file,
                   b.name AS callee, b.file_path AS callee_file
            LIMIT 200
        """)
        mat["cross_calls"] = [dict(rec) for rec in r]

        # 调用链 (长度 3)
        r = s.run("""
            MATCH (a:Function)-[:CALLS]->(b:Function)-[:CALLS]->(c:Function)-[:CALLS]->(d:Function)
            WHERE a.file_path IS NOT NULL AND d.file_path IS NOT NULL
            RETURN a.name AS f1, b.name AS f2, c.name AS f3, d.name AS f4,
                   a.file_path AS file1, b.file_path AS file2,
                   c.file_path AS file3, d.file_path AS file4
            LIMIT 60
        """)
        mat["chains"] = [dict(rec) for rec in r]

        # 叶子函数（被调用但不调用别人）
        r = s.run("""
            MATCH (f:Function)
            WHERE f.fan_in > 2 AND f.fan_out = 0 AND f.file_path IS NOT NULL
            RETURN f.name AS name, f.file_path AS file, f.fan_in AS fan_in,
                   f.annotation_json AS ann
            ORDER BY f.fan_in DESC LIMIT 40
        """)
        mat["leaves"] = [dict(rec) for rec in r]

        # 同文件内调用对
        r = s.run("""
            MATCH (a:Function)-[:CALLS]->(b:Function)
            WHERE a.file_path = b.file_path AND a.file_path IS NOT NULL
              AND a.name <> b.name
            RETURN a.name AS caller, b.name AS callee, a.file_path AS file
            LIMIT 150
        """)
        mat["same_file_calls"] = [dict(rec) for rec in r]

        # 文件 -> 函数数量
        r = s.run("""
            MATCH (f:Function)
            WHERE f.file_path IS NOT NULL AND f.fan_in + f.fan_out > 0
            RETURN f.file_path AS file, count(f) AS cnt,
                   collect(f.name)[..10] AS sample_funcs
            ORDER BY cnt DESC LIMIT 30
        """)
        mat["file_stats"] = [dict(rec) for rec in r]

        # 函数的 caller 列表（用于生成答案）
        r = s.run("""
            MATCH (caller:Function)-[:CALLS]->(callee:Function)
            WHERE callee.fan_in >= 3 AND callee.file_path IS NOT NULL
            RETURN callee.name AS name, collect(DISTINCT caller.name)[..10] AS callers,
                   callee.file_path AS file
            ORDER BY callee.fan_in DESC LIMIT 80
        """)
        mat["callers_map"] = [dict(rec) for rec in r]

        # 函数的 callee 列表
        r = s.run("""
            MATCH (caller:Function)-[:CALLS]->(callee:Function)
            WHERE caller.fan_out >= 3 AND caller.file_path IS NOT NULL
            RETURN caller.name AS name, collect(DISTINCT callee.name)[..10] AS callees,
                   caller.file_path AS file
            ORDER BY caller.fan_out DESC LIMIT 80
        """)
        mat["callees_map"] = [dict(rec) for rec in r]

    return mat


def _ann_summary(ann_raw) -> str:
    """从 annotation_json 提取摘要。"""
    if not ann_raw:
        return ""
    try:
        ann = json.loads(ann_raw) if isinstance(ann_raw, str) else ann_raw
        return ann.get("summary", "")
    except Exception:
        return ""


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


def generate_questions(mat: dict, target_count: int = 100) -> list[dict]:
    """基于素材生成题目，每种意图类型约 target_count/12 题。"""
    per_intent = max(target_count // 12, 1)
    extra = target_count - per_intent * 12
    questions = []
    used_entities = set()

    def _pick_unique(pool, key="name", n=1):
        candidates = [x for x in pool if x[key] not in used_entities]
        if len(candidates) < n:
            candidates = pool[:n*2]
        picked = random.sample(candidates, min(n, len(candidates)))
        for p in picked:
            used_entities.add(p[key])
        return picked

    # 1. Architecture exploration — 文件级架构问题
    for fs in mat["file_stats"][:per_intent]:
        fname = fs["file"].split("/")[-1]
        funcs_str = ", ".join(fs["sample_funcs"])
        questions.append(_make_row(
            "文件级", "File", "what", "Architecture exploration",
            fname,
            f"文件 {fs['file']} 中有哪些核心函数？它们之间的调用关系是什么？",
            f"该文件包含 {fs['cnt']} 个有调用关系的函数，其中代表性函数包括：{funcs_str}。",
            fs["file"],
        ))

    # 2. Concept / Definition — 函数定义与作用
    for f in _pick_unique(mat["hubs"], n=per_intent):
        summary = _ann_summary(f.get("ann"))
        ans = f"函数 {f['name']} 定义在 {f['file']} 中，fan_in={f['fan_in']}，fan_out={f['fan_out']}。"
        if summary:
            ans += f" 功能摘要：{summary}"
        questions.append(_make_row(
            "函数级", "Function", "what", "Concept / Definition",
            f["name"],
            f"函数 {f['name']} 的定义和作用是什么？",
            ans,
            f["file"],
        ))

    # 3. Dependency tracing — 函数依赖了哪些其他函数
    for f in mat["callees_map"][:per_intent]:
        callees_str = ", ".join(f["callees"])
        questions.append(_make_row(
            "函数级", "Function", "what", "Dependency tracing",
            f["name"],
            f"函数 {f['name']} 依赖（调用）了哪些其他函数？",
            f"函数 {f['name']}（位于 {f['file']}）调用了以下函数：{callees_str}。",
            f["file"],
        ))

    # 4. Design rationale — 为什么跨文件调用
    cross_sample = random.sample(mat["cross_calls"], min(per_intent, len(mat["cross_calls"])))
    for c in cross_sample:
        questions.append(_make_row(
            "函数级", "Function", "why", "Design rationale",
            c["caller"],
            f"为什么 {c['caller']}（{c['caller_file']}）需要调用 {c['callee']}（{c['callee_file']}）？这种跨模块依赖的设计考量是什么？",
            f"{c['caller']} 位于 {c['caller_file']}，调用了 {c['callee_file']} 中的 {c['callee']}，形成跨模块依赖。",
            f"{c['caller_file']}, {c['callee_file']}",
        ))

    # 5. Purpose Exploration — 函数在系统中的角色
    for f in _pick_unique(mat["high_fan_in"], n=per_intent):
        summary = _ann_summary(f.get("ann"))
        ans = f"{f['name']} 被 {f['fan_in']} 个函数调用，是系统中的基础设施函数。定义在 {f['file']}。"
        if summary:
            ans += f" {summary}"
        questions.append(_make_row(
            "函数级", "Function", "why", "Purpose Exploration",
            f["name"],
            f"函数 {f['name']} 在整个系统中扮演什么角色？为什么有这么多函数依赖它？",
            ans,
            f["file"],
        ))

    # 6. Performance — 高复杂度函数
    for f in mat["high_fan_out"][:per_intent]:
        questions.append(_make_row(
            "函数级", "Function", "how", "Performance",
            f["name"],
            f"函数 {f['name']} 调用了 {f['fan_out']} 个其他函数，这种高 fan_out 对性能有什么影响？如何优化？",
            f"{f['name']}（{f['file']}）fan_out={f['fan_out']}，是系统中逻辑最复杂的函数之一。高 fan_out 意味着执行路径多、分支复杂。",
            f["file"],
        ))

    # 7. Data / Control-flow — 调用链追踪
    chain_sample = random.sample(mat["chains"], min(per_intent, len(mat["chains"])))
    for c in chain_sample:
        evidence_files = set(filter(None, [c.get(f"file{i}") for i in range(1, 5)]))
        questions.append(_make_row(
            "函数级", "Function", "how", "Data / Control-flow",
            c["f1"],
            f"从 {c['f1']} 到 {c['f4']} 的调用链是怎样的？数据如何在这条链路上流转？",
            f"调用链：{c['f1']} -> {c['f2']} -> {c['f3']} -> {c['f4']}。",
            ", ".join(sorted(evidence_files)),
        ))

    # 8. Feature Location — 功能定位
    for f in _pick_unique(mat["leaves"], n=per_intent):
        questions.append(_make_row(
            "函数级", "Function", "where", "Feature Location",
            f["name"],
            f"实现 {f['name']} 功能的代码在哪里？哪些函数使用了它？",
            f"{f['name']} 定义在 {f['file']}，被 {f['fan_in']} 个函数调用。",
            f["file"],
        ))

    # 9. Identifier Location — 标识符定位
    id_pool = mat["hubs"] + mat["high_fan_in"] + mat["leaves"]
    for f in _pick_unique(id_pool, n=per_intent):
        questions.append(_make_row(
            "函数级", "Function", "where", "Identifier Location",
            f["name"],
            f"函数 {f['name']} 定义在哪个文件的什么位置？",
            f"{f['name']} 定义在 {f['file']}。",
            f["file"],
        ))

    # 10. System Design — 跨模块交互
    # 按文件对分组
    file_pairs = {}
    for c in mat["cross_calls"]:
        key = tuple(sorted([c["caller_file"], c["callee_file"]]))
        file_pairs.setdefault(key, []).append(c)
    top_pairs = sorted(file_pairs.items(), key=lambda x: len(x[1]), reverse=True)
    for (f1, f2), calls in top_pairs[:per_intent]:
        call_strs = [f"{c['caller']}->{c['callee']}" for c in calls[:5]]
        questions.append(_make_row(
            "文件级", "File", "how", "System Design",
            f"{f1.split('/')[-1]} & {f2.split('/')[-1]}",
            f"文件 {f1} 和 {f2} 之间是如何交互的？有哪些跨模块调用？",
            f"这两个文件之间有 {len(calls)} 条调用关系，包括：{', '.join(call_strs)}。",
            f"{f1}, {f2}",
        ))

    # 11. Algorithm Implementation — 实现细节
    for f in mat["high_fan_out"][:per_intent]:
        if f["name"] in used_entities:
            continue
        used_entities.add(f["name"])
        summary = _ann_summary(f.get("ann"))
        ans = f"{f['name']}（{f['file']}）调用了 {f['fan_out']} 个子函数来完成其功能。"
        if summary:
            ans += f" {summary}"
        questions.append(_make_row(
            "函数级", "Function", "how", "Algorithm Implementation",
            f["name"],
            f"函数 {f['name']} 的实现逻辑是怎样的？它调用了哪些子函数来完成工作？",
            ans,
            f["file"],
        ))

    # 12. API / Framework Support — 如何使用某函数
    for f in mat["callers_map"][:per_intent]:
        callers_str = ", ".join(f["callers"])
        questions.append(_make_row(
            "函数级", "Function", "how", "API / Framework Support",
            f["name"],
            f"如何使用函数 {f['name']}？有哪些函数调用了它？",
            f"{f['name']}（{f['file']}）被以下函数调用：{callers_str}。",
            f["file"],
        ))

    # 补齐到 target_count
    random.shuffle(questions)
    if len(questions) > target_count:
        questions = questions[:target_count]

    # 重新编号排序
    questions.sort(key=lambda x: (INTENTS.index(x["意图"]) if x["意图"] in INTENTS else 99))
    return questions


def main():
    parser = argparse.ArgumentParser(description="基于代码图生成高质量 QA 题目")
    parser.add_argument("--count", type=int, default=100, help="目标题数（默认 100）")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    driver = get_driver()
    try:
        driver.verify_connectivity()
        print("从图中提取出题素材...")
        mat = _fetch_materials(driver, NEO4J_DATABASE)
        for k, v in mat.items():
            print(f"  {k}: {len(v)} 条")
    finally:
        driver.close()

    print(f"\n生成题目（目标 {args.count} 题）...")
    questions = generate_questions(mat, args.count)

    # 统计
    from collections import Counter
    intent_counts = Counter(q["意图"] for q in questions)
    print(f"实际生成: {len(questions)} 题")
    for intent in INTENTS:
        print(f"  {intent}: {intent_counts.get(intent, 0)} 题")

    # 写 CSV
    fieldnames = ["一级分类", "二级分类", "问题类型", "意图", "实体名称", "具体问题", "答案", "Evidence"]
    with open(args.output, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(questions)
    print(f"\n题目已写入: {args.output}")


if __name__ == "__main__":
    main()
