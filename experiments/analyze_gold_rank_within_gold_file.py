#!/usr/bin/env python3
"""
Analyze gold symbol rank within gold file.

For each question where the gold FILE is retrieved but the gold SYMBOL is not (category B),
retrieve functions ONLY from the gold file(s) and rank them by embedding similarity.
Then compute the rank of the gold symbol within its own file.

This answers: is the gold function just outside top-k, or deeply buried in the file?
"""
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.qa.retrievers.embedding import EmbeddingRetriever


def normalize_path(path: str) -> str:
    return path.lstrip('./')


def load_items(bench_path: Path, range_str: str) -> list[dict]:
    with open(bench_path, "r", encoding="utf-8") as f:
        bench = json.load(f)
    items = bench["items"]
    if "," in range_str:
        start, end = map(int, range_str.split(","))
    else:
        start, end = 0, len(items)
    selected = []
    for idx in range(start, end):
        item = items[idx]
        gold_files = sorted(set(
            ev["file"] for ev in item.get("gold_evidence", [])
            if not ev["file"].endswith((".h", ".hpp"))
        ))
        selected.append({
            "qa_id": item.get("qa_id", f"q{idx}"),
            "question": item.get("question", ""),
            "gold_files": gold_files,
        })
    return selected


def get_rank_in_file(question: str, gold_file: str, gold_symbol: str,
                     retriever: EmbeddingRetriever, top_n: int = 100) -> int | None:
    """Retrieve functions only from gold_file and return rank of gold_symbol."""
    results = retriever.retrieve(question, top_k=top_n, file_filter={gold_file})
    for rank, r in enumerate(results, 1):
        if r.metadata.get("name") == gold_symbol:
            return rank
    return None


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, default=Path("datasets/benchmark_hard.json"))
    parser.add_argument("--oracle", type=Path, default=Path("results/gold_function_expansion_oracle_0_15.json"))
    parser.add_argument("--range", default="0,15")
    parser.add_argument("--top-n", type=int, default=200)
    parser.add_argument("-o", "--output", type=Path, default=Path("results/gold_rank_within_gold_file_0_15.json"))
    args = parser.parse_args()

    items = load_items(args.benchmark, args.range)
    with open(args.oracle, "r", encoding="utf-8") as f:
        oracle = json.load(f)
    oracle_by_qa = {r["qa_id"]: r for r in oracle["per_item"]}

    retriever = EmbeddingRetriever()

    analyses = []
    ranks = []

    print(f"=== Gold Symbol Rank Within Gold File (top_n={args.top_n}) ===\n")
    for item in items:
        qa_id = item["qa_id"]
        o = oracle_by_qa[qa_id]
        gold_symbols = set(o.get("gold_symbols", []))
        best_symbol = o.get("best_symbol")
        gold_files = item["gold_files"]

        if not gold_symbols or not gold_files or not best_symbol:
            continue

        # First check full-repo retrieval to classify A/B/C
        full_results = retriever.retrieve(item["question"], top_k=10)
        full_names = {r.metadata.get("name") for r in full_results}
        full_files = {normalize_path(r.metadata.get("file_path", "")) for r in full_results}

        file_hit = any(g in full_files or any(g in ff or ff in g for ff in full_files) for g in gold_files)
        symbol_hit = bool(gold_symbols & full_names)

        if not file_hit or symbol_hit:
            continue  # only analyze category B

        item_analysis = {
            "qa_id": qa_id,
            "question": item["question"],
            "gold_files": gold_files,
            "gold_symbols": list(gold_symbols),
            "best_symbol": best_symbol,
            "ranks_per_file": [],
        }

        best_rank = None
        for gold_file in gold_files:
            for sym in gold_symbols:
                rank = get_rank_in_file(item["question"], gold_file, sym, retriever, args.top_n)
                if rank is not None:
                    item_analysis["ranks_per_file"].append({
                        "file": gold_file,
                        "symbol": sym,
                        "rank": rank,
                    })
                    if sym == best_symbol:
                        if best_rank is None or rank < best_rank:
                            best_rank = rank

        item_analysis["best_rank_in_gold_file"] = best_rank
        analyses.append(item_analysis)
        if best_rank is not None:
            ranks.append(best_rank)

        print(f"{qa_id}: best={best_symbol}, rank_in_gold_file={best_rank}")
        for rpf in item_analysis["ranks_per_file"]:
            print(f"  {rpf['file']} :: {rpf['symbol']} = rank {rpf['rank']}")

    if ranks:
        avg_rank = sum(ranks) / len(ranks)
        median_rank = sorted(ranks)[len(ranks) // 2]
        mrr = sum(1 / r for r in ranks) / len(ranks)

        print(f"\n{'='*60}")
        print(f"Gold Symbol Rank Within Gold File (n={len(ranks)} B-cases)")
        print(f"{'='*60}")
        print(f"平均排名: {avg_rank:.1f}")
        print(f"中位数排名: {median_rank}")
        print(f"MRR: {mrr:.3f}")
        for k in [1, 5, 10, 20, 50, 100]:
            rate = sum(1 for r in ranks if r <= k) / len(ranks)
            print(f"Recall@{k}: {rate*100:.1f}%")

    output = {
        "summary": {
            "total_b_cases": len(analyses),
            "avg_rank": sum(ranks) / len(ranks) if ranks else None,
            "median_rank": sorted(ranks)[len(ranks) // 2] if ranks else None,
            "mrr": sum(1 / r for r in ranks) / len(ranks) if ranks else None,
        },
        "per_item": analyses,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
