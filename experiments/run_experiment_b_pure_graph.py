#!/usr/bin/env python3
"""
Experiment B: Pure graph expansion from embedding seeds.

No LLM selection. Just:
1. Embedding Top-K candidates as seeds
2. Graph-expand each seed by N hops (callers + callees)
3. Expand all reachable symbols
4. Union coverage

This measures how far structure alone can go.
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
from src.search.code_reader import read_full_file
from src.search.grep_search_v2 import grep_files


def normalize_path(path: str) -> str:
    repo_root = str(get_repo_root())
    if path.startswith(repo_root):
        path = path[len(repo_root):].lstrip('/')
    return path.lstrip('./')


def file_priority(path: str) -> int:
    p = path.lower()
    if "/src/" in p or p.startswith("src/"):
        return 100
    if "/common/" in p or p.startswith("common/"):
        return 90
    if "/ggml/src/" in p or p.startswith("ggml/src/"):
        return 85
    if "/include/" in p or p.startswith("include/"):
        return 50
    if "/tests/" in p or p.startswith("tests/"):
        return 20
    if "/examples/" in p or p.startswith("examples/"):
        return 10
    if "/tools/" in p or p.startswith("tools/"):
        return 30
    return 40


def select_definition_file(files: list[str]) -> str | None:
    if not files:
        return None
    return max(files, key=lambda f: (file_priority(f), -len(f)))


def expand_from_function(entry_function: str, repo_path: str,
                         callees_top_k: int, files_per_symbol: int) -> set[str]:
    from collections import Counter
    all_occurrences = grep_files(entry_function, repo_path, limit=100)
    visited = {normalize_path(f) for f in all_occurrences}
    definition_file = select_definition_file(list(visited))
    if definition_file:
        try:
            content = read_full_file(definition_file)
        except Exception:
            content = ""
        if content:
            from src.qa.investigation.base import BaseInvestigator
            investigator = BaseInvestigator.__new__(BaseInvestigator)
            symbols = investigator.extract_symbols(content)
            counter = Counter(symbols)
            callee_symbols = [sym for sym, _ in counter.most_common(callees_top_k)
                              if sym != entry_function]
            for sym in callee_symbols:
                files = grep_files(sym, repo_path, limit=50)
                selected = {normalize_path(f) for f in files[:files_per_symbol]}
                visited.update(selected)
    return visited


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
                # callers
                r1 = run_cypher("""
                    MATCH (caller:Function)-[:CALLS]->(callee:Function {name: $name})
                    RETURN caller.name AS name LIMIT $limit
                """, {"name": sym, "limit": limit_per_node})
                # callees
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


def run_item(item: dict, retriever: EmbeddingRetriever, repo_path: str,
             top_k: int, hops: int, top_n: int, files_per_symbol: int,
             graph_limit: int) -> dict:
    results = retriever.retrieve(item["question"], top_k=top_k)
    seeds = []
    seen = set()
    for r in results:
        meta = r.metadata
        name = meta.get("name", "")
        if name and name not in seen:
            seen.add(name)
            seeds.append(name)

    # Graph expand
    reachable = graph_expand(seeds, hops=hops, limit_per_node=graph_limit)

    # Expand all reachable symbols
    union_visited = set()
    for sym in reachable:
        visited = expand_from_function(sym, repo_path, top_n, files_per_symbol)
        union_visited.update(visited)

    gold_norm = {normalize_path(g) for g in item["gold_files"]}
    union_norm = {normalize_path(v) for v in union_visited}
    coverage = len(gold_norm & union_norm) / len(gold_norm) if gold_norm else 1.0

    return {
        "qa_id": item["qa_id"],
        "gold_files": item["gold_files"],
        "num_seeds": len(seeds),
        "reachable_symbols": len(reachable),
        "union_visited_count": len(union_visited),
        "coverage": coverage,
    }


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, default=Path("datasets/benchmark_hard.json"))
    parser.add_argument("--range", default="0,15")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--hops", type=int, default=2)
    parser.add_argument("--graph-limit", type=int, default=10)
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument("--files-per-symbol", type=int, default=3)
    parser.add_argument("-w", "--workers", type=int, default=2)
    parser.add_argument("-o", "--output", type=Path, default=Path("results/experiment_b_pure_graph_2hop_0_15.json"))
    args = parser.parse_args()

    items = load_items(args.benchmark, args.range)
    retriever = EmbeddingRetriever()
    repo_path = str(get_repo_root())

    results = []
    completed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(run_item, item, retriever, repo_path,
                            args.top_k, args.hops, args.top_n, args.files_per_symbol,
                            args.graph_limit): item
            for item in items
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            completed += 1
            print(f"  [{completed}/{len(items)}] {result['qa_id']}: "
                  f"seeds={result['num_seeds']} reachable={result['reachable_symbols']} "
                  f"visited={result['union_visited_count']} cov={result['coverage']*100:.0f}%")

    results.sort(key=lambda x: x["qa_id"])
    total = len(results)
    avg_coverage = sum(r["coverage"] for r in results) / total
    full = sum(1 for r in results if r["coverage"] >= 1.0)
    avg_reachable = sum(r["reachable_symbols"] for r in results) / total
    avg_visited = sum(r["union_visited_count"] for r in results) / total

    print(f"\n{'='*60}")
    print(f"Experiment B: Pure Graph Expansion ({args.hops}-hop)")
    print(f"{'='*60}")
    print(f"总题数: {total}")
    print(f"平均 Coverage: {avg_coverage*100:.1f}%")
    print(f"完全覆盖: {full}/{total} ({full/total*100:.1f}%)")
    print(f"平均 Reachable Symbols: {avg_reachable:.1f}")
    print(f"平均 Union 文件数: {avg_visited:.1f}")

    output = {
        "summary": {
            "total": total,
            "hops": args.hops,
            "avg_coverage": avg_coverage,
            "full_correct": full,
            "avg_reachable": avg_reachable,
            "avg_visited": avg_visited,
        },
        "per_item": results,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
