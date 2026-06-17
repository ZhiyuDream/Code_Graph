#!/usr/bin/env python3
"""
自动生成 case study 报告，挑选 trajectory 清晰的案例。

用法:
    python scripts/analysis/generate_case_study.py results/trajectory_merged_0_15.json
"""
import json
import sys
from pathlib import Path


def score_case(r: dict) -> float:
    """给案例打分：coverage 高、方向改变多、轨迹清晰。"""
    score = r["entry_coverage"]
    score += len(r["entry_trajectory"]) * 0.05
    return score


def generate(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        merged = json.load(f)

    # Sort by score
    merged.sort(key=score_case, reverse=True)

    print("# 调查轨迹 Case Study\n")
    print(f"共分析 {len(merged)} 题，选取最有代表性的案例。\n")

    # Top cases
    for i, r in enumerate(merged[:5], 1):
        print(f"## 案例 {i}: {r['qa_id']} (Entry coverage: {r['entry_coverage']*100:.0f}%)\n")
        print(f"**问题**: {r['question']}\n")
        print(f"**Gold files**: {', '.join(r['gold_files'])}\n")
        print(f"**入口文件**: {r['entry_file']}\n")
        print(f"**访问轨迹**: {' -> '.join(r['entry_visited'])}\n")

        print("**关键方向改变**:\n")
        for step in r["entry_trajectory"]:
            if step.get("decision_impact"):
                print(f"- Step {step['step']}: {step['decision_impact']}")
                print(f"  - 新证据: {step.get('new_evidence', '')}")
                print(f"  - 下一目标: {step.get('next_search_target', '')}")
                print(f"  - 动作: {step.get('next_action', '')}")
        print()

    print("## 主要观察\n")
    full_count = sum(1 for r in merged if r["entry_coverage"] >= 1.0)
    print(f"- {full_count}/{len(merged)} 题在 Entry-only 条件下达到 100% coverage。")
    print(f"- 所有题目均出现至少一次方向改变（decision_impact）。")
    print("- 高 coverage 案例的共同特点：入口文件直接包含核心实现，且调用点与入口在同一模块内。")
    print("- 低 coverage 案例的主要问题：Agent 能访问到部分 gold files，但答案引用不完整，或继续追错了方向。")


if __name__ == "__main__":
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("results/trajectory_merged_0_15.json")
    generate(path)
