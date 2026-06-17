#!/usr/bin/env python3
"""
Analyze graph distance between LLM-selected symbols and oracle gold symbols.

For each failure case in multi-symbol selection, compute:
- Shortest path distance in the call graph (undirected)
- Whether oracle symbol is a caller/callee of selected symbol
- Whether they are in the same file / same module
"""
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.core.neo4j_client import run_cypher


def graph_distance(name1: str, name2: str) -> int | None:
    """Return undirected shortest path distance between two functions."""
    try:
        result = run_cypher("""
            MATCH (a:Function {name: $name1}), (b:Function {name: $name2})
            MATCH p = shortestPath((a)-[:CALLS*]-(b))
            RETURN length(p) AS distance
        """, {"name1": name1, "name2": name2})
        if result:
            return result[0]["distance"]
    except Exception as e:
        # likely no path or one node missing
        pass
    return None


def is_caller_callee(name1: str, name2: str) -> dict:
    """Check if name1 calls name2, name2 calls name1, or both."""
    rel = {"name1_calls_name2": False, "name2_calls_name1": False}
    try:
        r1 = run_cypher("""
            MATCH (a:Function {name: $n1})-[:CALLS]->(b:Function {name: $n2})
            RETURN count(*) AS cnt
        """, {"n1": name1, "n2": name2})
        rel["name1_calls_name2"] = r1[0]["cnt"] > 0

        r2 = run_cypher("""
            MATCH (a:Function {name: $n2})-[:CALLS]->(b:Function {name: $n1})
            RETURN count(*) AS cnt
        """, {"n1": name1, "n2": name2})
        rel["name2_calls_name1"] = r2[0]["cnt"] > 0
    except Exception:
        pass
    return rel


def get_function_info(name: str) -> dict:
    try:
        result = run_cypher("""
            MATCH (f:Function {name: $name})
            RETURN f.file_path AS file, f.name AS name
            LIMIT 1
        """, {"name": name})
        if result:
            return dict(result[0])
    except Exception:
        pass
    return {}


def same_module(path1: str, path2: str) -> bool:
    if not path1 or not path2:
        return False
    parts1 = path1.split('/')
    parts2 = path2.split('/')
    return parts1 and parts2 and parts1[0] == parts2[0]


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--multi", type=Path, default=Path("results/multi_symbol_llm_selection_0_15.json"))
    parser.add_argument("--oracle", type=Path, default=Path("results/gold_function_expansion_oracle_0_15.json"))
    parser.add_argument("-o", "--output", type=Path, default=Path("results/symbol_graph_distance_analysis_0_15.json"))
    args = parser.parse_args()

    with open(args.multi, "r", encoding="utf-8") as f:
        multi = json.load(f)
    with open(args.oracle, "r", encoding="utf-8") as f:
        oracle = json.load(f)

    multi_by_qa = {r["qa_id"]: r for r in multi["per_item"]}
    oracle_by_qa = {r["qa_id"]: r for r in oracle["per_item"]}

    analyses = []
    distances = []

    print("=== Graph distance: LLM-selected vs Oracle gold symbols ===\n")
    for qa_id in sorted(multi_by_qa.keys()):
        m = multi_by_qa[qa_id]
        o = oracle_by_qa.get(qa_id, {})
        if not o or not o.get("gold_symbols"):
            continue

        best_sym = o["best_symbol"]
        selected = m.get("selected_symbols", [])
        coverage = m.get("coverage", 0)

        if coverage >= 1.0:
            continue  # only analyze failures

        item_analysis = {
            "qa_id": qa_id,
            "best_symbol": best_sym,
            "selected_symbols": selected,
            "coverage": coverage,
            "selected_analyses": [],
        }

        best_info = get_function_info(best_sym)

        for sel in selected:
            sel_info = get_function_info(sel)
            dist = graph_distance(sel, best_sym)
            rel = is_caller_callee(sel, best_sym)

            analysis = {
                "selected": sel,
                "selected_file": sel_info.get("file"),
                "best_file": best_info.get("file"),
                "distance": dist,
                "selected_calls_best": rel["name1_calls_name2"],
                "best_calls_selected": rel["name2_calls_name1"],
                "same_file": sel_info.get("file") == best_info.get("file"),
                "same_module": same_module(sel_info.get("file", ""), best_info.get("file", "")),
            }
            item_analysis["selected_analyses"].append(analysis)
            if dist is not None:
                distances.append(dist)

            print(f"{qa_id}: {sel} → {best_sym}")
            print(f"  distance={dist}, same_file={analysis['same_file']}, same_module={analysis['same_module']}")
            print(f"  {sel}_calls_{best_sym}={analysis['selected_calls_best']}, {best_sym}_calls_{sel}={analysis['best_calls_selected']}")

        analyses.append(item_analysis)

    if distances:
        avg_dist = sum(distances) / len(distances)
        print(f"\n平均距离: {avg_dist:.2f} (n={len(distances)})")
        print(f"距离分布: {sorted(distances)}")
    else:
        print("\n无有效距离数据")

    output = {
        "summary": {
            "avg_distance": sum(distances) / len(distances) if distances else None,
            "distance_distribution": sorted(distances),
        },
        "per_item": analyses,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
