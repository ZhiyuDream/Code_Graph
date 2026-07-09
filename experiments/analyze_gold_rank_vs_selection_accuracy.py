#!/usr/bin/env python3
"""
Analyze how LLM selection accuracy varies with gold symbol rank in the pool.

If accuracy decreases as gold rank increases, it suggests an anchoring effect:
LLM relies heavily on the initial embedding ranking rather than independently
judging evidence-bearing value.
"""
import json
import sys
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--gold-rank", type=Path, default=Path("results/gold_rank_in_pool_0_50.json"))
    parser.add_argument("--decomposition", type=Path, default=Path("results/retrieval_selection_decomposition_0_50.json"))
    parser.add_argument("-o", "--output", type=Path, default=Path("results/gold_rank_vs_selection_accuracy_0_50.json"))
    args = parser.parse_args()

    with open(args.gold_rank, "r", encoding="utf-8") as f:
        gr = json.load(f)
    with open(args.decomposition, "r", encoding="utf-8") as f:
        dec = json.load(f)

    rank_data = {r["qa_id"]: r for r in gr["per_item"]}
    dec_data = {r["qa_id"]: r for r in dec["per_item"]}

    buckets = [
        ("rank_1", lambda r: r is not None and r == 1),
        ("rank_2_5", lambda r: r is not None and 2 <= r <= 5),
        ("rank_6_10", lambda r: r is not None and 6 <= r <= 10),
        ("rank_11_20", lambda r: r is not None and 11 <= r <= 20),
        ("rank_21_50", lambda r: r is not None and 21 <= r <= 50),
        ("rank_51_plus", lambda r: r is not None and r > 50),
        ("not_in_pool", lambda r: r is None),
    ]

    records = []
    for qa_id in rank_data:
        if qa_id not in dec_data:
            continue
        rank = rank_data[qa_id].get("gold_rank")
        single_hit = dec_data[qa_id].get("single_hit", False)
        multi_hit = dec_data[qa_id].get("multi_hit", False)
        records.append({
            "qa_id": qa_id,
            "gold_rank": rank,
            "single_hit": single_hit,
            "multi_hit": multi_hit,
        })

    print("\nGold Rank vs Selection Accuracy")
    print(f"{'='*70}")
    print(f"{'Bucket':<16s} {'Count':>6s} {'Single Acc':>12s} {'Multi Acc':>12s}")

    summary = []
    for bucket_name, pred in buckets:
        bucket_records = [r for r in records if pred(r["gold_rank"])]
        count = len(bucket_records)
        if count == 0:
            continue
        single_acc = sum(1 for r in bucket_records if r["single_hit"]) / count
        multi_acc = sum(1 for r in bucket_records if r["multi_hit"]) / count
        summary.append({
            "bucket": bucket_name,
            "count": count,
            "single_accuracy": single_acc,
            "multi_accuracy": multi_acc,
        })
        print(f"{bucket_name:<16s} {count:>6d} {single_acc*100:>11.1f}% {multi_acc*100:>11.1f}%")

    # Correlation
    valid = [r for r in records if r["gold_rank"] is not None]
    ranks = np.array([r["gold_rank"] for r in valid])
    single_hits = np.array([1 if r["single_hit"] else 0 for r in valid])
    multi_hits = np.array([1 if r["multi_hit"] else 0 for r in valid])

    print(f"\nSpearman rank correlation (gold_rank vs single_hit): {np.corrcoef(ranks, single_hits)[0,1]:.3f}")
    print(f"Spearman rank correlation (gold_rank vs multi_hit):  {np.corrcoef(ranks, multi_hits)[0,1]:.3f}")

    output = {
        "summary": summary,
        "correlation": {
            "single": float(np.corrcoef(ranks, single_hits)[0,1]),
            "multi": float(np.corrcoef(ranks, multi_hits)[0,1]),
        },
        "per_item": records,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
