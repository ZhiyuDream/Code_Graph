#!/usr/bin/env python3
"""
比较 Entry-only 和 Gold-files 两种条件的 Coverage，分析 Oracle-Entry Gap。

用法:
    python scripts/analysis/compare_entry_gold_gap.py \
        results/trajectory_entry_0_5.json \
        results/trajectory_gold_0_5.json
"""
import json
import sys
from pathlib import Path


def load(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return {r["qa_id"]: r for r in data}


def main():
    entry_path = Path(sys.argv[1])
    gold_path = Path(sys.argv[2])

    entry = load(entry_path)
    gold = load(gold_path)

    print(f"{'='*70}")
    print("Oracle-Entry Gap 分析")
    print(f"{'='*70}\n")

    total = len(entry)
    entry_full = sum(1 for r in entry.values() if r["coverage"] >= 1.0)
    gold_full = sum(1 for r in gold.values() if r["coverage"] >= 1.0)
    entry_avg = sum(r["coverage"] for r in entry.values()) / total
    gold_avg = sum(r["coverage"] for r in gold.values()) / total

    print(f"总题数: {total}")
    print(f"Gold 引用全: {gold_full}/{total} ({gold_full/total*100:.0f}%)")
    print(f"Entry 引用全: {entry_full}/{total} ({entry_full/total*100:.0f}%)")
    print(f"Gold 平均覆盖率: {gold_avg*100:.1f}%")
    print(f"Entry 平均覆盖率: {entry_avg*100:.1f}%")
    print(f"平均 Gap: {(gold_avg - entry_avg)*100:.1f} 个百分点\n")

    print(f"{'='*70}")
    print("逐题对比")
    print(f"{'='*70}\n")

    for qa_id in sorted(entry.keys()):
        e = entry[qa_id]
        g = gold[qa_id]
        gap = g["coverage"] - e["coverage"]

        # Missing gold files in entry
        entry_visited = set(e["visited_files"])
        missing = [f for f in g["gold_files"] if f not in entry_visited]

        print(f"{qa_id}:")
        print(f"  问题: {e['question']}")
        print(f"  Entry coverage: {e['coverage']*100:.0f}% ({e['num_visited']} files)")
        print(f"  Gold coverage:  {g['coverage']*100:.0f}% ({g['num_visited']} files)")
        print(f"  Gap: {gap*100:.0f} 个百分点")
        if missing:
            print(f"  Entry 漏掉的 gold files: {', '.join(missing)}")
        else:
            print(f"  Entry 访问了所有 gold files，但 coverage 仍低于 gold（引用不全）")
        print()


if __name__ == "__main__":
    main()
