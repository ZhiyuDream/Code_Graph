#!/usr/bin/env python3
"""
Oracle Multi-Symbol Expansion Ceiling.

For each question, run expand_from_function on:
1. The single best gold symbol (Oracle-1)
2. All gold symbols unioned (Oracle-All)

Compare against LLM-driven multi-symbol selection to separate
selection error from expansion error.
"""
import json
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from config import get_repo_root
from src.qa.investigation.base import BaseInvestigator
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


def run_item(item: dict, oracle_item: dict, repo_path: str,
             top_n: int, files_per_symbol: int) -> dict:
    gold_files_norm = {normalize_path(g) for g in item["gold_files"]}

    # Oracle-1: best single gold symbol
    best_sym = oracle_item["best_symbol"]
    best_visited = expand_from_function(best_sym, repo_path, top_n, files_per_symbol)
    best_coverage = len(gold_files_norm & best_visited) / len(gold_files_norm) if gold_files_norm else 1.0

    # Oracle-All: union of all gold symbols
    all_visited = set()
    per_symbol = []
    for sym in oracle_item["gold_symbols"]:
        visited = expand_from_function(sym, repo_path, top_n, files_per_symbol)
        all_visited.update(visited)
        per_symbol.append({
            "symbol": sym,
            "coverage": len(gold_files_norm & visited) / len(gold_files_norm) if gold_files_norm else 1.0,
            "visited_count": len(visited),
        })
    all_coverage = len(gold_files_norm & all_visited) / len(gold_files_norm) if gold_files_norm else 1.0

    return {
        "qa_id": item["qa_id"],
        "gold_files": item["gold_files"],
        "best_symbol": best_sym,
        "best_coverage": best_coverage,
        "best_visited_count": len(best_visited),
        "all_coverage": all_coverage,
        "all_visited_count": len(all_visited),
        "per_symbol": per_symbol,
    }


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, default=Path("datasets/benchmark_hard.json"))
    parser.add_argument("--oracle", type=Path, default=Path("results/gold_function_expansion_oracle_0_15.json"))
    parser.add_argument("--range", default="0,15")
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument("--files-per-symbol", type=int, default=3)
    parser.add_argument("-w", "--workers", type=int, default=4)
    parser.add_argument("-o", "--output", type=Path, default=Path("results/oracle_multi_symbol_expansion_0_15.json"))
    args = parser.parse_args()

    items = load_items(args.benchmark, args.range)
    with open(args.oracle, "r", encoding="utf-8") as f:
        oracle = json.load(f)
    oracle_by_qa = {r["qa_id"]: r for r in oracle["per_item"]}
    repo_path = str(get_repo_root())

    results = []
    completed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(run_item, item, oracle_by_qa[item["qa_id"]], repo_path,
                            args.top_n, args.files_per_symbol): item
            for item in items
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            completed += 1
            print(f"  [{completed}/{len(items)}] {result['qa_id']}: "
                  f"best={result['best_coverage']*100:.0f}% "
                  f"all={result['all_coverage']*100:.0f}% "
                  f"(best_vis={result['best_visited_count']}, all_vis={result['all_visited_count']})")

    results.sort(key=lambda x: x["qa_id"])
    total = len(results)
    avg_best = sum(r["best_coverage"] for r in results) / total
    avg_all = sum(r["all_coverage"] for r in results) / total
    full_best = sum(1 for r in results if r["best_coverage"] >= 1.0)
    full_all = sum(1 for r in results if r["all_coverage"] >= 1.0)

    print(f"\n{'='*60}")
    print("Oracle Multi-Symbol Expansion Ceiling")
    print(f"{'='*60}")
    print(f"总题数: {total}")
    print(f"Oracle-Best 平均 Coverage: {avg_best*100:.1f}%")
    print(f"Oracle-Best 完全覆盖: {full_best}/{total} ({full_best/total*100:.1f}%)")
    print(f"Oracle-All 平均 Coverage: {avg_all*100:.1f}%")
    print(f"Oracle-All 完全覆盖: {full_all}/{total} ({full_all/total*100:.1f}%)")

    output = {
        "summary": {
            "total": total,
            "avg_best_coverage": avg_best,
            "full_best": full_best,
            "avg_all_coverage": avg_all,
            "full_all": full_all,
        },
        "per_item": results,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
