#!/usr/bin/env python3
"""
Gold Symbol Ranking Experiment.

For each question, ask LLM to rank all candidate symbols.
Then compute where the gold symbol ranks.

This answers: does LLM see the gold symbol as important, or does it ignore it?
"""
import json
import re
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from config import get_repo_root
from src.qa.retrievers.embedding import EmbeddingRetriever
from src.qa.investigation.base import LLMClient, load_prompt


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


def rank_candidates(question: str, candidates: list[dict], llm: LLMClient) -> list[int]:
    prompt_template = load_prompt("rank_candidates")
    summaries = []
    for i, c in enumerate(candidates, 1):
        summaries.append(f"[{i}] {c['file_path']}::{c['name']}()")
    prompt = prompt_template.format(
        question=question,
        file_summaries="\n\n".join(summaries),
    )
    text = llm.call(prompt).strip()

    try:
        start = text.find('[')
        end = text.rfind(']')
        if start >= 0 and end > start:
            arr = json.loads(text[start:end+1])
            if isinstance(arr, list):
                valid = []
                seen = set()
                for idx in arr:
                    if isinstance(idx, int) and 1 <= idx <= len(candidates) and idx not in seen:
                        valid.append(idx)
                        seen.add(idx)
                # append missing ids at the end
                for i in range(1, len(candidates) + 1):
                    if i not in seen:
                        valid.append(i)
                return valid
    except Exception:
        pass

    # Fallback: try to parse numbers in order
    valid = []
    seen = set()
    for m in re.finditer(r'\d+', text):
        idx = int(m.group(0))
        if 1 <= idx <= len(candidates) and idx not in seen:
            valid.append(idx)
            seen.add(idx)
    for i in range(1, len(candidates) + 1):
        if i not in seen:
            valid.append(i)
    return valid


def run_item(item: dict, oracle_item: dict, retriever: EmbeddingRetriever,
             llm: LLMClient, top_k: int) -> dict:
    results = retriever.retrieve(item["question"], top_k=top_k)
    candidates = []
    seen = set()
    for r in results:
        meta = r.metadata
        name = meta.get("name", "")
        file_path = meta.get("file_path", "")
        key = (file_path, name)
        if name and key not in seen:
            seen.add(key)
            candidates.append({
                "file_path": file_path,
                "name": name,
            })

    gold_symbols = set(oracle_item.get("gold_symbols", []))
    best_symbol = oracle_item.get("best_symbol")

    if not candidates:
        return {"qa_id": item["qa_id"], "error": "no candidates"}

    ranked_ids = rank_candidates(item["question"], candidates, llm)
    ranked = [candidates[i-1] for i in ranked_ids]

    # Compute gold symbol ranks
    gold_ranks = []
    for sym in gold_symbols:
        for rank, c in enumerate(ranked, 1):
            if c["name"] == sym:
                gold_ranks.append({"symbol": sym, "rank": rank})
                break

    best_rank = None
    if best_symbol:
        for rank, c in enumerate(ranked, 1):
            if c["name"] == best_symbol:
                best_rank = rank
                break

    gold_in_top1 = any(r["rank"] == 1 for r in gold_ranks)
    gold_in_top3 = any(r["rank"] <= 3 for r in gold_ranks)
    gold_in_top5 = any(r["rank"] <= 5 for r in gold_ranks)

    return {
        "qa_id": item["qa_id"],
        "num_candidates": len(candidates),
        "gold_symbols": list(gold_symbols),
        "best_symbol": best_symbol,
        "best_rank": best_rank,
        "gold_ranks": gold_ranks,
        "gold_in_top1": gold_in_top1,
        "gold_in_top3": gold_in_top3,
        "gold_in_top5": gold_in_top5,
        "ranked_names": [c["name"] for c in ranked],
    }


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, default=Path("datasets/benchmark_hard.json"))
    parser.add_argument("--oracle", type=Path, default=Path("results/gold_function_expansion_oracle_0_15.json"))
    parser.add_argument("--range", default="0,15")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("-w", "--workers", type=int, default=2)
    parser.add_argument("-o", "--output", type=Path, default=Path("results/gold_ranking_experiment_0_15.json"))
    args = parser.parse_args()

    items = load_items(args.benchmark, args.range)
    with open(args.oracle, "r", encoding="utf-8") as f:
        oracle = json.load(f)
    oracle_by_qa = {r["qa_id"]: r for r in oracle["per_item"]}

    retriever = EmbeddingRetriever()
    llm = LLMClient()

    results = []
    completed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(run_item, item, oracle_by_qa[item["qa_id"]],
                            retriever, llm, args.top_k): item
            for item in items
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            completed += 1
            print(f"  [{completed}/{len(items)}] {result['qa_id']}: "
                  f"best_rank={result.get('best_rank')} "
                  f"gold_top1={result.get('gold_in_top1')} "
                  f"gold_top3={result.get('gold_in_top3')} "
                  f"gold_top5={result.get('gold_in_top5')}")

    results.sort(key=lambda x: x["qa_id"])
    total = len([r for r in results if "error" not in r])

    best_ranks = [r["best_rank"] for r in results if "error" not in r and r["best_rank"] is not None]
    avg_best_rank = sum(best_ranks) / len(best_ranks) if best_ranks else None
    top1 = sum(1 for r in results if "error" not in r and r.get("gold_in_top1")) / total
    top3 = sum(1 for r in results if "error" not in r and r.get("gold_in_top3")) / total
    top5 = sum(1 for r in results if "error" not in r and r.get("gold_in_top5")) / total
    mrr = sum(1 / r for r in best_ranks) / len(best_ranks) if best_ranks else None

    print(f"\n{'='*60}")
    print("Gold Symbol Ranking Experiment")
    print(f"{'='*60}")
    print(f"总题数: {total}")
    print(f"Best Gold Symbol 平均排名: {avg_best_rank:.2f}")
    print(f"MRR: {mrr:.3f}")
    print(f"Gold in Top-1: {top1*100:.1f}%")
    print(f"Gold in Top-3: {top3*100:.1f}%")
    print(f"Gold in Top-5: {top5*100:.1f}%")

    output = {
        "summary": {
            "total": total,
            "avg_best_rank": avg_best_rank,
            "mrr": mrr,
            "top1_rate": top1,
            "top3_rate": top3,
            "top5_rate": top5,
        },
        "per_item": results,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
