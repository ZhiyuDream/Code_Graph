#!/usr/bin/env python3
"""
事后审计 QA 答案质量评估器。

评估维度：
1. 召回覆盖率：召回函数覆盖证据文件路径的比例
2. 关键函数命中：生成答案是否提及参考答案中的关键函数名
3. 文件路径命中：生成答案是否包含证据中的文件路径
4. Token / 时延统计

用法：
    python scripts/eval/audit_eval.py \
        --dataset datasets/posthoc_audit_qa.json \
        --results results/posthoc_audit_qa_w20.json \
        --output results/audit_eval_report.json
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


def extract_file_paths(text: str) -> set[str]:
    """从文本中提取文件路径（如 src/llama.cpp）"""
    pattern = r'[\w\-/.]+\.(?:cpp|c|h|hpp)'
    return set(re.findall(pattern, text))


def extract_function_names(text: str) -> set[str]:
    """从文本中提取可能的函数名（C++ 风格）"""
    # 匹配 `func_name(` 或 `func_name` 在代码块中
    pattern = r'`?([a-zA-Z_][a-zA-Z0-9_:]+)`?\s*\('
    return set(re.findall(pattern, text))


def evaluate_one(raw_item: dict, result: dict) -> dict[str, Any]:
    """评估单题"""
    # 1. 召回覆盖率
    evidence_files = set()
    for ev in raw_item.get("gold_evidence", []):
        if isinstance(ev, dict):
            fp = ev.get("file", "")
            if fp:
                evidence_files.add(fp)

    retrieved_files = set()
    for f in result.get("retrieved_functions", []):
        fp = f.get("file_path", "")
        if fp:
            retrieved_files.add(fp)

    covered = evidence_files & retrieved_files
    recall_coverage = len(covered) / len(evidence_files) * 100 if evidence_files else 0

    # 2. 关键函数命中（参考答案中的函数名是否在生成答案中出现）
    ref_answer = raw_item.get("reference_answer", "")
    gen_answer = result.get("answer", "")

    ref_funcs = extract_function_names(ref_answer)
    gen_funcs = extract_function_names(gen_answer)
    func_hits = ref_funcs & gen_funcs
    func_hit_rate = len(func_hits) / len(ref_funcs) * 100 if ref_funcs else 0

    # 3. 文件路径命中（证据中的文件路径是否在生成答案中出现）
    gen_files = extract_file_paths(gen_answer)
    file_hits = evidence_files & gen_files
    file_hit_rate = len(file_hits) / len(evidence_files) * 100 if evidence_files else 0

    # 4. 基础统计
    steps = result.get("steps", [])
    react_steps = [s for s in steps if s.get("phase") == "react_search"]
    total_tokens = result.get("total_tokens", {})

    return {
        "qa_id": raw_item.get("qa_id"),
        "question": raw_item.get("question", "")[:60],
        "recall_coverage": round(recall_coverage, 1),
        "evidence_files": len(evidence_files),
        "retrieved_files": len(retrieved_files),
        "covered_files": len(covered),
        "func_hit_rate": round(func_hit_rate, 1),
        "ref_funcs": len(ref_funcs),
        "hit_funcs": len(func_hits),
        "file_hit_rate": round(file_hit_rate, 1),
        "hit_files": len(file_hits),
        "answer_length": len(gen_answer),
        "react_steps": len(react_steps),
        "latency_ms": result.get("latency_ms", 0),
        "total_tokens": total_tokens.get("total", 0),
        "prompt_tokens": total_tokens.get("prompt", 0),
        "completion_tokens": total_tokens.get("completion", 0),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", required=True, help="原始数据集 JSON")
    parser.add_argument("--results", required=True, help="生成结果 JSON")
    parser.add_argument("--output", default="results/audit_eval_report.json")
    args = parser.parse_args()

    with open(args.dataset) as f:
        dataset = json.load(f)
    with open(args.results) as f:
        results = json.load(f)

    raw_map = {item["qa_id"]: item for item in dataset["items"]}
    result_map = {r["id"]: r for r in results}

    evaluations = []
    for qid in sorted(raw_map.keys()):
        if qid in result_map:
            ev = evaluate_one(raw_map[qid], result_map[qid])
            evaluations.append(ev)

    # 汇总统计
    total = len(evaluations)
    avg_recall = sum(e["recall_coverage"] for e in evaluations) / total
    avg_func_hit = sum(e["func_hit_rate"] for e in evaluations) / total
    avg_file_hit = sum(e["file_hit_rate"] for e in evaluations) / total
    full_recall = sum(1 for e in evaluations if e["recall_coverage"] >= 100)
    zero_recall = sum(1 for e in evaluations if e["recall_coverage"] == 0)
    avg_latency = sum(e["latency_ms"] for e in evaluations) / total
    total_tokens = sum(e["total_tokens"] for e in evaluations)
    avg_answer_len = sum(e["answer_length"] for e in evaluations) / total

    summary = {
        "total_questions": total,
        "avg_recall_coverage": round(avg_recall, 1),
        "full_recall_count": f"{full_recall}/{total}",
        "zero_recall_count": f"{zero_recall}/{total}",
        "avg_func_hit_rate": round(avg_func_hit, 1),
        "avg_file_hit_rate": round(avg_file_hit, 1),
        "avg_latency_ms": round(avg_latency, 0),
        "total_tokens": total_tokens,
        "avg_answer_length": round(avg_answer_len, 0),
    }

    report = {
        "summary": summary,
        "evaluations": evaluations,
    }

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print("=== 评估汇总 ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    print(f"\n详细报告: {args.output}")


if __name__ == "__main__":
    main()
