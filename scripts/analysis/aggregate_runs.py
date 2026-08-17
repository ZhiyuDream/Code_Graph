#!/usr/bin/env python3
"""多轮采样聚合：self-consistency 实验。

输入多轮 QA 结果 + 各自的 det 评估，输出：
- 每题逐轮覆盖、成功率
- best-of-N（每题取覆盖最高的答案）的整体覆盖
- mean-of-N（真实水位估计）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

def main():
    runs = sys.argv[1:]  # eval json files (det mode)
    if len(runs) < 2:
        print("用法: aggregate_runs.py eval1.json eval2.json [eval3.json ...]")
        return
    per_q = {}
    for rf in runs:
        for e in json.load(open(rf)):
            qid = e["qa_id"]
            det = e.get("eval_citation_det", e["eval_citation"])
            per_q.setdefault(qid, []).append(det["coverage_ratio"])

    n = len(runs)
    best, mean = [], []
    zeros_best = 0
    print(f"{'qa_id':22s} {'各轮覆盖':24s} 成功率(满引)")
    for qid in sorted(per_q):
        ratios = per_q[qid]
        b = max(ratios)
        m = sum(ratios) / len(ratios)
        best.append(b)
        mean.append(m)
        if b == 0:
            zeros_best += 1
        succ = sum(1 for r in ratios if r >= 0.999)
        print(f"{qid:22s} {' '.join(f'{r:5.0%}' for r in ratios):24s} {succ}/{len(ratios)}")

    print(f"\n轮数: {n}")
    print(f"mean-of-{n}（真实水位）: {sum(mean)/len(mean):.1%}")
    print(f"best-of-{n}（取最优）: {sum(best)/len(best):.1%}, 零引用 {zeros_best} 题")
    full_best = sum(1 for b in best if b >= 0.999)
    print(f"best-of-{n} 全引用: {full_best}/{len(best)}")

if __name__ == "__main__":
    main()
