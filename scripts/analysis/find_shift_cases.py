#!/usr/bin/env python3
"""
从 trajectory 结果中找出搜索方向发生明显改变的案例。

用法:
    python scripts/analysis/find_shift_cases.py results/trajectory_entry_0_5.json
"""
import json
import sys
from pathlib import Path


SHIFT_KEYWORDS = [
    "转向", "从", "转到", "改为", "因此", "需要", "必须", "下一步",
    "shift", "turn", "move to", "switch", "therefore", "need to", "next"
]


def has_shift(step: dict) -> bool:
    """判断某一步是否发生明显方向改变。"""
    impact = step.get("decision_impact", "")
    if not impact:
        return False
    return any(kw in impact for kw in SHIFT_KEYWORDS)


def analyze(result_path: Path):
    with open(result_path, "r", encoding="utf-8") as f:
        results = json.load(f)

    cases = []
    for r in results:
        shifts = [step for step in r.get("trajectory", []) if has_shift(step)]
        if shifts:
            cases.append({
                "qa_id": r["qa_id"],
                "coverage": r["coverage"],
                "num_visited": r["num_visited"],
                "num_shifts": len(shifts),
                "shifts": shifts,
            })

    print(f"共 {len(results)} 题，其中 {len(cases)} 题出现明显方向改变。\n")

    for case in cases:
        r = next(res for res in results if res["qa_id"] == case["qa_id"])
        print(f"{'='*70}")
        print(f"题目: {case['qa_id']}")
        print(f"问题: {r['question']}")
        print(f"覆盖率: {case['coverage']*100:.0f}% | 访问文件数: {case['num_visited']} | 方向改变次数: {case['num_shifts']}")
        print(f"Gold files: {', '.join(r['gold_files'])}")
        print(f"访问轨迹: {' -> '.join(r['visited_files'])}")
        print("\n方向改变详情:")
        for step in case["shifts"]:
            print(f"\n  Step {step['step']}:")
            print(f"    新证据: {step.get('new_evidence', '')}")
            print(f"    决策影响: {step.get('decision_impact', '')}")
            print(f"    下一目标: {step.get('next_search_target', '')}")
            print(f"    下一步动作: {step.get('next_action', '')}")
        print()


if __name__ == "__main__":
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("results/trajectory_entry_0_5.json")
    analyze(path)
