#!/usr/bin/env python3
"""
Analyze filtered k-hop neighborhood coverage.

For each failure case in multi-symbol selection, check whether the oracle
best symbol is within the k-hop neighborhood of any LLM-selected symbol,
using a call graph with hub nodes filtered out.

This tests the Surface -> Mechanism hypothesis without shortestPath hub bias.
"""
import json
import re
import sys
from pathlib import Path
from collections import deque

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.core.neo4j_client import run_cypher


def load_hub_nodes(degree_threshold: int = 50) -> set[str]:
    """Load hub node names by degree and known generic patterns."""
    hubs = set()

    # Pattern-based hubs
    generic_patterns = [
        r"^main$",
        r"^find$",
        r"^length$",
        r"^get$",
        r"^pool$",
        r"^data$",
        r"^operator.*",
        r"^ggml_abort$",
        r"^ggml_log_internal$",
        r"^common_log_.*",
        r"^make_test_cases.*",
        r"^LOG.*",
    ]

    result = run_cypher("""
        MATCH (f:Function)
        OPTIONAL MATCH (f)-[:CALLS]->(out:Function)
        OPTIONAL MATCH (in:Function)-[:CALLS]->(f)
        WITH f, count(DISTINCT out) AS out_deg, count(DISTINCT in) AS in_deg
        WHERE (out_deg + in_deg) > $threshold
        RETURN f.name AS name
    """, {"threshold": degree_threshold})

    for row in result:
        name = row.get("name")
        if name:
            hubs.add(name)

    # Also fetch all names to apply pattern matching
    all_names = run_cypher("MATCH (f:Function) RETURN f.name AS name")
    for row in all_names:
        name = row.get("name", "")
        for pat in generic_patterns:
            if re.match(pat, name):
                hubs.add(name)
                break

    return hubs


def get_one_hop_neighbors(symbol: str, hubs: set[str]) -> set[str]:
    """Return 1-hop neighbors excluding hubs."""
    if not symbol:
        return set()
    try:
        result = run_cypher("""
            MATCH (f:Function {name: $name})-[:CALLS]-(n:Function)
            WHERE NOT n.name IN $hubs
            RETURN n.name AS name
        """, {"name": symbol, "hubs": list(hubs)})
        return {row["name"] for row in result if row.get("name")}
    except Exception:
        return set()


def k_hop_neighbors(symbol: str, k: int, hubs: set[str]) -> set[str]:
    """BFS k-hop neighbors excluding hubs."""
    visited = {symbol}
    frontier = {symbol}
    for _ in range(k):
        next_frontier = set()
        for node in frontier:
            neighbors = get_one_hop_neighbors(node, hubs)
            for nb in neighbors:
                if nb not in visited:
                    next_frontier.add(nb)
                    visited.add(nb)
        frontier = next_frontier
        if not frontier:
            break
    return visited - {symbol}


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--multi", type=Path, default=Path("results/multi_symbol_llm_selection_0_15.json"))
    parser.add_argument("--oracle", type=Path, default=Path("results/gold_function_expansion_oracle_0_15.json"))
    parser.add_argument("--degree-threshold", type=int, default=50)
    parser.add_argument("-o", "--output", type=Path, default=Path("results/filtered_neighborhood_coverage_0_15.json"))
    args = parser.parse_args()

    with open(args.multi, "r", encoding="utf-8") as f:
        multi = json.load(f)
    with open(args.oracle, "r", encoding="utf-8") as f:
        oracle = json.load(f)

    multi_by_qa = {r["qa_id"]: r for r in multi["per_item"]}
    oracle_by_qa = {r["qa_id"]: r for r in oracle["per_item"]}

    print(f"Loading hub nodes (degree threshold = {args.degree_threshold})...")
    hubs = load_hub_nodes(args.degree_threshold)
    print(f"Hub nodes: {len(hubs)}")

    analyses = []
    coverage_stats = {1: [], 2: [], 3: []}

    print("\n=== Filtered k-hop neighborhood coverage ===\n")
    for qa_id in sorted(multi_by_qa.keys()):
        m = multi_by_qa[qa_id]
        o = oracle_by_qa.get(qa_id, {})
        best_sym = o.get("best_symbol")
        selected = m.get("selected_symbols", [])
        coverage = m.get("coverage", 0)

        if coverage >= 1.0 or not best_sym:
            continue

        item_analysis = {
            "qa_id": qa_id,
            "best_symbol": best_sym,
            "selected_symbols": selected,
            "coverage": coverage,
        }

        reachable_at = {}
        for k in [1, 2, 3]:
            found = False
            for sel in selected:
                neighbors = k_hop_neighbors(sel, k, hubs)
                if best_sym in neighbors:
                    found = True
                    break
            reachable_at[k] = found
            coverage_stats[k].append(found)

        item_analysis["best_in_filtered_khop"] = reachable_at
        analyses.append(item_analysis)

        print(f"{qa_id}: best={best_sym}")
        print(f"  selected: {selected}")
        print(f"  best in filtered 1-hop: {reachable_at[1]}, 2-hop: {reachable_at[2]}, 3-hop: {reachable_at[3]}")

    total = len(analyses)
    if total > 0:
        print(f"\n{'='*60}")
        print(f"Filtered Neighborhood Coverage (n={total} failure cases)")
        print(f"{'='*60}")
        for k in [1, 2, 3]:
            rate = sum(coverage_stats[k]) / total
            print(f"Best symbol in filtered {k}-hop: {rate*100:.1f}% ({sum(coverage_stats[k])}/{total})")

    output = {
        "summary": {
            "total_failure_cases": total,
            "hub_node_count": len(hubs),
            "filtered_1hop_rate": sum(coverage_stats[1]) / total if total else 0,
            "filtered_2hop_rate": sum(coverage_stats[2]) / total if total else 0,
            "filtered_3hop_rate": sum(coverage_stats[3]) / total if total else 0,
        },
        "per_item": analyses,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
