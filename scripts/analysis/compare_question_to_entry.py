#!/usr/bin/env python3
"""
比较不同 Question → Entry 方法的效果。

用法:
    python scripts/analysis/compare_question_to_entry.py \
        results/question_to_entry_embedding_0_15.json \
        results/question_to_entry_keyword_0_15.json \
        results/question_to_entry_symbol_0_15.json \
        results/question_to_entry_hypothesis_0_15.json
"""
import json
import sys
from pathlib import Path


def load(path: Path) -> list:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    paths = [Path(p) for p in sys.argv[1:]]
    all_results = {}
    for p in paths:
        data = load(p)
        if not data:
            continue
        method = data[0]["method"]
        all_results[method] = {r["qa_id"]: r for r in data}

    print(f"{'='*70}")
    print("Question → Entry 方法对比")
    print(f"{'='*70}\n")

    print(f"{'方法':<20} {'Top-1':>8} {'Top-5':>8} {'Top-10':>8}")
    print("-" * 50)
    for method, results in all_results.items():
        total = len(results)
        top1 = sum(1 for r in results.values() if r["top1_hit"]) / total * 100
        top5 = sum(1 for r in results.values() if r["top5_hit"]) / total * 100
        top10 = sum(1 for r in results.values() if r["top10_hit"]) / total * 100
        print(f"{method:<20} {top1:>7.1f}% {top5:>7.1f}% {top10:>7.1f}%")

    # Per-question best method
    print("\n逐题最佳方法:")
    qa_ids = sorted(next(iter(all_results.values())).keys())
    for qa_id in qa_ids:
        best_method = ""
        best_score = -1
        for method, results in all_results.items():
            r = results[qa_id]
            score = (1 if r["top1_hit"] else 0) * 100 + (1 if r["top5_hit"] else 0) * 10 + (1 if r["top10_hit"] else 0)
            if score > best_score:
                best_score = score
                best_method = method
        print(f"  {qa_id}: {best_method}")


if __name__ == "__main__":
    main()
