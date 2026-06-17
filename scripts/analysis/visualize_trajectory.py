#!/usr/bin/env python3
"""
可视化 Dynamic Investigation 的 trajectory。

用法:
    python scripts/analysis/visualize_trajectory.py results/trajectory_entry_0_5.json
"""
import json
import sys
from pathlib import Path


def visualize(result_path: Path):
    with open(result_path, "r", encoding="utf-8") as f:
        results = json.load(f)

    for r in results:
        print(f"\n{'='*70}")
        print(f"题目: {r['qa_id']}  |  条件: {r['condition']}")
        print(f"覆盖率: {r['coverage']*100:.0f}%  |  访问文件数: {r['num_visited']}")
        print(f"问题: {r['question']}")
        print(f"Gold files: {', '.join(r['gold_files'])}")
        print(f"{'='*70}")

        if not r.get("trajectory"):
            print("(无调查轨迹)")
            continue

        print("\n调查轨迹:\n")
        print(f"  入口: {r['entry_file']}")
        for step in r["trajectory"]:
            print(f"\n  Step {step['step']}:")
            if step.get("new_evidence"):
                print(f"    新证据: {step['new_evidence']}")
            if step.get("decision_impact"):
                print(f"    决策影响: {step['decision_impact']}")
            if step.get("next_search_target"):
                print(f"    下一目标: {step['next_search_target']}")
            if step.get("next_action"):
                print(f"    下一步动作: {step['next_action']}")

        print(f"\n  最终访问文件: {' -> '.join(r['visited_files'])}")


if __name__ == "__main__":
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("results/trajectory_entry_0_5.json")
    visualize(path)
