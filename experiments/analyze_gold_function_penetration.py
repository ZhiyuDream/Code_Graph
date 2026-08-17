"""Three-layer penetration analysis for gold evidence functions.

For each gold evidence entry (function-level):
  Stage 0: Was the containing file visited by the worklist?
  Stage 1: Was the specific function retrieved into top-K functions?
  Stage 2: Was the containing file cited in the answer?

Outputs:
  - Overall A/B/C/D distribution
  - Per-item breakdown
  - Representative examples for each category
"""
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BENCH_PATH = ROOT / "datasets" / "benchmark_hard.json"
WORKLIST_PATH = ROOT / "results" / "symgraph_worklist_hard_L100_K20_E5_M15_H2_S5.json"
V3_PATH = ROOT / "results" / "worklist_e2e_v3_symgraph_worklist_hard_L100_K20_E5_M15_H2_S5.json"


def normalize_path(path: str) -> str:
    return path.lstrip("./").lstrip("/")


def load_function_index() -> dict:
    """Load embedding index and build (file_path, function_name) -> line range mapping."""
    with open(ROOT / "data" / "qa_embedding_index.json") as f:
        data = json.load(f)
    func_index = {}
    for ch in data.get("chunks", []):
        meta = ch.get("meta", {})
        fp = meta.get("file_path", "")
        name = meta.get("name", "")
        func_index[(fp, name)] = {
            "start_line": meta.get("start_line", 0),
            "end_line": meta.get("end_line", 0),
            "text": ch.get("text", ""),
        }
    return func_index


def match_gold_to_function(gold: dict, func_entries: list[dict], func_index: dict) -> dict | None:
    """Find the retrieved function entry that contains this gold evidence.

    Match by:
    1. file_path match + line range overlap with embedding index function
    2. file_path match + gold symbol equals function name
    """
    gold_file = normalize_path(gold["file"])
    gold_symbol = gold.get("symbol", "")
    gold_start = gold.get("line_start", 0)
    gold_end = gold.get("line_end", 0)

    # First, try to find the canonical function from index by line range
    canonical_name = None
    for (fp, name), info in func_index.items():
        if normalize_path(fp) == gold_file:
            if gold_start and gold_end:
                # Check overlap
                if not (info["end_line"] < gold_start or info["start_line"] > gold_end):
                    canonical_name = name
                    break
            elif gold_symbol and name == gold_symbol:
                canonical_name = name
                break

    # Now check if this function (or any matching function) is in retrieved entries
    for f in func_entries:
        f_file = normalize_path(f["file_path"])
        f_name = f["name"]
        if f_file == gold_file:
            # Direct name match
            if gold_symbol and (f_name == gold_symbol or f_name in gold_symbol or gold_symbol in f_name):
                return f
            # Line range match
            f_start = f.get("start_line", 0)
            f_end = f.get("end_line", 0)
            if gold_start and gold_end and f_start and f_end:
                if not (f_end < gold_start or f_start > gold_end):
                    return f
            # Canonical name match
            if canonical_name and (f_name == canonical_name or f_name in canonical_name or canonical_name in f_name):
                return f
    return None


def main():
    with open(BENCH_PATH) as f:
        bench = json.load(f)["items"]
    with open(WORKLIST_PATH) as f:
        worklist = json.load(f)["per_item"]
    with open(V3_PATH) as f:
        v3 = json.load(f)["per_item"]

    wl_map = {x["qa_id"]: x for x in worklist}
    v3_map = {x["qa_id"]: x for x in v3}
    func_index = load_function_index()

    categories = Counter()
    category_examples = defaultdict(list)
    item_stats = []

    for i, bench_item in enumerate(bench):
        qa_id = bench_item["qa_id"]
        wl_item = wl_map[qa_id]
        v3_item = v3_map[qa_id]

        visited_files = set(normalize_path(f) for f in wl_item.get("visited_files", []))
        func_entries = v3_item.get("function_entries", [])
        cited_files = set(normalize_path(f) for f in v3_item.get("cited_files", []))

        item_cat = Counter()
        for ev in bench_item.get("gold_evidence", []):
            if ev["file"].endswith((".h", ".hpp")):
                continue

            gold_file = normalize_path(ev["file"])
            matched_func = match_gold_to_function(ev, func_entries, func_index)

            # Stage 0: file visited?
            if gold_file not in visited_files:
                cat = "A_file_not_visited"
            elif matched_func is None:
                cat = "B_file_visited_func_not_retrieved"
            elif gold_file not in cited_files:
                cat = "C_func_retrieved_not_cited"
            else:
                cat = "D_cited"

            categories[cat] += 1
            item_cat[cat] += 1

            example = {
                "qa_id": qa_id,
                "gold_file": gold_file,
                "gold_symbol": ev.get("symbol", ""),
                "gold_lines": f"{ev.get('line_start', 0)}-{ev.get('line_end', 0)}",
                "matched_function": f"{matched_func['file_path']}:{matched_func['name']}" if matched_func else None,
                "matched_function_rank": None,
            }
            if matched_func:
                for rank, f in enumerate(func_entries):
                    if f["file_path"] == matched_func["file_path"] and f["name"] == matched_func["name"]:
                        example["matched_function_rank"] = rank
                        break

            category_examples[cat].append(example)

        item_stats.append({
            "qa_id": qa_id,
            "categories": dict(item_cat),
            "total_gold": sum(item_cat.values()),
        })

    total = sum(categories.values())
    print("=" * 70)
    print("GOLD FUNCTION THREE-LAYER PENETRATION ANALYSIS")
    print("=" * 70)
    print(f"\nTotal gold function entries (excluding .h/.hpp): {total}\n")
    print(f'{"Category":<45} {"Count":>8} {"Pct":>8}')
    print("-" * 70)
    for cat, count in categories.most_common():
        pct = count / total * 100 if total else 0
        print(f"{cat:<45} {count:>8} {pct:>7.1f}%")

    print("\n" + "=" * 70)
    print("PER-ITEM BREAKDOWN (items with any failure)")
    print("=" * 70)
    for stat in item_stats:
        cats = stat["categories"]
        if cats.get("D_cited", 0) == stat["total_gold"]:
            continue
        print(f"\n{stat['qa_id']} (total {stat['total_gold']} gold functions)")
        for cat, count in cats.items():
            print(f"  {cat}: {count}")

    print("\n" + "=" * 70)
    print("REPRESENTATIVE EXAMPLES")
    print("=" * 70)
    for cat in ["A_file_not_visited", "B_file_visited_func_not_retrieved", "C_func_retrieved_not_cited"]:
        print(f"\n--- {cat} (showing up to 5) ---")
        for ex in category_examples[cat][:5]:
            print(f"  {ex['qa_id']}")
            print(f"    gold: {ex['gold_file']}:{ex['gold_symbol']} lines {ex['gold_lines']}")
            if ex['matched_function']:
                print(f"    matched func: {ex['matched_function']} (rank {ex['matched_function_rank']})")
            print()

    # Save detailed results
    out = {
        "summary": {
            "total_gold_entries": total,
            "categories": dict(categories),
        },
        "per_item": item_stats,
        "examples": {k: v[:20] for k, v in category_examples.items()},
    }
    out_path = ROOT / "results" / "gold_function_penetration_analysis.json"
    with open(out_path, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\nSaved detailed results to {out_path}")


if __name__ == "__main__":
    main()
