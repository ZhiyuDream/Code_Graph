#!/usr/bin/env python3
"""
分析表现不好的题目，结合 debug 日志研究 LLM 输入输出。

用法：
    python scripts/eval/analyze_bad_cases.py \
        --dataset datasets/posthoc_audit_benchmark_v2.json \
        --results results/posthoc_v2_xxx.json \
        --debug results/posthoc_v2_xxx.debug.json \
        --output analysis/bad_cases_report.md
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load_json(path: str) -> dict | list:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def analyze(
    dataset: dict,
    results: list[dict],
    debug_calls: list[dict],
    output_path: str,
    coverage_threshold: float = 50.0,
):
    items = {item["qa_id"]: item for item in dataset["items"]}
    result_map = {r["id"]: r for r in results}

    # 计算覆盖率
    bad_cases = []
    for qid, item in items.items():
        if qid not in result_map:
            continue
        r = result_map[qid]
        evidence_files = {ev["file"] for ev in item.get("gold_evidence", []) if ev.get("file")}
        retrieved_files = {f.get("file_path", "") for f in r.get("retrieved_functions", [])}
        covered = evidence_files & retrieved_files
        coverage = len(covered) / len(evidence_files) * 100 if evidence_files else 0

        if coverage < coverage_threshold:
            bad_cases.append({
                "qa_id": qid,
                "question": item.get("question", ""),
                "reference_answer": item.get("reference_answer", "")[:200],
                "coverage": coverage,
                "evidence_files": sorted(evidence_files),
                "retrieved_files": sorted(retrieved_files),
                "covered_files": sorted(covered),
                "answer": r.get("answer", "")[:500],
                "steps": r.get("steps", []),
            })

    bad_cases.sort(key=lambda x: x["coverage"])

    # 为每个 bad case 关联 debug calls
    # 简单策略：按顺序分配（因为 benchmark 是并行的，顺序可能不完全对应）
    # 更好的策略：通过 question 内容匹配
    debug_by_question = {}
    for call in debug_calls:
        msgs = call.get("messages", [])
        for msg in msgs:
            content = msg.get("content", "")
            # 找 question 开头
            if content and len(content) > 20:
                # 用前 60 个字符作为 key
                key = content[:60].strip()
                if key not in debug_by_question:
                    debug_by_question[key] = []
                debug_by_question[key].append(call)
                break

    # 生成报告
    lines = [
        "# 表现不好的题目分析报告",
        f"\n覆盖率阈值: {coverage_threshold}%",
        f"表现不好的题目数: {len(bad_cases)} / {len(items)}",
        "\n---\n",
    ]

    for i, case in enumerate(bad_cases, 1):
        lines.append(f"\n## {i}. [{case['qa_id']}] 覆盖率: {case['coverage']:.1f}%\n")
        lines.append(f"**问题**: {case['question']}\n")
        lines.append(f"**参考答案摘要**: {case['reference_answer']}...\n")
        lines.append(f"**生成答案摘要**: {case['answer']}...\n")
        lines.append(f"**证据文件** ({len(case['evidence_files'])}): {', '.join(case['evidence_files'])}\n")
        lines.append(f"**召回文件** ({len(case['retrieved_files'])}): {', '.join(case['retrieved_files'])}\n")
        lines.append(f"**命中文件** ({len(case['covered_files'])}): {', '.join(case['covered_files']) if case['covered_files'] else '无'}\n")

        # ReAct steps
        steps = case["steps"]
        if steps:
            lines.append(f"\n**ReAct 步骤** ({len(steps)}):\n")
            for step in steps:
                phase = step.get("phase", "")
                detail = step.get("detail", "")
                lines.append(f"- [{phase}] {detail[:100]}\n")

        # 关联 debug calls
        qkey = case["question"][:60].strip()
        calls = debug_by_question.get(qkey, [])
        if calls:
            lines.append(f"\n**LLM 调用记录** ({len(calls)} 条):\n")
            for j, call in enumerate(calls[:5], 1):  # 最多 5 条
                lines.append(f"\n### Call {j}\n")
                lines.append(f"```json\n{json.dumps(call, ensure_ascii=False, indent=2)[:2000]}\n```\n")
        else:
            lines.append("\n**LLM 调用记录**: 未匹配到\n")

        lines.append("\n---\n")

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.writelines(lines)

    print(f"报告已保存: {output_path}")
    print(f"表现不好的题目数: {len(bad_cases)}")
    for c in bad_cases[:10]:
        print(f"  - {c['qa_id']}: coverage={c['coverage']:.1f}% | {c['question'][:60]}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="datasets/posthoc_audit_benchmark_v2.json")
    parser.add_argument("--results", required=True)
    parser.add_argument("--debug", required=True)
    parser.add_argument("--output", default="analysis/bad_cases_report.md")
    parser.add_argument("--threshold", type=float, default=50.0)
    args = parser.parse_args()

    dataset = load_json(args.dataset)
    results = load_json(args.results)
    debug_calls = load_json(args.debug)

    analyze(dataset, results, debug_calls, args.output, args.threshold)


if __name__ == "__main__":
    main()
