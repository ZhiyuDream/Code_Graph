#!/usr/bin/env python3
"""Analyze gold symbol rank distribution inside the candidate pool."""
import json
import sys
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.qa.candidate_pool import build_function_pool
from src.qa.retrievers.fast_embedding import FastEmbeddingRetriever


def rank_of(gold_symbols: set[str], candidates: list[dict]) -> int | None:
    for i, c in enumerate(candidates, 1):
        if c["name"] in gold_symbols:
            return i
    return None


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, default=Path("datasets/benchmark_hard.json"))
    parser.add_argument("--oracle", type=Path, default=Path("results/gold_function_expansion_oracle_0_15.json"))
    parser.add_argument("--range", default="0,15")
    parser.add_argument("--top-k-files", type=int, default=20)
    parser.add_argument("--top-m-functions", type=int, default=5)
    parser.add_argument("-o", "--output", type=Path, default=Path("results/gold_rank_in_pool_0_15.json"))
    args = parser.parse_args()

    with open(args.benchmark, "r", encoding="utf-8") as f:
        bench = json.load(f)
    items = bench["items"]
    if "," in args.range:
        start, end = map(int, args.range.split(","))
    else:
        start, end = 0, len(items)

    selected_items = []
    for idx in range(start, end):
        item = items[idx]
        qa_id = item.get("qa_id", f"q{idx}")
        gold_symbols = set()
        for ev in item.get("gold_evidence", []):
            sym = ev.get("symbol", "")
            if sym:
                gold_symbols.add(sym)
        if args.oracle.exists():
            with open(args.oracle, "r", encoding="utf-8") as f:
                oracle = json.load(f)
            oracle_by_qa = {r["qa_id"]: r for r in oracle["per_item"]}
            if qa_id in oracle_by_qa:
                gold_symbols.update(oracle_by_qa[qa_id].get("gold_symbols", []))
        selected_items.append({
            "qa_id": qa_id,
            "question": item.get("question", ""),
            "gold_symbols": gold_symbols,
        })

    retriever = FastEmbeddingRetriever()
    query_embs = retriever.encode_queries([it["question"] for it in selected_items])

    records = []
    ranks = []
    for emb, item in zip(query_embs, selected_items):
        pool = build_function_pool(emb, retriever, args.top_k_files, args.top_m_functions)
        r = rank_of(item["gold_symbols"], pool)
        records.append({
            "qa_id": item["qa_id"],
            "question": item["question"],
            "gold_symbols": sorted(item["gold_symbols"]),
            "pool_size": len(pool),
            "gold_rank": r,
            "in_pool": r is not None,
        })
        if r is not None:
            ranks.append(r)

    total = len(records)
    in_pool = sum(1 for r in records if r["in_pool"])

    print(f"\nGold Symbol Rank Distribution in Pool ({args.top_k_files} files × {args.top_m_functions} funcs)")
    print(f"{'='*70}")
    print(f"Total questions: {total}")
    print(f"Gold in pool: {in_pool} / {total} = {in_pool/total*100:.1f}%")
    if ranks:
        ranks_arr = np.array(ranks)
        print(f"Median rank: {int(np.median(ranks_arr))}")
        print(f"Mean rank: {np.mean(ranks_arr):.1f}")
        for threshold in [5, 10, 20, 50]:
            pct = np.mean(ranks_arr <= threshold) * 100
            print(f"Rank <= {threshold}: {pct:.1f}%")
    print(f"{'='*70}")

    output = {
        "config": {"top_k_files": args.top_k_files, "top_m_functions": args.top_m_functions},
        "summary": {
            "total": total,
            "in_pool": in_pool,
            "in_pool_rate": in_pool / total,
            "median_rank": int(np.median(ranks)) if ranks else None,
            "mean_rank": float(np.mean(ranks)) if ranks else None,
            "rank_le_5": float(np.mean(np.array(ranks) <= 5)) if ranks else None,
            "rank_le_10": float(np.mean(np.array(ranks) <= 10)) if ranks else None,
            "rank_le_20": float(np.mean(np.array(ranks) <= 20)) if ranks else None,
            "rank_le_50": float(np.mean(np.array(ranks) <= 50)) if ranks else None,
        },
        "per_item": records,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
