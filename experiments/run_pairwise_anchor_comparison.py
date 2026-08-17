#!/usr/bin/env python3
"""
Pairwise comparison experiment.

For each question where the gold symbol appears in the top-N of the candidate pool,
pit the gold symbol against the highest-ranked non-gold candidate.

This tests whether LLM's failure is:
  (a) inability to recognize evidence anchors, or
  (b) inability to select them from a long list (preference bias).
"""
import json
import random
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.qa.candidate_pool import build_function_pool
from src.qa.investigation.base import LLMClient, load_prompt
from src.qa.retrievers.fast_embedding import FastEmbeddingRetriever


def pairwise_compare(question: str, a: dict, b: dict, llm: LLMClient) -> str:
    prompt = load_prompt("pairwise_anchor").format(
        question=question,
        candidate_a=f"{a['file_path']}::{a['name']}()",
        candidate_b=f"{b['file_path']}::{b['name']}()",
    )
    text = llm.call(prompt).strip().upper()
    if text.startswith("A"):
        return "A"
    if text.startswith("B"):
        return "B"
    if "A" in text and "B" not in text:
        return "A"
    if "B" in text and "A" not in text:
        return "B"
    return "A"


def run_item(item: dict, query_emb, retriever: FastEmbeddingRetriever,
             llm: LLMClient, max_rank: int) -> dict | None:
    pool = build_function_pool(query_emb, retriever)
    gold_symbols = item["gold_symbols"]

    gold_candidate = None
    gold_rank = None
    for i, c in enumerate(pool, 1):
        if c["name"] in gold_symbols:
            gold_candidate = c
            gold_rank = i
            break

    if gold_candidate is None or gold_rank is None or gold_rank > max_rank:
        return None

    top_non_gold = None
    for c in pool:
        if c["name"] not in gold_symbols:
            top_non_gold = c
            break

    if top_non_gold is None:
        return None

    candidates = [gold_candidate, top_non_gold]
    random.shuffle(candidates)
    a, b = candidates
    gold_label = "A" if a["name"] in gold_symbols else "B"

    choice = pairwise_compare(item["question"], a, b, llm)
    correct = (choice == gold_label)

    return {
        "qa_id": item["qa_id"],
        "gold_rank": gold_rank,
        "gold_candidate": gold_candidate["name"],
        "gold_file": gold_candidate["file_path"],
        "non_gold_candidate": top_non_gold["name"],
        "non_gold_file": top_non_gold["file_path"],
        "gold_label": gold_label,
        "llm_choice": choice,
        "correct": correct,
    }


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, default=Path("datasets/benchmark_hard.json"))
    parser.add_argument("--range", default="0,50")
    parser.add_argument("--max-rank", type=int, default=10)
    parser.add_argument("-w", "--workers", type=int, default=5)
    parser.add_argument("-o", "--output", type=Path, default=Path("results/pairwise_anchor_comparison_0_50.json"))
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    random.seed(args.seed)

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

    results = []
    completed = 0
    skipped = 0
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(run_item, item, query_embs[i], retriever, llm, args.max_rank): item
            for i, item in enumerate(items)
        }
        for future in as_completed(futures):
            result = future.result()
            if result is None:
                skipped += 1
                continue
            results.append(result)
            completed += 1
            print(f"  [{completed}] {result['qa_id']}: rank={result['gold_rank']} "
                  f"gold={result['gold_candidate']} vs {result['non_gold_candidate']} "
                  f"-> {'CORRECT' if result['correct'] else 'WRONG'}")

    results.sort(key=lambda x: x["qa_id"])
    total = len(results)
    correct = sum(1 for r in results if r["correct"])

    print(f"\n{'='*60}")
    print(f"Pairwise Anchor Comparison (gold rank <= {args.max_rank})")
    print(f"{'='*60}")
    print(f"Total questions evaluated: {total} (skipped {skipped})")
    print(f"LLM picks gold over top non-gold: {correct} / {total} = {correct/total*100:.1f}%")

    output = {
        "config": {"max_rank": args.max_rank},
        "summary": {"total": total, "correct": correct, "accuracy": correct / total if total else 0},
        "per_item": results,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
