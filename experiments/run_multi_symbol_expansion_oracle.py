#!/usr/bin/env python3
"""
Multi-Symbol Expansion Oracle.

For each question, retrieve Top-K function candidates and run expand_from_function
on each candidate. Measure the union coverage over gold files.

This estimates the ceiling of "investigate all plausible symbols in parallel".
"""
import json
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from config import get_repo_root
from src.qa.retrievers.embedding import EmbeddingRetriever
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


def run_item(item: dict, retriever: EmbeddingRetriever, repo_path: str,
             top_k: int, top_n: int, files_per_symbol: int) -> dict:
    results = retriever.retrieve(item["question"], top_k=top_k)
    candidates = []
    seen = set()
    for r in results:
        meta = r.metadata
        key = (meta.get("file_path", ""), meta.get("name", ""))
        if key[0] and key not in seen:
            seen.add(key)
            candidates.append({
                "file_path": meta.get("file_path", ""),
                "name": meta.get("name", ""),
            })

    union_visited = set()
    per_candidate = []
    for c in candidates:
        visited = expand_from_function(c["name"], repo_path, top_n, files_per_symbol)
        union_visited.update(visited)
        per_candidate.append({
            "name": c["name"],
            "file_path": c["file_path"],
            "visited_count": len(visited),
        })

    gold_norm = {normalize_path(g) for g in item["gold_files"]}
    union_norm = {normalize_path(v) for v in union_visited}
    coverage = len(gold_norm & union_norm) / len(gold_norm) if gold_norm else 1.0

    return {
        "qa_id": item["qa_id"],
        "gold_files": item["gold_files"],
        "num_candidates": len(candidates),
        "union_visited_count": len(union_visited),
        "coverage": coverage,
        "per_candidate": per_candidate,
    }


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, default=Path("datasets/benchmark_hard.json"))
    parser.add_argument("--range", default="0,15")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument("--files-per-symbol", type=int, default=3)
    parser.add_argument("-w", "--workers", type=int, default=4)
    parser.add_argument("-o", "--output", type=Path, default=Path("results/multi_symbol_expansion_oracle_0_15.json"))
    args = parser.parse_args()

    items = load_items(args.benchmark, args.range)
    retriever = EmbeddingRetriever()
    repo_path = str(get_repo_root())

    results = []
    completed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(run_item, item, retriever, repo_path,
                            args.top_k, args.top_n, args.files_per_symbol): item
            for item in items
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            completed += 1
            print(f"  [{completed}/{len(items)}] {result['qa_id']}: "
                  f"candidates={result['num_candidates']} "
                  f"union={result['union_visited_count']} "
                  f"coverage={result['coverage']*100:.0f}%")

    results.sort(key=lambda x: x["qa_id"])
    total = len(results)
    avg_coverage = sum(r["coverage"] for r in results) / total
    full = sum(1 for r in results if r["coverage"] >= 1.0)
    avg_union_size = sum(r["union_visited_count"] for r in results) / total

    print(f"\n{'='*60}")
    print("Multi-Symbol Expansion Oracle (Top-10 Union)")
    print(f"{'='*60}")
    print(f"总题数: {total}")
    print(f"平均 Union Coverage: {avg_coverage*100:.1f}%")
    print(f"完全覆盖: {full}/{total} ({full/total*100:.1f}%)")
    print(f"平均 Union 文件数: {avg_union_size:.1f}")

    output = {
        "summary": {
            "total": total,
            "avg_coverage": avg_coverage,
            "full_correct": full,
            "avg_union_size": avg_union_size,
        },
        "per_item": results,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
