#!/usr/bin/env python3
"""
Tournament selection pilot.

Use pairwise comparisons to progressively eliminate candidates.
This tests whether strong pairwise recognition can be composed into
reliable global selection.
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


def pairwise_compare(question: str, a: dict, b: dict, llm: LLMClient) -> dict:
    candidates = [a, b]
    random.shuffle(candidates)
    a, b = candidates
    prompt = load_prompt("pairwise_anchor").format(
        question=question,
        candidate_a=f"{a['file_path']}::{a['name']}()",
        candidate_b=f"{b['file_path']}::{b['name']}()",
    )
    text = llm.call(prompt).strip().upper()
    if text.startswith("A"):
        winner = a
    elif text.startswith("B"):
        winner = b
    elif "A" in text and "B" not in text:
        winner = a
    elif "B" in text and "A" not in text:
        winner = b
    else:
        winner = a
    return winner


def tournament(question: str, candidates: list[dict], llm: LLMClient) -> dict:
    current = candidates[:]
    while len(current) > 1:
        random.shuffle(current)
        next_round = []
        for i in range(0, len(current), 2):
            if i + 1 >= len(current):
                next_round.append(current[i])
            else:
                winner = pairwise_compare(question, current[i], current[i + 1], llm)
                next_round.append(winner)
        current = next_round
    return current[0]


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, default=Path("datasets/benchmark_hard.json"))
    parser.add_argument("--range", default="0,50")
    parser.add_argument("--top-n", type=int, default=8, help="Run tournament on top-N candidates")
    parser.add_argument("--n-questions", type=int, default=20)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("-w", "--workers", type=int, default=5)
    parser.add_argument("-o", "--output", type=Path, default=Path("results/tournament_selection_pilot_top8_0_20.json"))
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

    ranked_items = []
    for emb, item in zip(query_embs, items):
        pool = build_function_pool(emb, retriever)
        for i, c in enumerate(pool, 1):
            if c["name"] in item["gold_symbols"]:
                item["gold_rank"] = i
                break
        else:
            item["gold_rank"] = None
        ranked_items.append(item)

    # Sample diverse questions
    rank_1 = [it for it in ranked_items if it["gold_rank"] == 1]
    rank_2_10 = [it for it in ranked_items if it["gold_rank"] and 2 <= it["gold_rank"] <= 10]
    rank_11_plus = [it for it in ranked_items if it["gold_rank"] and it["gold_rank"] > 10]
    missing = [it for it in ranked_items if it["gold_rank"] is None]

    selected = []
    selected.extend(rank_1[:2])
    selected.extend(rank_2_10[:2])
    selected.extend(rank_11_plus[:1])
    selected.extend(missing[:2])
    selected = selected[:args.n_questions]

    if len(selected) < args.n_questions:
        remaining = [it for it in ranked_items if it not in selected]
        selected.extend(random.sample(remaining, args.n_questions - len(selected)))

    selected_embs = retriever.encode_queries([it["question"] for it in selected])

    llm = LLMClient()

    results = []
    completed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                lambda it, emb: (
                    it["qa_id"],
                    tournament(it["question"], build_function_pool(emb, retriever, max_pool_size=args.top_n), llm),
                    it["gold_rank"],
                ),
                item, emb
            ): item for item, emb in zip(selected, selected_embs)
        }
        for future in as_completed(futures):
            qa_id, winner, gold_rank = future.result()
            item = futures[future]
            correct = winner["name"] in item["gold_symbols"]
            results.append({
                "qa_id": qa_id,
                "gold_rank": gold_rank,
                "winner": winner["name"],
                "winner_file": winner["file_path"],
                "correct": correct,
                "gold_symbols": sorted(item["gold_symbols"]),
            })
            completed += 1
            print(f"  [{completed}/{len(selected)}] {qa_id}: rank={gold_rank} "
                  f"winner={winner['name']} -> {'CORRECT' if correct else 'WRONG'}")

    total = len(results)
    correct_count = sum(1 for r in results if r["correct"])

    print(f"\n{'='*60}")
    print(f"Tournament Selection Pilot ({total} questions, top-{args.top_n})")
    print(f"{'='*60}")
    print(f"Tournament recall: {correct_count}/{total} = {correct_count/total*100:.1f}%")

    output = {
        "config": {"top_n": args.top_n, "n_questions": total},
        "summary": {"total": total, "correct": correct_count, "recall": correct_count / total},
        "per_item": results,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
