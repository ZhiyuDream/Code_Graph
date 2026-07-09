#!/usr/bin/env python3
"""
Selection ablation + prompt redesign experiment.

Compares conditions to determine whether selection errors are due to:
  (1) missing information (signature should help), or
  (2) wrong objective (evidence/anchor framing should help).
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


def llm_select(question: str, candidates: list[dict], llm: LLMClient,
               prompt_template: str, include_signature: bool,
               multi: bool = False) -> list[dict]:
    summaries = []
    for i, c in enumerate(candidates, 1):
        sig = c.get("signature", "")
        if include_signature and sig:
            summaries.append(f"[{i}] {c['file_path']}::{c['name']}({sig})")
        else:
            summaries.append(f"[{i}] {c['file_path']}::{c['name']}()")

    prompt = prompt_template.format(
        question=question,
        file_summaries="\n\n".join(summaries),
    )
    text = llm.call(prompt).strip()

    if multi:
        try:
            start = text.find('[')
            end = text.rfind(']')
            if start >= 0 and end > start:
                arr = json.loads(text[start:end + 1])
                if isinstance(arr, list):
                    valid = []
                    seen = set()
                    for idx in arr:
                        if isinstance(idx, int) and 1 <= idx <= len(candidates) and idx not in seen:
                            valid.append(idx)
                            seen.add(idx)
                    return [candidates[i - 1] for i in valid]
        except Exception:
            pass

    all_ids = []
    for m in re.finditer(r'\d+', text):
        idx = int(m.group(0))
        if 1 <= idx <= len(candidates):
            all_ids.append(idx)

    seen = set()
    unique_ids = []
    for idx in all_ids:
        if idx not in seen:
            unique_ids.append(idx)
            seen.add(idx)

    if multi:
        return [candidates[i - 1] for i in unique_ids[:5]]
    if unique_ids:
        return [candidates[unique_ids[0] - 1]]
    return [candidates[0]]


def run_condition(items: list[dict], query_embs, retriever: FastEmbeddingRetriever,
                  llm: LLMClient, prompt_file: str, include_signature: bool,
                  workers: int) -> dict:
    prompt_template = load_prompt(prompt_file.replace(".txt", ""))
    results = {it["qa_id"]: {"gold_symbols": it["gold_symbols"]} for it in items}

    def process(idx_item):
        idx, item = idx_item
        emb = query_embs[idx]
        pool = build_function_pool(emb, retriever)
        single = llm_select(item["question"], pool, llm, prompt_template, include_signature, multi=False)
        multi = llm_select(item["question"], pool, llm, prompt_template, include_signature, multi=True)
        return item["qa_id"], {
            "single": single[0]["name"] if single else "",
            "multi": [c["name"] for c in multi],
            "pool_recall": bool(item["gold_symbols"] & {c["name"] for c in pool}),
        }

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(process, (i, it)) for i, it in enumerate(items)]
        for future in as_completed(futures):
            qa_id, res = future.result()
            results[qa_id].update(res)

    total = len(results)
    single_hits = sum(1 for r in results.values() if r["single"] in r["gold_symbols"])
    multi_hits = sum(1 for r in results.values() if r["gold_symbols"] & set(r["multi"]))
    pool_recalls = sum(1 for r in results.values() if r["pool_recall"])

    return {
        "prompt": prompt_file,
        "signature": include_signature,
        "single_recall": single_hits / total,
        "multi_recall": multi_hits / total,
        "pool_recall": pool_recalls / total,
        "per_item": {k: {
            "single": v["single"],
            "multi": v["multi"],
            "gold_symbols": sorted(v["gold_symbols"]),
            "pool_recall": v["pool_recall"],
        } for k, v in results.items()},
    }


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, default=Path("datasets/benchmark_hard.json"))
    parser.add_argument("--range", default="0,50")
    parser.add_argument("-w", "--workers", type=int, default=5)
    parser.add_argument("-o", "--output", type=Path, default=Path("results/selection_ablation_prompt_design_0_50.json"))
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

    conditions = [
        ("stage1_select_function", False, "A. baseline"),
        ("stage1_select_function_signature", True, "B. baseline+signature"),
        ("stage1_select_function_evidence", True, "C. evidence+signature"),
        ("stage1_select_function_anchor", True, "D. anchor+signature"),
    ]

    summaries = []
    all_results = {}
    for prompt_file, include_sig, label in conditions:
        print(f"\nRunning {label}...")
        result = run_condition(items, query_embs, retriever, llm, prompt_file, include_sig, args.workers)
        all_results[label] = result
        summaries.append({
            "condition": label,
            "prompt": prompt_file,
            "signature": include_sig,
            "pool_recall": result["pool_recall"],
            "single_recall": result["single_recall"],
            "multi_recall": result["multi_recall"],
        })
        print(f"  pool={result['pool_recall']*100:.1f}% "
              f"single={result['single_recall']*100:.1f}% "
              f"multi={result['multi_recall']*100:.1f}%")

    print(f"\n{'='*70}")
    print("Selection Ablation + Prompt Redesign Summary")
    print(f"{'='*70}")
    print(f"{'Condition':<30s} {'Pool':>8s} {'Single':>8s} {'Multi':>8s}")
    for s in summaries:
        print(f"{s['condition']:<30s} {s['pool_recall']*100:>7.1f}% {s['single_recall']*100:>7.1f}% {s['multi_recall']*100:>7.1f}%")

    output = {
        "config": {"range": args.range},
        "summary": summaries,
        "results": all_results,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
