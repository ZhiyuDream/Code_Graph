#!/usr/bin/env python3
"""
Analyze module distance between LLM-selected symbols and oracle gold symbols.

Check:
1. Same file
2. Same directory (1-level, 2-level, 3-level)
3. Same community (if available in Neo4j)
"""
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.core.neo4j_client import run_cypher


def get_function_info(symbol: str) -> dict:
    """Return file_path and community for a function."""
    try:
        result = run_cypher("""
            MATCH (f:Function {name: $name})
            RETURN f.file_path AS file, f.community AS community
            LIMIT 1
        """, {"name": symbol})
        if result:
            return dict(result[0])
    except Exception:
        pass
    return {"file": "", "community": None}


def same_directory(path1: str, path2: str, levels: int) -> bool:
    if not path1 or not path2:
        return False
    parts1 = Path(path1).parts
    parts2 = Path(path2).parts
    if len(parts1) < levels or len(parts2) < levels:
        return False
    return parts1[:levels] == parts2[:levels]


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--multi", type=Path, default=Path("results/multi_symbol_llm_selection_0_15.json"))
    parser.add_argument("--oracle", type=Path, default=Path("results/gold_function_expansion_oracle_0_15.json"))
    parser.add_argument("-o", "--output", type=Path, default=Path("results/module_distance_analysis_0_15.json"))
    args = parser.parse_args()

    with open(args.multi, "r", encoding="utf-8") as f:
        multi = json.load(f)
    with open(args.oracle, "r", encoding="utf-8") as f:
        oracle = json.load(f)

    multi_by_qa = {r["qa_id"]: r for r in multi["per_item"]}
    oracle_by_qa = {r["qa_id"]: r for r in oracle["per_item"]}

    analyses = []
    stats = {
        "same_file": [],
        "same_dir_1": [],
        "same_dir_2": [],
        "same_dir_3": [],
        "same_community": [],
    }

    print("=== Module distance: selected vs gold symbols ===\n")
    for qa_id in sorted(multi_by_qa.keys()):
        m = multi_by_qa[qa_id]
        o = oracle_by_qa.get(qa_id, {})
        best_sym = o.get("best_symbol")
        selected = m.get("selected_symbols", [])
        coverage = m.get("coverage", 0)

        if coverage >= 1.0 or not best_sym:
            continue

        best_info = get_function_info(best_sym)
        best_file = best_info.get("file", "")
        best_community = best_info.get("community")

        item_analysis = {
            "qa_id": qa_id,
            "best_symbol": best_sym,
            "best_file": best_file,
            "best_community": best_community,
            "selected_symbols": [],
            "coverage": coverage,
        }

        same_file = False
        same_dir_1 = False
        same_dir_2 = False
        same_dir_3 = False
        same_community = False

        for sel in selected:
            sel_info = get_function_info(sel)
            sel_file = sel_info.get("file", "")
            sel_community = sel_info.get("community")

            item_analysis["selected_symbols"].append({
                "name": sel,
                "file": sel_file,
                "community": sel_community,
            })

            if best_file and sel_file and best_file == sel_file:
                same_file = True
            if same_directory(best_file, sel_file, 1):
                same_dir_1 = True
            if same_directory(best_file, sel_file, 2):
                same_dir_2 = True
            if same_directory(best_file, sel_file, 3):
                same_dir_3 = True
            if best_community is not None and sel_community is not None and best_community == sel_community:
                same_community = True

        item_analysis["same_file"] = same_file
        item_analysis["same_dir_1"] = same_dir_1
        item_analysis["same_dir_2"] = same_dir_2
        item_analysis["same_dir_3"] = same_dir_3
        item_analysis["same_community"] = same_community

        for key in stats:
            stats[key].append(item_analysis[key])

        analyses.append(item_analysis)

        print(f"{qa_id}: best={best_sym} ({best_file})")
        print(f"  same_file={same_file}, same_dir_1={same_dir_1}, same_dir_2={same_dir_2}, same_dir_3={same_dir_3}, same_community={same_community}")
        for s in item_analysis["selected_symbols"]:
            print(f"    {s['name']}: {s['file']}")

    total = len(analyses)
    if total > 0:
        print(f"\n{'='*60}")
        print(f"Module Distance Summary (n={total} failure cases)")
        print(f"{'='*60}")
        for key in stats:
            rate = sum(stats[key]) / total
            print(f"{key}: {rate*100:.1f}% ({sum(stats[key])}/{total})")

    output = {
        "summary": {
            "total_failure_cases": total,
            "same_file_rate": sum(stats["same_file"]) / total if total else 0,
            "same_dir_1_rate": sum(stats["same_dir_1"]) / total if total else 0,
            "same_dir_2_rate": sum(stats["same_dir_2"]) / total if total else 0,
            "same_dir_3_rate": sum(stats["same_dir_3"]) / total if total else 0,
            "same_community_rate": sum(stats["same_community"]) / total if total else 0,
        },
        "per_item": analyses,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
