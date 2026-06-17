#!/usr/bin/env python3
"""
Experiment B (lightweight): Graph reachability from embedding seeds.

Check whether oracle gold symbols are reachable within N hops in the call graph
from embedding Top-K seeds. No file expansion, just graph traversal.

This answers: how far can pure structure take us?
"""
import json
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from config import get_repo_root
from src.qa.retrievers.embedding import EmbeddingRetriever
from src.core.neo4j_client import run_cypher


def graph_expand(seed_symbols: list[str], hops: int, limit_per_node: int = 10) -> set[str]:
    """Expand seed symbols by N hops in the call graph (undirected)."""
    reachable = set(seed_symbols)
    frontier = set(seed_symbols)
    for _ in range(hops):
        if not frontier:
            break
        next_frontier = set()
        for sym in frontier:
            try:
                r1 = run_cypher("""
                    MATCH (caller:Function)-[:CALLS]->(callee:Function {name: $name})
                    RETURN caller.name AS name LIMIT $limit
                """, {"name": sym, "limit": limit_per_node})
                r2 = run_cypher("""
                    MATCH (caller:Function {name: $name})-[:CALLS]->(callee:Function)
                    RETURN callee.name AS name LIMIT $limit
                """, {"name": sym, "limit": limit_per_node})
                for row in r1 + r2:
                    name = row.get("name")
                    if name and name not in reachable:
                        next_frontier.add(name)
                        reachable.add(name)
            except Exception:
                pass
        frontier = next_frontier
    return reachable


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


def run_item(item: dict, oracle_item: dict, retriever: EmbeddingRetriever,
             top_k: int, graph_limit: int) -> dict:
    results = retriever.retrieve(item["question"], top_k=top_k)
    seeds = []
    seen = set()
    for r in results:
        meta = r.metadata
        name = meta.get("name", "")
        if name and name not in seen:
            seen.add(name)
            seeds.append(name)

    reachable_1hop = graph_expand(seeds, hops=1, limit_per_node=graph_limit)
    reachable_2hop = graph_expand(seeds, hops=2, limit_per_node=graph_limit)
    reachable_3hop = graph_expand(seeds, hops=3, limit_per_node=graph_limit)

    gold_symbols = set(oracle_item.get("gold_symbols", []))
    best_symbol = oracle_item.get("best_symbol")

    return {
        "qa_id": item["qa_id"],
        "num_seeds": len(seeds),
        "reachable_1hop": len(reachable_1hop),
        "reachable_2hop": len(reachable_2hop),
        "reachable_3hop": len(reachable_3hop),
        "gold_symbols": list(gold_symbols),
        "best_symbol": best_symbol,
        "gold_in_1hop": bool(gold_symbols & reachable_1hop),
        "gold_in_2hop": bool(gold_symbols & reachable_2hop),
        "gold_in_3hop": bool(gold_symbols & reachable_3hop),
        "best_in_1hop": best_symbol in reachable_1hop if best_symbol else False,
        "best_in_2hop": best_symbol in reachable_2hop if best_symbol else False,
        "best_in_3hop": best_symbol in reachable_3hop if best_symbol else False,
    }


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, default=Path("datasets/benchmark_hard.json"))
    parser.add_argument("--oracle", type=Path, default=Path("results/gold_function_expansion_oracle_0_15.json"))
    parser.add_argument("--range", default="0,15")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--graph-limit", type=int, default=10)
    parser.add_argument("-w", "--workers", type=int, default=2)
    parser.add_argument("-o", "--output", type=Path, default=Path("results/experiment_b_graph_reachability_0_15.json"))
    args = parser.parse_args()

    items = load_items(args.benchmark, args.range)
    with open(args.oracle, "r", encoding="utf-8") as f:
        oracle = json.load(f)
    oracle_by_qa = {r["qa_id"]: r for r in oracle["per_item"]}

    retriever = EmbeddingRetriever()

    results = []
    completed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(run_item, item, oracle_by_qa[item["qa_id"]],
                            retriever, args.top_k, args.graph_limit): item
            for item in items
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            completed += 1
            print(f"  [{completed}/{len(items)}] {result['qa_id']}: "
                  f"1hop={result['gold_in_1hop']} 2hop={result['gold_in_2hop']} 3hop={result['gold_in_3hop']} "
                  f"(reachable: {result['reachable_1hop']}/{result['reachable_2hop']}/{result['reachable_3hop']})")

    results.sort(key=lambda x: x["qa_id"])
    total = len(results)

    def rate(key):
        return sum(1 for r in results if r[key]) / total

    print(f"\n{'='*60}")
    print("Experiment B: Graph Reachability from Embedding Seeds")
    print(f"{'='*60}")
    print(f"总题数: {total}")
    print(f"Gold symbol in 1-hop: {rate('gold_in_1hop')*100:.1f}%")
    print(f"Gold symbol in 2-hop: {rate('gold_in_2hop')*100:.1f}%")
    print(f"Gold symbol in 3-hop: {rate('gold_in_3hop')*100:.1f}%")
    print(f"Best symbol in 1-hop: {rate('best_in_1hop')*100:.1f}%")
    print(f"Best symbol in 2-hop: {rate('best_in_2hop')*100:.1f}%")
    print(f"Best symbol in 3-hop: {rate('best_in_3hop')*100:.1f}%")

    output = {
        "summary": {
            "total": total,
            "gold_in_1hop_rate": rate("gold_in_1hop"),
            "gold_in_2hop_rate": rate("gold_in_2hop"),
            "gold_in_3hop_rate": rate("gold_in_3hop"),
            "best_in_1hop_rate": rate("best_in_1hop"),
            "best_in_2hop_rate": rate("best_in_2hop"),
            "best_in_3hop_rate": rate("best_in_3hop"),
        },
        "per_item": results,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
