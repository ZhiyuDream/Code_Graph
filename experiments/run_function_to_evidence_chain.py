#!/usr/bin/env python3
"""
Function → Evidence Chain Completion。

给定一个正确的入口函数（gold function oracle），测量通过 symbol/caller/callee 扩展能覆盖多少 gold files。

扩展策略（纯 grep）：
1. Definition: 读取入口函数所在文件
2. Callers: grep 入口函数名，找到所有调用位置
3. Callees: 从入口函数体中提取它调用的其他函数，取 top-k，分别 grep

对比：
- def_only
- def_and_callers
- def_callers_callees

用法:
    python experiments/run_function_to_evidence_chain.py \
        --benchmark datasets/benchmark_hard.json \
        --range 0,15 \
        --callees-top-k 5 \
        --files-per-symbol 3 \
        -w 2 \
        -o results/function_to_evidence_chain_0_15.json
"""
import json
import re
import sys
from collections import Counter
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


def select_entry_symbol(gold_symbols: list[str], gold_files: list[str]) -> str | None:
    """从 gold symbols 中选择入口 symbol。优先选非 header gold file 中的 symbol。"""
    if gold_symbols:
        return gold_symbols[0]
    return None


def file_priority(fp: str) -> int:
    p = fp.lower()
    if "/src/" in p or p.startswith("src/"):
        return 100
    if "/common/" in p or p.startswith("common/"):
        return 90
    if "/ggml/src/" in p or p.startswith("ggml/src/"):
        return 85
    if "/tests/" in p or p.startswith("tests/"):
        return 20
    if "/examples/" in p or p.startswith("examples/"):
        return 10
    return 40


def select_definition_file(files: list[str]) -> str | None:
    """从 symbol 出现文件中选最可能是定义的文件。"""
    if not files:
        return None
    return max(files, key=lambda f: (file_priority(f), -len(f)))


def expand_from_symbol(entry_symbol: str, repo_path: str,
                       callees_top_k: int, files_per_symbol: int) -> dict:
    """从入口 symbol 扩展证据链，返回三种条件下的访问文件集合。"""
    visited_def = set()
    visited_callers = set()
    visited_callees = set()

    # Find definition file
    all_occurrences = grep_files(entry_symbol, repo_path, limit=100)
    all_occurrences_norm = [normalize_path(f) for f in all_occurrences]
    definition_file = select_definition_file(all_occurrences_norm)

    # Read definition file
    content = ""
    if definition_file:
        try:
            content = read_full_file(definition_file)
            visited_def.add(normalize_path(definition_file))
        except Exception:
            pass

    # Callers: all occurrences of the symbol
    caller_files = {normalize_path(f) for f in all_occurrences_norm}
    visited_callers = visited_def | caller_files

    # Callees: extract top symbols from the definition file content
    callee_symbols = []
    if content:
        investigator = BaseInvestigator.__new__(BaseInvestigator)
        all_symbols = investigator.extract_symbols(content)
        symbol_counter = Counter(all_symbols)
        callee_symbols = [sym for sym, _ in symbol_counter.most_common(callees_top_k)
                          if sym != entry_symbol]

    # Grep each callee symbol
    callee_files = set()
    for sym in callee_symbols:
        files = grep_files(sym, repo_path, limit=50)
        selected = [normalize_path(f) for f in files[:files_per_symbol]]
        callee_files.update(selected)

    visited_callees = visited_callers | callee_files

    return {
        "entry_symbol": entry_symbol,
        "definition_file": normalize_path(definition_file) if definition_file else None,
        "callee_symbols": callee_symbols,
        "def_only": sorted(visited_def),
        "def_and_callers": sorted(visited_callers),
        "def_callers_callees": sorted(visited_callees),
    }


def compute_coverage(visited: list[str], gold_files: list[str]) -> float:
    if not gold_files:
        return 1.0
    norm_visited = {normalize_path(v) for v in visited}
    norm_gold = {normalize_path(g) for g in gold_files}
    hit = len(norm_visited & norm_gold)
    return hit / len(norm_gold)


def run_item(item: dict, repo_path: str, callees_top_k: int, files_per_symbol: int) -> dict:
    entry_symbol = select_entry_symbol(item["gold_symbols"], item["gold_files"])
    if not entry_symbol:
        return {
            "qa_id": item["qa_id"],
            "error": "no entry symbol found",
            "gold_files": item["gold_files"],
        }

    expansion = expand_from_symbol(
        entry_symbol, repo_path,
        callees_top_k, files_per_symbol
    )

    return {
        "qa_id": item["qa_id"],
        "question": item["question"],
        "gold_files": item["gold_files"],
        "gold_symbols": item["gold_symbols"],
        "entry_symbol": expansion["entry_symbol"],
        "definition_file": expansion["definition_file"],
        "callee_symbols": expansion["callee_symbols"],
        "def_only_coverage": compute_coverage(expansion["def_only"], item["gold_files"]),
        "def_and_callers_coverage": compute_coverage(expansion["def_and_callers"], item["gold_files"]),
        "full_coverage": compute_coverage(expansion["def_callers_callees"], item["gold_files"]),
        "def_only_files": expansion["def_only"],
        "def_and_callers_files": expansion["def_and_callers"],
        "full_files": expansion["def_callers_callees"],
    }


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--range", default="0,15")
    parser.add_argument("--callees-top-k", type=int, default=5)
    parser.add_argument("--files-per-symbol", type=int, default=3)
    parser.add_argument("-w", "--workers", type=int, default=2)
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
                  f"entry={result.get('entry_function')} "
                  f"def={result.get('def_only_coverage', 0)*100:.0f}% "
                  f"+callers={result.get('def_and_callers_coverage', 0)*100:.0f}% "
                  f"+callees={result.get('full_coverage', 0)*100:.0f}%")

    results.sort(key=lambda x: x["qa_id"])

    total = len([r for r in results if "error" not in r])
    avg_def = sum(r["def_only_coverage"] for r in results if "error" not in r) / total
    avg_callers = sum(r["def_and_callers_coverage"] for r in results if "error" not in r) / total
    avg_full = sum(r["full_coverage"] for r in results if "error" not in r) / total

    full_correct = sum(1 for r in results if "error" not in r and r["full_coverage"] >= 1.0)

    print(f"\n{'='*60}")
    print("Function → Evidence Chain Completion")
    print(f"{'='*60}")
    print(f"总题数: {total}")
    print(f"仅 Definition 平均覆盖率: {avg_def*100:.1f}%")
    print(f"Definition + Callers 平均覆盖率: {avg_callers*100:.1f}%")
    print(f"Definition + Callers + Callees 平均覆盖率: {avg_full*100:.1f}%")
    print(f"引用全对 (full chain): {full_correct}/{total} ({full_correct/total*100:.1f}%)")

    output = {
        "callees_top_k": args.callees_top_k,
        "files_per_symbol": args.files_per_symbol,
        "summary": {
            "total": total,
            "avg_def_only_coverage": avg_def,
            "avg_def_and_callers_coverage": avg_callers,
            "avg_full_coverage": avg_full,
            "full_correct": full_correct,
        },
        "per_item": results,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
