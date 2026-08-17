#!/usr/bin/env python3
"""
Top-3 Select + Read experiment.

From a candidate pool, LLM first selects top 3 most likely evidence anchors.
Then it reads the code of these 3 functions and makes a final selection.

This tests whether LLM's selection improves when given actual code content
of a small candidate set.
"""
import json
import re
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.qa.candidate_pool import build_function_pool
from src.qa.investigation.base import LLMClient, load_prompt
from src.qa.retrievers.fast_embedding import FastEmbeddingRetriever


def parse_topk_ids(text: str, max_id: int, k: int) -> list[int]:
    try:
        start = text.find('[')
        end = text.rfind(']')
        if start >= 0 and end > start:
            arr = json.loads(text[start:end + 1])
            if isinstance(arr, list):
                valid = []
                for idx in arr:
                    if isinstance(idx, int) and 1 <= idx <= max_id and idx not in valid:
                        valid.append(idx)
                return valid[:k]
    except Exception:
        pass
    valid = []
    for m in re.finditer(r'\d+', text):
        idx = int(m.group(0))
        if 1 <= idx <= max_id and idx not in valid:
            valid.append(idx)
    return valid[:k]


def extract_function_name(text: str) -> str:
    final_guess = text.split('\n')[0].strip().strip('"').strip('`').strip()
    final_guess = re.sub(r'.*::', '', final_guess)
    final_guess = re.sub(r'\(\)$', '', final_guess)
    final_guess = re.sub(r'\(.*\)$', '', final_guess)
    return final_guess


def run_item(item: dict, query_emb, retriever: FastEmbeddingRetriever,
             llm: LLMClient, k: int, max_pool_size: int) -> dict:
    pool = build_function_pool(query_emb, retriever, max_pool_size=max_pool_size)
    gold_symbols = item["gold_symbols"]

    # Stage 1: select top k by name+signature
    summaries = []
    for i, c in enumerate(pool, 1):
        sig = c.get("signature", "")
        summaries.append(f"[{i}] {c['file_path']}::{c['name']}({sig})")

    select_prompt = load_prompt("select_top3").format(
        question=item["question"],
        candidates="\n".join(summaries),
        k=k,
    )
    text1 = llm.call(select_prompt).strip()
    topk_ids = parse_topk_ids(text1, len(pool), k)
    topk = [pool[i - 1] for i in topk_ids if 1 <= i <= len(pool)]
    if not topk:
        topk = pool[:k]

    # Stage 2: read code and final select
    code_blocks = []
    for i, c in enumerate(topk, 1):
        code = c["text"][:2000]
        code_blocks.append(
            f"=== Candidate {i}: {c['file_path']}::{c['name']}() ===\n```cpp\n{code}\n```"
        )

    final_prompt = load_prompt("final_select_from_code").format(
        question=item["question"],
        candidates_with_code="\n\n".join(code_blocks),
    )
    text2 = llm.call(final_prompt).strip()
    final_guess = extract_function_name(text2)

    correct = final_guess in gold_symbols
    return {
        "qa_id": item["qa_id"],
        "gold_symbols": sorted(gold_symbols),
        "topk": [c["name"] for c in topk],
        "gold_in_topk": bool(gold_symbols & set(c["name"] for c in topk)),
        "final_guess": final_guess,
        "correct": correct,
    }


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, default=Path("datasets/benchmark_hard.json"))
    parser.add_argument("--range", default="0,50")
    parser.add_argument("--k", type=int, default=3, help="Number of candidates to select for reading")
    parser.add_argument("--max-pool-size", type=int, default=20, help="Initial candidate pool size")
    parser.add_argument("-w", "--workers", type=int, default=2)
    parser.add_argument("-o", "--output", type=Path, default=Path("results/top3_read_selection_0_50.json"))
    args = parser.parse_args()

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
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(run_item, item, query_embs[i], retriever, llm, args.k, args.max_pool_size): item
            for i, item in enumerate(items)
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            completed += 1
            print(f"  [{completed}/{len(items)}] {result['qa_id']}: "
                  f"correct={result['correct']} gold_in_topk={result['gold_in_topk']} "
                  f"guess={result['final_guess']} topk={result['topk']}")

    results.sort(key=lambda x: x["qa_id"])
    total = len(results)
    correct = sum(1 for r in results if r["correct"])
    gold_in_topk = sum(1 for r in results if r["gold_in_topk"])
    conditional = correct / gold_in_topk if gold_in_topk else 0

    print(f"\n{'='*60}")
    print(f"Top-{args.k} Select + Read ({total} questions, pool={args.max_pool_size})")
    print(f"{'='*60}")
    print(f"Gold in top-{args.k}: {gold_in_topk}/{total} = {gold_in_topk/total*100:.1f}%")
    print(f"Final selection recall: {correct}/{total} = {correct/total*100:.1f}%")
    print(f"Conditional recall: {conditional*100:.1f}%")

    output = {
        "config": {"k": args.k, "max_pool_size": args.max_pool_size, "range": args.range},
        "summary": {
            "total": total,
            "gold_in_topk": gold_in_topk,
            "gold_in_topk_rate": gold_in_topk / total,
            "correct": correct,
            "recall": correct / total,
            "conditional_recall": conditional,
        },
        "per_item": results,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
