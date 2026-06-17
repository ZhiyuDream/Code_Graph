#!/usr/bin/env python3
"""
合并多个 trajectory 结果文件，并做完整分析。

用法:
    python scripts/analysis/merge_and_analyze_trajectories.py \
        results/trajectory_entry_0_5.json results/trajectory_entry_5_15.json \
        results/trajectory_gold_0_5.json results/trajectory_gold_5_15.json \
        -o results/trajectory_merged_0_15.json
"""
import json
import sys
from pathlib import Path


def load_results(path: Path) -> list:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def merge(entry_paths: list[Path], gold_paths: list[Path]) -> list:
    entries = []
    for p in entry_paths:
        entries.extend(load_results(p))
    entry_map = {r["qa_id"]: r for r in entries}

    golds = []
    for p in gold_paths:
        golds.extend(load_results(p))
    gold_map = {r["qa_id"]: r for r in golds}

    merged = []
    for qa_id in sorted(entry_map.keys()):
        e = entry_map[qa_id]
        g = gold_map.get(qa_id)
        merged.append({
            "qa_id": qa_id,
            "question": e["question"],
            "gold_files": e["gold_files"],
            "entry_file": e["entry_file"],
            "entry_coverage": e["coverage"],
            "entry_visited": e["visited_files"],
            "entry_num_visited": e["num_visited"],
            "entry_trajectory": e.get("trajectory", []),
            "gold_coverage": g["coverage"] if g else None,
            "gold_visited": g["visited_files"] if g else [],
            "gap": (g["coverage"] - e["coverage"]) if g else None,
        })
    return merged


def analyze(merged: list):
    total = len(merged)
    entry_full = sum(1 for r in merged if r["entry_coverage"] >= 1.0)
    gold_full = sum(1 for r in merged if r["gold_coverage"] is not None and r["gold_coverage"] >= 1.0)
    entry_avg = sum(r["entry_coverage"] for r in merged) / total
    gold_avg = sum(r["gold_coverage"] for r in merged if r["gold_coverage"] is not None) / total

    print(f"总题数: {total}")
    print(f"Gold 引用全: {gold_full}/{total} ({gold_full/total*100:.0f}%)")
    print(f"Entry 引用全: {entry_full}/{total} ({entry_full/total*100:.0f}%)")
    print(f"Gold 平均覆盖率: {gold_avg*100:.1f}%")
    print(f"Entry 平均覆盖率: {entry_avg*100:.1f}%")
    print(f"平均 Gap: {(gold_avg - entry_avg)*100:.1f} 个百分点\n")

    print("逐题对比:")
    for r in merged:
        gap = r["gap"]
        gap_str = f"{gap*100:.0f}" if gap is not None else "N/A"
        entry_visited = set(r["entry_visited"])
        missing = [f for f in r["gold_files"] if f not in entry_visited]
        missing_str = f"  漏掉: {', '.join(missing)}" if missing else "  访问了所有 gold files"
        print(f"{r['qa_id']}: Entry={r['entry_coverage']*100:.0f}%  Gold={r['gold_coverage']*100:.0f}%  Gap={gap_str}pp{missing_str}")


def main():
    args = sys.argv[1:]
    output = None
    if "-o" in args:
        idx = args.index("-o")
        output = Path(args[idx + 1])
        args = args[:idx] + args[idx + 2:]

    # Split into entry and gold by condition inside file or by convention
    # For now, assume first half are entry, second half are gold
    mid = len(args) // 2
    entry_paths = [Path(p) for p in args[:mid]]
    gold_paths = [Path(p) for p in args[mid:]]

    merged = merge(entry_paths, gold_paths)
    analyze(merged)

    if output:
        with open(output, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        print(f"\n合并结果已保存: {output}")


if __name__ == "__main__":
    main()
