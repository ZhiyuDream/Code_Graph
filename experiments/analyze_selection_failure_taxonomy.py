#!/usr/bin/env python3
"""
Selection Failure Taxonomy for repository QA.

Inputs:
  - retrieval_selection_decomposition results
  - gold_rank_in_pool results
  - benchmark (for gold evidence file paths)
  - embedding index (for symbol -> file_path mapping)

Outputs:
  - Per-question taxonomy table (JSON + Markdown)
  - Aggregate statistics

Categories (priority order):
  1. correct             : LLM hit gold in single or multi selection
  2. retrieval_miss      : gold symbol not in candidate pool
  3. same_file           : selected symbol is in the same file as gold evidence
  4. same_module         : selected symbol is in the same module/directory as gold
  5. abstraction_shift   : selected is more generic/specific abstraction (register/init/apply/verify/...)
  6. test_or_utility     : selected is test function or generic utility
  7. semantic_neighbor   : related by topic/naming but no direct structural link
  8. unrelated           : no clear relation
"""
import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from config import get_repo_root


def normalize_path(path: str) -> str:
    if not path:
        return ""
    repo_root = str(get_repo_root())
    if path.startswith(repo_root):
        path = path[len(repo_root):].lstrip('/')
    return path.lstrip('./').lstrip('/')


def get_module(file_path: str) -> str:
    """Return top-level module/dir."""
    parts = file_path.split('/')
    if len(parts) == 0:
        return ""
    if parts[0] == "ggml" and len(parts) >= 3 and parts[1] == "src":
        return '/'.join(parts[:3])
    return parts[0]


class SymbolFileResolver:
    def __init__(self, index_path: Path | None = None):
        if index_path is None:
            index_path = Path(__file__).resolve().parent.parent / "data" / "qa_embedding_index.json"
        with open(index_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.symbol_to_files: dict[str, set[str]] = {}
        for ch in data.get("chunks", []):
            name = ch.get("meta", {}).get("name", "")
            fp = ch.get("meta", {}).get("file_path", "")
            if name and fp:
                self.symbol_to_files.setdefault(name, set()).add(normalize_path(fp))

    def resolve(self, symbol: str, preferred_files: set[str] | None = None) -> str:
        files = self.symbol_to_files.get(symbol, set())
        if not files:
            return ""
        if preferred_files:
            intersect = files & preferred_files
            if intersect:
                return min(intersect)
        # Prefer .cpp over .h, shorter path
        cpp = [f for f in files if f.endswith(".cpp")]
        if cpp:
            return min(cpp)
        return min(files)


def classify(selected: str, selected_file: str, gold_symbols: set[str],
             gold_files: set[str], pool_recall: bool) -> dict:
    if selected in gold_symbols:
        return {"category": "correct", "detail": "LLM selected gold symbol"}

    if not pool_recall:
        return {"category": "retrieval_miss", "detail": "gold symbol not in candidate pool"}

    if selected_file:
        if selected_file in gold_files:
            return {"category": "same_file", "detail": f"{selected} in same file as gold"}
        selected_module = get_module(selected_file)
        for gf in gold_files:
            if selected_module and selected_module == get_module(gf):
                return {"category": "same_module", "detail": f"{selected} in same module as gold ({selected_module})"}

    name_lower = selected.lower()
    abstraction_markers = ["register", "init", "apply", "verify", "backend", "common_",
                           "get_", "set_", "free", "destroy", "create"]
    if any(m in name_lower for m in abstraction_markers):
        return {"category": "abstraction_shift", "detail": f"{selected} appears more generic/specific abstraction"}

    if name_lower.startswith("test_") or "util" in name_lower or "helper" in name_lower:
        return {"category": "test_or_utility", "detail": f"{selected} is test/utility function"}

    return {"category": "semantic_neighbor", "detail": f"{selected} is semantic neighbor of gold"}


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--decomposition", type=Path, default=Path("results/retrieval_selection_decomposition_0_50.json"))
    parser.add_argument("--gold-rank", type=Path, default=Path("results/gold_rank_in_pool_0_50.json"))
    parser.add_argument("--benchmark", type=Path, default=Path("datasets/benchmark_hard.json"))
    parser.add_argument("-o", "--output", type=Path, default=Path("results/selection_failure_taxonomy_0_50.json"))
    parser.add_argument("--md", type=Path, default=Path("docs/research/selection_failure_taxonomy_0_50.md"))
    args = parser.parse_args()

    if not args.decomposition.exists():
        print(f"Decomposition file not found: {args.decomposition}")
        return

    with open(args.decomposition, "r", encoding="utf-8") as f:
        dec = json.load(f)

    pool_rank_data = {}
    if args.gold_rank.exists():
        with open(args.gold_rank, "r", encoding="utf-8") as f:
            gr = json.load(f)
        pool_rank_data = {r["qa_id"]: r for r in gr.get("per_item", [])}

    bench_by_qa = {}
    if args.benchmark.exists():
        with open(args.benchmark, "r", encoding="utf-8") as f:
            bench = json.load(f)
        for it in bench.get("items", []):
            qa_id = it.get("qa_id")
            gold_files = set()
            for ev in it.get("gold_evidence", []):
                fp = ev.get("file", "")
                if fp and not fp.endswith((".h", ".hpp")):
                    gold_files.add(normalize_path(fp))
            bench_by_qa[qa_id] = {
                "gold_files": gold_files,
                "gold_symbols": set(ev.get("symbol", "") for ev in it.get("gold_evidence", [])),
            }

    resolver = SymbolFileResolver()

    records = []
    for item in dec["per_item"]:
        qa_id = item["qa_id"]
        gold_symbols = set(item.get("gold_symbols", []))
        gold_files = bench_by_qa.get(qa_id, {}).get("gold_files", set())

        # Try enrich gold_files from resolver
        for sym in gold_symbols:
            gf = resolver.resolve(sym)
            if gf:
                gold_files.add(gf)

        single_selected = item.get("single_selected", "")
        single_file = item.get("single_selected_file", "")
        if not single_file:
            single_file = resolver.resolve(single_selected, gold_files)

        cat_info = classify(single_selected, single_file, gold_symbols, gold_files, item.get("pool_recall", False))

        records.append({
            "qa_id": qa_id,
            "question": item.get("question", ""),
            "gold_symbols": sorted(gold_symbols),
            "gold_files": sorted(gold_files),
            "single_selected": single_selected,
            "single_selected_file": single_file,
            "multi_selected": item.get("multi_selected", []),
            "pool_recall": item.get("pool_recall", False),
            "gold_rank": pool_rank_data.get(qa_id, {}).get("gold_rank"),
            "category": cat_info["category"],
            "detail": cat_info["detail"],
        })

    counter = Counter(r["category"] for r in records)
    total = len(records)

    print("\nSelection Failure Taxonomy")
    print(f"{'='*60}")
    for cat, count in counter.most_common():
        print(f"{cat:22s}: {count:3d} / {total} = {count/total*100:5.1f}%")
    print(f"{'='*60}")

    output = {
        "summary": dict(counter),
        "summary_rate": {k: v/total for k, v in counter.items()},
        "per_item": records,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # Markdown report
    lines = [
        "# Selection Failure Taxonomy (50 questions)",
        "",
        f"Based on decomposition: `{args.decomposition}`",
        f"and gold rank analysis: `{args.gold_rank}`.",
        "",
        "## Aggregate Statistics",
        "",
        "| Category | Count | Rate |",
        "|----------|-------|------|",
    ]
    for cat, count in counter.most_common():
        lines.append(f"| {cat} | {count} | {count/total*100:.1f}% |")
    lines.append("")
    lines.append("## Per-Question Detail")
    lines.append("")
    lines.append("| QA ID | Gold Rank | Category | Gold Symbol(s) | LLM Selected | Detail |")
    lines.append("|-------|-----------|----------|----------------|--------------|--------|")
    for r in records:
        rank = str(r["gold_rank"]) if r["gold_rank"] else "MISS"
        gold_syms = ", ".join(r["gold_symbols"]) or "-"
        lines.append(
            f"| {r['qa_id']} | {rank} | {r['category']} | {gold_syms} | "
            f"`{r['single_selected']}` | {r['detail']} |"
        )

    args.md.parent.mkdir(parents=True, exist_ok=True)
    with open(args.md, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"\nJSON saved: {args.output}")
    print(f"Markdown saved: {args.md}")


if __name__ == "__main__":
    main()
