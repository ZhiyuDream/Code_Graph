#!/usr/bin/env python3
"""
Lightweight Two-Stage Retrieval Recall Experiment.

Stage 1: Question -> Top-K files
Stage 2: For each file -> Top-M functions
Check if gold symbol is in aggregated pool.

No LLM, no expansion. Fast.
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


def two_stage_recall(question: str, gold_symbols: set[str], gold_files: set[str],
                     retriever: EmbeddingRetriever,
                     top_k_files: int, top_m_functions: int) -> dict:
    # Stage 1
    file_results = retriever.retrieve(question, top_k=top_k_files * 3)
    file_scores = {}
    for r in file_results:
        fp = normalize_path(r.metadata.get("file_path", ""))
        if fp:
            file_scores[fp] = max(file_scores.get(fp, 0), r.score)
    top_files = sorted(file_scores.items(), key=lambda x: -x[1])[:top_k_files]

    # Stage 2
    all_names = set()
    per_file = []
    for fp, _ in top_files:
        func_results = retriever.retrieve(question, top_k=top_m_functions, file_filter={fp})
        names = []
        for r in func_results:
            name = r.metadata.get("name", "")
            if name:
                names.append(name)
                all_names.add(name)
        per_file.append({"file": fp, "functions": names})

    symbol_hit = bool(gold_symbols & all_names)
    top_file_set = {fp for fp, _ in top_files}
    file_hit = bool(gold_files & top_file_set) or any(
        any(g in tf or tf in g for tf in top_file_set) for g in gold_files
    )

    return {
        "symbol_hit": symbol_hit,
        "file_hit": file_hit,
        "num_candidates": len(all_names),
        "top_files": [fp for fp, _ in top_files],
        "per_file": per_file,
    }


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, default=Path("datasets/benchmark_hard.json"))
    parser.add_argument("--oracle", type=Path, default=Path("results/gold_function_expansion_oracle_0_15.json"))
    parser.add_argument("--range", default="0,15")
    parser.add_argument("--top-k-files", type=int, default=10)
    parser.add_argument("--top-m-functions", type=int, default=5)
    parser.add_argument("-o", "--output", type=Path, default=Path("results/two_stage_recall_0_15.json"))
    args = parser.parse_args()

    items = load_items(args.benchmark, args.range)
    with open(args.oracle, "r", encoding="utf-8") as f:
        oracle = json.load(f)
    oracle_by_qa = {r["qa_id"]: r for r in oracle["per_item"]}

    retriever = EmbeddingRetriever()

    results = []
    print(f"=== Two-Stage Recall (files={args.top_k_files}, funcs={args.top_m_functions}) ===\n")
    for item in items:
        qa_id = item["qa_id"]
        o = oracle_by_qa[qa_id]
        gold_symbols = set(o.get("gold_symbols", []))
        gold_files = set(item["gold_files"])

        result = two_stage_recall(item["question"], gold_symbols, gold_files,
                                  retriever, args.top_k_files, args.top_m_functions)
        result["qa_id"] = qa_id
        result["gold_symbols"] = list(gold_symbols)
        results.append(result)

        print(f"{qa_id}: file_hit={result['file_hit']} symbol_hit={result['symbol_hit']} "
              f"candidates={result['num_candidates']} gold={gold_symbols}")

    total = len(results)
    file_recall = sum(1 for r in results if r["file_hit"]) / total
    symbol_recall = sum(1 for r in results if r["symbol_hit"]) / total

    print(f"\n{'='*60}")
    print("Two-Stage Recall Summary")
    print(f"{'='*60}")
    print(f"总题数: {total}")
    print(f"File Recall: {file_recall*100:.1f}%")
    print(f"Symbol Recall: {symbol_recall*100:.1f}%")

    output = {
        "summary": {
            "total": total,
            "top_k_files": args.top_k_files,
            "top_m_functions": args.top_m_functions,
            "file_recall": file_recall,
            "symbol_recall": symbol_recall,
        },
        "per_item": results,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
