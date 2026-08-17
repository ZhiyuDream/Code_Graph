#!/usr/bin/env python3
"""
重新分析 Module Prediction：用 Gold Module Coverage 替代 "至少一个 gold function"。

问题：旧指标 "Recall@K = 至少一个 gold function 在 top-K modules" 有粒度陷阱。
因为 Module 比 Function 粗，一个 module 包含很多函数，容易"碰巧命中"。

本脚本计算真正的 Gold Module Coverage：
  coverage = |gold_modules ∩ top_k_modules| / |gold_modules|

同时分析：
- 每题 gold module 数量分布
- Top-3 module coverage 分布
- 旧指标 vs 新指标的差距
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))


def load_results(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def analyze(data: dict):
    per_q = data["max"]["per_question"]  # 用 max scoring 的结果

    gold_module_counts = []
    old_recall_values = []
    new_coverage_values = []
    top3_covered_counts = []

    for q in per_q:
        gold_modules = set(q["gold_modules"])
        if not gold_modules:
            continue

        # 从 per_question 里重建 top-3 modules
        # scope_results.module_m3.scope_info.modules
        top3_modules = set(q["scope_results"]["module_m3"]["scope_info"]["modules"])

        old_recall = q.get("module_recall@3", False)
        new_coverage = len(gold_modules & top3_modules) / len(gold_modules)

        gold_module_counts.append(len(gold_modules))
        old_recall_values.append(1 if old_recall else 0)
        new_coverage_values.append(new_coverage)
        top3_covered_counts.append(len(gold_modules & top3_modules))

    print("=" * 70)
    print("GOLD MODULE COVERAGE ANALYSIS")
    print("=" * 70)

    print("\n[Gold Module Count Distribution]")
    counter = Counter(gold_module_counts)
    for k in sorted(counter.keys()):
        print(f"  {k} gold module(s): {counter[k]} questions ({counter[k]/len(per_q)*100:.1f}%)")

    print(f"\n  Mean gold modules per question: {np.mean(gold_module_counts):.2f}")
    print(f"  Median gold modules: {np.median(gold_module_counts):.0f}")

    print("\n[Old Metric vs New Metric]")
    print(f"  Old: Top-3 module recall (>=1 gold func): {np.mean(old_recall_values)*100:.1f}%")
    print(f"  New: Top-3 gold module coverage (avg):    {np.mean(new_coverage_values)*100:.1f}%")

    print("\n[New Metric Distribution]")
    bins = [0, 0.25, 0.5, 0.75, 0.99, 1.0]
    labels = ["0%", "1-25%", "26-50%", "51-75%", "76-99%", "100%"]
    for i in range(len(bins) - 1):
        count = sum(1 for c in new_coverage_values if bins[i] < c <= bins[i + 1])
        print(f"  Coverage {labels[i]:>7}: {count} questions ({count/len(per_q)*100:.1f}%)")

    print("\n[Top-3 Covered Module Count Distribution]")
    covered_counter = Counter(top3_covered_counts)
    for k in sorted(covered_counter.keys()):
        print(f"  Covered {k} module(s): {covered_counter[k]} questions")


if __name__ == "__main__":
    path = _ROOT / "results" / "module_constrained_retrieval_full.json"
    data = load_results(path)
    analyze(data)
