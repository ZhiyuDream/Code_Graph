#!/usr/bin/env python3
"""Test embedding retriever stability across multiple runs."""
import json
import sys
from pathlib import Path
from collections import defaultdict

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.qa.retrievers.embedding import EmbeddingRetriever


def load_items(bench_path: Path, range_str: str) -> list[dict]:
    with open(bench_path, "r", encoding="utf-8") as f:
        bench = json.load(f)
    items = bench["items"]
    if "," in range_str:
        start, end = map(int, range_str.split(","))
    else:
        start, end = 0, len(items)
    return items[start:end]


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, default=Path("datasets/benchmark_hard.json"))
    parser.add_argument("--oracle", type=Path, default=Path("results/gold_function_expansion_oracle_0_15.json"))
    parser.add_argument("--range", default="0,15")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("-o", "--output", type=Path, default=Path("results/retriever_stability_test_0_15.json"))
    args = parser.parse_args()

    items = load_items(args.benchmark, args.range)
    with open(args.oracle, "r", encoding="utf-8") as f:
        oracle = json.load(f)
    oracle_by_qa = {r["qa_id"]: r for r in oracle["per_item"]}

    retriever = EmbeddingRetriever()

    results = []
    print(f"=== Retriever stability: {args.runs} runs per question (top_k={args.top_k}) ===\n")

    for item in items:
        qa_id = item["qa_id"]
        question = item["question"]
        gold_symbols = set(oracle_by_qa[qa_id].get("gold_symbols", []))
        best_symbol = oracle_by_qa[qa_id].get("best_symbol")

        # Aggregate candidate scores across runs
        candidate_scores = defaultdict(list)
        candidate_meta = {}
        run_rankings = []

        for run in range(args.runs):
            retrieved = retriever.retrieve(question, top_k=args.top_k)
            seen = set()
            ranking = []
            for rank, r in enumerate(retrieved, 1):
                meta = r.metadata
                name = meta.get("name", "")
                file_path = meta.get("file_path", "")
                key = (file_path, name)
                if name and key not in seen:
                    seen.add(key)
                    ranking.append(name)
                    candidate_scores[name].append(rank)
                    candidate_meta[name] = {"file_path": file_path, "name": name}
            run_rankings.append(ranking)

        # Aggregate by average rank (lower is better); penalize missing by assigning top_k+1
        avg_ranks = []
        for name, ranks in candidate_scores.items():
            avg_rank = sum(ranks) / len(ranks)
            missing_penalty = (args.runs - len(ranks)) * (args.top_k + 1)
            adjusted_rank = (sum(ranks) + missing_penalty) / args.runs
            avg_ranks.append((name, adjusted_rank, avg_rank, len(ranks)))
        avg_ranks.sort(key=lambda x: x[1])

        # Check gold symbol presence in each run and in aggregated ranking
        gold_in_runs = []
        for ranking in run_rankings:
            gold_in_runs.append(any(sym in ranking for sym in gold_symbols))
        gold_in_aggregated_topk = []
        for sym in gold_symbols:
            for rank, (name, _, _, _) in enumerate(avg_ranks[:args.top_k], 1):
                if name == sym:
                    gold_in_aggregated_topk.append({"symbol": sym, "rank": rank})
                    break

        result = {
            "qa_id": qa_id,
            "gold_symbols": list(gold_symbols),
            "best_symbol": best_symbol,
            "gold_in_run_rate": sum(gold_in_runs) / len(gold_in_runs),
            "gold_in_aggregated_topk": gold_in_aggregated_topk,
            "aggregated_top10": [name for name, _, _, _ in avg_ranks[:10]],
            "run_rankings": run_rankings,
        }
        results.append(result)

        print(f"{qa_id}: gold_in_run_rate={result['gold_in_run_rate']:.1%}")
        print(f"  aggregated top 5: {result['aggregated_top10'][:5]}")
        print(f"  gold in aggregated top{args.top_k}: {result['gold_in_aggregated_topk']}")

    total = len(results)
    gold_in_run_rate_avg = sum(r["gold_in_run_rate"] for r in results) / total
    gold_in_aggregated_topk_count = sum(1 for r in results if r["gold_in_aggregated_topk"]) / total

    print(f"\n{'='*60}")
    print("Retriever Stability Summary")
    print(f"{'='*60}")
    print(f"总题数: {total}")
    print(f"Gold symbol 平均出现在单次 run top-{args.top_k} 的比例: {gold_in_run_rate_avg*100:.1f}%")
    print(f"Gold symbol 出现在聚合排名 top-{args.top_k} 的比例: {gold_in_aggregated_topk_count*100:.1f}%")

    output = {
        "summary": {
            "total": total,
            "runs": args.runs,
            "top_k": args.top_k,
            "gold_in_run_rate_avg": gold_in_run_rate_avg,
            "gold_in_aggregated_topk_rate": gold_in_aggregated_topk_count,
        },
        "per_item": results,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
