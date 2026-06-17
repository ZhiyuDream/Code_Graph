#!/usr/bin/env python3
"""
Unified File + Symbol Recall Decomposition.

For each question, using the same embedding retrieval setting:
1. Check if any gold FILE is in top-k candidates (file recall)
2. Check if any gold SYMBOL is in top-k candidates (symbol recall)
3. Decompose into:
   A. file hit + symbol hit
   B. file hit + symbol miss
   C. file miss

This resolves the file-level vs symbol-level confusion.
"""
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.qa.retrievers.embedding import EmbeddingRetriever


def normalize_path(path: str) -> str:
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
        selected.append({
            "qa_id": item.get("qa_id", f"q{idx}"),
            "question": item.get("question", ""),
            "gold_files": gold_files,
        })
    return selected


def run_item(item: dict, oracle_item: dict, retriever: EmbeddingRetriever, top_k: int) -> dict:
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

    candidate_files = {normalize_path(c["file_path"]) for c in candidates}
    candidate_names = {c["name"] for c in candidates}

    gold_files = {normalize_path(g) for g in item["gold_files"]}
    gold_symbols = set(oracle_item.get("gold_symbols", []))
    best_symbol = oracle_item.get("best_symbol")

    file_hit = any(g in candidate_files or any(g in cf or cf in g for cf in candidate_files) for g in gold_files)
    symbol_hit = bool(gold_symbols & candidate_names)
    best_hit = best_symbol in candidate_names if best_symbol else False

    if file_hit and symbol_hit:
        category = "A"
    elif file_hit and not symbol_hit:
        category = "B"
    else:
        category = "C"

    return {
        "qa_id": item["qa_id"],
        "category": category,
        "file_hit": file_hit,
        "symbol_hit": symbol_hit,
        "best_hit": best_hit,
        "gold_files": list(gold_files),
        "gold_symbols": list(gold_symbols),
        "best_symbol": best_symbol,
        "candidate_files": sorted(candidate_files),
        "candidate_names": sorted(candidate_names),
    }


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, default=Path("datasets/benchmark_hard.json"))
    parser.add_argument("--oracle", type=Path, default=Path("results/gold_function_expansion_oracle_0_15.json"))
    parser.add_argument("--range", default="0,15")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("-o", "--output", type=Path, default=Path("results/file_symbol_recall_decomposition_0_15.json"))
    args = parser.parse_args()

    items = load_items(args.benchmark, args.range)
    with open(args.oracle, "r", encoding="utf-8") as f:
        oracle = json.load(f)
    oracle_by_qa = {r["qa_id"]: r for r in oracle["per_item"]}

    retriever = EmbeddingRetriever()

    results = []
    print(f"=== File + Symbol Recall Decomposition (top_k={args.top_k}) ===\n")
    for item in items:
        result = run_item(item, oracle_by_qa[item["qa_id"]], retriever, args.top_k)
        results.append(result)
        print(f"{result['qa_id']}: category={result['category']} "
              f"file_hit={result['file_hit']} symbol_hit={result['symbol_hit']} best_hit={result['best_hit']} "
              f"gold={result['gold_symbols']}")

    total = len(results)
    cat_a = sum(1 for r in results if r["category"] == "A") / total
    cat_b = sum(1 for r in results if r["category"] == "B") / total
    cat_c = sum(1 for r in results if r["category"] == "C") / total
    file_recall = sum(1 for r in results if r["file_hit"]) / total
    symbol_recall = sum(1 for r in results if r["symbol_hit"]) / total
    best_recall = sum(1 for r in results if r["best_hit"]) / total

    print(f"\n{'='*60}")
    print("File + Symbol Recall Decomposition")
    print(f"{'='*60}")
    print(f"总题数: {total}")
    print(f"File Recall @K={args.top_k}: {file_recall*100:.1f}%")
    print(f"Symbol Recall @K={args.top_k}: {symbol_recall*100:.1f}%")
    print(f"Best Symbol Recall @K={args.top_k}: {best_recall*100:.1f}%")
    print(f"\nA. File hit + Symbol hit: {cat_a*100:.1f}% ({cat_a*total:.0f}/{total})")
    print(f"B. File hit + Symbol miss: {cat_b*100:.1f}% ({cat_b*total:.0f}/{total})")
    print(f"C. File miss: {cat_c*100:.1f}% ({cat_c*total:.0f}/{total})")

    output = {
        "summary": {
            "total": total,
            "top_k": args.top_k,
            "file_recall": file_recall,
            "symbol_recall": symbol_recall,
            "best_symbol_recall": best_recall,
            "category_A": cat_a,
            "category_B": cat_b,
            "category_C": cat_c,
        },
        "per_item": results,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
