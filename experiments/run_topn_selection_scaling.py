#!/usr/bin/env python3
"""
Top-N selection scaling experiment.

Measure how LLM single-selection recall degrades as the candidate set size grows.
"""
import json
import re
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.qa.candidate_pool import build_function_pool
from src.qa.investigation.base import LLMClient, load_prompt
from src.qa.retrievers.fast_embedding import FastEmbeddingRetriever


def select_one(question: str, candidates: list[dict], llm: LLMClient,
               prompt_template: str) -> dict:
    summaries = []
    for i, c in enumerate(candidates, 1):
        summaries.append(f"[{i}] {c['file_path']}::{c['name']}()")
    prompt = prompt_template.format(
        question=question,
        file_summaries="\n\n".join(summaries),
    )
    text = llm.call(prompt).strip()

    all_ids = []
    for m in re.finditer(r'\d+', text):
        idx = int(m.group(0))
        if 1 <= idx <= len(candidates):
            all_ids.append(idx)

    seen = set()
    for idx in all_ids:
        if idx not in seen:
            return candidates[idx - 1]
    return candidates[0]


def run_question(item: dict, query_emb, retriever: FastEmbeddingRetriever,
                 llm: LLMClient, prompt_template: str, ns: list[int]) -> dict:
    full_pool = build_function_pool(query_emb, retriever)
    gold_symbols = item["gold_symbols"]

    per_n = {}
    for n in ns:
        pool = full_pool[:n]
        if not pool:
            per_n[n] = {"selected": "", "correct": False, "gold_in_pool": False}
            continue
        gold_in_pool = bool(gold_symbols & {c["name"] for c in pool})
        selected = select_one(item["question"], pool, llm, prompt_template)
        correct = selected["name"] in gold_symbols
        per_n[n] = {
            "selected": selected["name"],
            "selected_file": selected.get("file_path", ""),
            "correct": correct,
            "gold_in_pool": gold_in_pool,
        }
    return {"qa_id": item["qa_id"], "per_n": per_n}


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, default=Path("datasets/benchmark_hard.json"))
    parser.add_argument("--range", default="0,50")
    parser.add_argument("--ns", default="2,5,10,20,50,100")
    parser.add_argument("--prompt", default="stage1_select_function_anchor")
    parser.add_argument("-w", "--workers", type=int, default=5)
    parser.add_argument("-o", "--output", type=Path, default=Path("results/topn_selection_scaling_0_50.json"))
    args = parser.parse_args()

    ns = [int(x) for x in args.ns.split(",")]

    with open(args.benchmark, "r", encoding="utf-8") as f:
        bench = json.load(f)
    all_items = bench["items"]
    if "," in args.range:
        start, end = map(int, args.range.split(","))
    else:
        start, end = 0, len(all_items)

    items = []
    for idx in range(start, end):
        item = all_items[idx]
        gold_symbols = set()
        for ev in item.get("gold_evidence", []):
            sym = ev.get("symbol", "")
            if sym:
                gold_symbols.add(sym)
        items.append({
            "qa_id": item.get("qa_id", f"q{idx}"),
            "question": item.get("question", ""),
            "gold_symbols": gold_symbols,
        })

    retriever = FastEmbeddingRetriever()
    query_embs = retriever.encode_queries([it["question"] for it in items])
    llm = LLMClient()
    prompt_template = load_prompt(args.prompt)

    results = []
    completed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(run_question, item, query_embs[i], retriever, llm, prompt_template, ns): item
            for i, item in enumerate(items)
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            completed += 1
            print(f"  [{completed}/{len(items)}] {result['qa_id']}")

    results.sort(key=lambda x: x["qa_id"])

    summary = []
    for n in ns:
        total = len(results)
        gold_in_pool = sum(1 for r in results if r["per_n"][n]["gold_in_pool"])
        correct = sum(1 for r in results if r["per_n"][n]["correct"])
        recall = correct / total
        conditional_recall = correct / gold_in_pool if gold_in_pool else 0
        summary.append({
            "n": n,
            "gold_in_pool": gold_in_pool,
            "gold_in_pool_rate": gold_in_pool / total,
            "correct": correct,
            "recall": recall,
            "conditional_recall": conditional_recall,
        })

    print(f"\n{'='*60}")
    print(f"Top-N Selection Scaling (prompt={args.prompt})")
    print(f"{'='*60}")
    print(f"{'N':>4s} {'Pool Recall':>12s} {'Selection Recall':>18s} {'Cond. Recall':>14s}")
    for s in summary:
        print(f"{s['n']:>4d} {s['gold_in_pool_rate']*100:>11.1f}% {s['recall']*100:>17.1f}% {s['conditional_recall']*100:>13.1f}%")

    output = {
        "config": {"ns": ns, "prompt": args.prompt},
        "summary": summary,
        "per_item": results,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
