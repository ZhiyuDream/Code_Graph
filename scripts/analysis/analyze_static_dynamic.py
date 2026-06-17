#!/usr/bin/env python3
"""分析 Static vs Dynamic 实验结果。"""
import json
import sys
from pathlib import Path


def analyze(result_path: Path):
    with open(result_path, "r", encoding="utf-8") as f:
        results = json.load(f)

    total = len(results)
    static_full = sum(1 for r in results if r["static"]["coverage"] >= 1.0)
    dynamic_full = sum(1 for r in results if r["dynamic"]["coverage"] >= 1.0)
    static_avg_cov = sum(r["static"]["coverage"] for r in results) / total
    dynamic_avg_cov = sum(r["dynamic"]["coverage"] for r in results) / total
    static_avg_files = sum(r["static"]["num_visited"] for r in results) / total
    dynamic_avg_files = sum(r["dynamic"]["num_visited"] for r in results) / total

    static_wins = sum(1 for r in results if r["static"]["coverage"] > r["dynamic"]["coverage"])
    dynamic_wins = sum(1 for r in results if r["dynamic"]["coverage"] > r["static"]["coverage"])
    ties = sum(1 for r in results if r["static"]["coverage"] == r["dynamic"]["coverage"])

    print(f"总题数: {total}")
    print(f"Static 引用全: {static_full}/{total} ({static_full/total*100:.0f}%)")
    print(f"Dynamic 引用全: {dynamic_full}/{total} ({dynamic_full/total*100:.0f}%)")
    print(f"Static 平均覆盖率: {static_avg_cov*100:.1f}%")
    print(f"Dynamic 平均覆盖率: {dynamic_avg_cov*100:.1f}%")
    print(f"Static 平均访问文件数: {static_avg_files:.1f}")
    print(f"Dynamic 平均访问文件数: {dynamic_avg_files:.1f}")
    print(f"Static 胜: {static_wins}, Dynamic 胜: {dynamic_wins}, 平: {ties}")

    print("\n逐题详情:")
    for r in results:
        s = r["static"]
        d = r["dynamic"]
        winner = "Dynamic" if d["coverage"] > s["coverage"] else ("Static" if s["coverage"] > d["coverage"] else "Tie")
        print(f"{r['qa_id']}: Static={s['coverage']*100:.0f}%({s['num_visited']}f) "
              f"Dynamic={d['coverage']*100:.0f}%({d['num_visited']}f) -> {winner}")


if __name__ == "__main__":
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("results/static_dynamic_full_v26.json")
    analyze(path)
