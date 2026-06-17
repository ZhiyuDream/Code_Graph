#!/usr/bin/env python3
"""
Gold Function Expansion Oracle。

对每道题的每个 gold symbol，分别做 symbol expansion，测量能覆盖多少 gold files。

目的：区分 "Root Function"（最佳扩展入口）和 "Supporting Evidence"（普通证据节点）。

输出每题：
- 每个 gold symbol 的 expansion coverage
- 最佳 gold symbol 及其 coverage
- 平均 coverage

用法:
    python experiments/run_gold_function_expansion_oracle.py \
        --benchmark datasets/benchmark_hard.json \
        --range 0,15 \
        --callees-top-k 5 \
        --files-per-symbol 3 \
        -w 4 \
        -o results/gold_function_expansion_oracle_0_15.json
"""
import json
import sys
from pathlib import Path
from collections import Counter
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
        gold_symbols = sorted(set(
            ev.get("symbol", "") for ev in item.get("gold_evidence", [])
            if ev.get("symbol") and not ev["file"].endswith((".h", ".hpp"))
        ))
        selected.append({
            "qa_id": item.get("qa_id", f"q{idx}"),
            "question": item.get("question", ""),
            "gold_files": gold_files,
            "gold_symbols": gold_symbols,
        })
    return selected


def compute_coverage(visited: set[str], gold_files: list[str]) -> float:
    if not gold_files:
        return 1.0
    norm_visited = {normalize_path(v) for v in visited}
    norm_gold = {normalize_path(g) for g in gold_files}
    hit = len(norm_visited & norm_gold)
    return hit / len(norm_gold)


def file_priority(fp: str) -> int:
    p = fp.lower()
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


def expand_from_symbol(entry_symbol: str, repo_path: str,
                       callees_top_k: int, files_per_symbol: int) -> set[str]:
    """从一个 symbol 出发做 1-step upstream + 1-step downstream expansion。"""
    # Upstream: all occurrences (callers)
    all_occurrences = grep_files(entry_symbol, repo_path, limit=100)
    visited = {normalize_path(f) for f in all_occurrences}

    # Downstream: read definition file, extract callees, grep them
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
                              if sym != entry_symbol]
            for sym in callee_symbols:
                files = grep_files(sym, repo_path, limit=50)
                selected = {normalize_path(f) for f in files[:files_per_symbol]}
                visited.update(selected)

    return visited


def run_item(item: dict, repo_path: str, callees_top_k: int, files_per_symbol: int) -> dict:
    symbol_results = []
    for symbol in item["gold_symbols"]:
        visited = expand_from_symbol(symbol, repo_path, callees_top_k, files_per_symbol)
        coverage = compute_coverage(visited, item["gold_files"])
        symbol_results.append({
            "symbol": symbol,
            "coverage": coverage,
            "visited_files": sorted(visited),
        })

    if symbol_results:
        best = max(symbol_results, key=lambda x: x["coverage"])
        avg_coverage = sum(r["coverage"] for r in symbol_results) / len(symbol_results)
    else:
        best = {"symbol": "", "coverage": 0.0, "visited_files": []}
        avg_coverage = 0.0

    return {
        "qa_id": item["qa_id"],
        "question": item["question"],
        "gold_files": item["gold_files"],
        "gold_symbols": item["gold_symbols"],
        "symbol_results": symbol_results,
        "best_symbol": best["symbol"],
        "best_coverage": best["coverage"],
        "avg_coverage": avg_coverage,
    }


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--range", default="0,15")
    parser.add_argument("--callees-top-k", type=int, default=5)
    parser.add_argument("--files-per-symbol", type=int, default=3)
    parser.add_argument("-w", "--workers", type=int, default=4)
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args()

    items = load_items(args.benchmark, args.range)
    print(f"加载 {len(items)} 题，callees_top_k={args.callees_top_k}, files_per_symbol={args.files_per_symbol}, workers={args.workers}")

    repo_path = str(get_repo_root())
    results = []
    completed = 0

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(run_item, item, repo_path, args.callees_top_k, args.files_per_symbol): item
            for item in items
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            completed += 1
            print(f"  [{completed}/{len(items)}] {result['qa_id']}: "
                  f"best={result['best_symbol']}({result['best_coverage']*100:.0f}%) "
                  f"avg={result['avg_coverage']*100:.0f}%")

    results.sort(key=lambda x: x["qa_id"])

    total = len(results)
    avg_best = sum(r["best_coverage"] for r in results) / total
    avg_avg = sum(r["avg_coverage"] for r in results) / total
    full_from_best = sum(1 for r in results if r["best_coverage"] >= 1.0)

    print(f"\n{'='*60}")
    print("Gold Function Expansion Oracle")
    print(f"{'='*60}")
    print(f"总题数: {total}")
    print(f"Best Gold Symbol 平均覆盖率: {avg_best*100:.1f}%")
    print(f"所有 Gold Symbol 平均覆盖率: {avg_avg*100:.1f}%")
    print(f"Best Gold Symbol 引用全对: {full_from_best}/{total} ({full_from_best/total*100:.1f}%)")

    output = {
        "callees_top_k": args.callees_top_k,
        "files_per_symbol": args.files_per_symbol,
        "summary": {
            "total": total,
            "avg_best_coverage": avg_best,
            "avg_avg_coverage": avg_avg,
            "full_from_best": full_from_best,
        },
        "per_item": results,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
