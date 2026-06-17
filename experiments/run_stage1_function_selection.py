#!/usr/bin/env python3
"""
Stage 1 (Function-Level): Function Region → Gold Function Selection。

对比三种输入：
- name_only: file.cpp::function_name()
- signature: file.cpp::function_name(...) [signature]
- signature_body: file.cpp::function_name(...) [signature] + first N lines

让 LLM 从 Top-K function candidates 中选择最相关的 function。

用法:
    python experiments/run_stage1_function_selection.py \
        --benchmark datasets/benchmark_hard.json \
        --range 0,15 \
        --stage0 results/stage0_function_recall_0_50.json \
        --mode name_only \
        --top-k 10 \
        -w 2 \
        -o results/stage1_function_selection_name_only_0_15.json
"""
import json
import re
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.qa.investigation.base import LLMClient, load_prompt


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


def build_function_summary(candidates: list[dict], mode: str, body_lines: int = 20) -> str:
    summaries = []
    for i, c in enumerate(candidates, 1):
        fp = c["file_path"]
        name = c["name"]
        signature = c.get("signature", "")

        if mode == "name_only":
            summaries.append(f"[{i}] {fp}::{name}()")
        elif mode == "signature":
            sig_text = signature.strip() if signature else "(signature unavailable)"
            summaries.append(f"[{i}] {fp}::{name}\n    {sig_text}")
        elif mode == "signature_body":
            sig_text = signature.strip() if signature else "(signature unavailable)"
            content = c.get("content", "")
            # Extract first N lines after signature
            body = ""
            if content:
                lines = content.split('\n')
                # Skip lines that are part of signature or metadata
                body_start = 0
                for j, line in enumerate(lines):
                    if '{' in line or '(' in line:
                        body_start = j + 1
                        break
                body_lines_list = [l for l in lines[body_start:body_start + body_lines] if l.strip()]
                body = '\n    '.join(body_lines_list)
            summaries.append(f"[{i}] {fp}::{name}\n    {sig_text}\n    {body}")
        else:
            raise ValueError(f"Unknown mode: {mode}")

    return "\n\n".join(summaries)


def llm_select_function(question: str, candidates: list[dict], mode: str, llm: LLMClient) -> list[dict]:
    prompt_template = load_prompt("stage1_select_function")
    summaries = build_function_summary(candidates, mode)

    prompt = prompt_template.format(
        question=question,
        file_summaries=summaries,
    )

    text = llm.call(prompt).strip()

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

    return [candidates[idx - 1] for idx in unique_ids[:1]]


def evaluate_function_selection(selected: list[dict], gold_files: list[str], candidates: list[dict]) -> dict:
    norm_gold = [normalize_path(g) for g in gold_files]
    norm_candidates = [normalize_path(c["file_path"]) for c in candidates]

    gold_in_candidates = any(
        any(g == c or g in c or c in g for c in norm_candidates)
        for g in norm_gold
    )

    selected_gold_files = []
    selected_gold_functions = []
    for s in selected:
        s_file = normalize_path(s["file_path"])
        for g in norm_gold:
            if s_file == g or s_file in g or g in s_file:
                selected_gold_files.append(g)
                selected_gold_functions.append(s["name"])
                break

    return {
        "gold_in_candidates": gold_in_candidates,
        "selection_hit": len(selected_gold_files) > 0,
        "selected_gold_files": sorted(set(selected_gold_files)),
        "selected_gold_functions": selected_gold_functions,
    }


def run_item(item: dict, stage0_data: dict, llm: LLMClient, top_k: int, mode: str) -> dict:
    stage0_item = stage0_data.get(item["qa_id"])
    if not stage0_item:
        return {"qa_id": item["qa_id"], "error": "not found in stage0"}

    candidates = stage0_item["candidates"][:top_k]
    selected = llm_select_function(item["question"], candidates, mode, llm)
    eval_result = evaluate_function_selection(selected, item["gold_files"], candidates)

    return {
        "qa_id": item["qa_id"],
        "question": item["question"],
        "gold_files": item["gold_files"],
        "candidates": [{"file_path": c["file_path"], "name": c["name"]} for c in candidates],
        "selected": [{"file_path": s["file_path"], "name": s["name"]} for s in selected],
        "gold_in_candidates": eval_result["gold_in_candidates"],
        "selection_hit": eval_result["selection_hit"],
        "selected_gold_files": eval_result["selected_gold_files"],
        "selected_gold_functions": eval_result["selected_gold_functions"],
    }


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--range", default="0,15")
    parser.add_argument("--stage0", type=Path, required=True)
    parser.add_argument("--mode", choices=["name_only", "signature", "signature_body"], required=True)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("-w", "--workers", type=int, default=2)
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args()

    items = load_items(args.benchmark, args.range)
    print(f"加载 {len(items)} 题，mode={args.mode}, top_k={args.top_k}, workers={args.workers}")

    with open(args.stage0, "r", encoding="utf-8") as f:
        stage0 = json.load(f)
    stage0_by_qa = {r["qa_id"]: r for r in stage0["per_item"]}

    llm = LLMClient()
    results = []
    completed = 0

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(run_item, item, stage0_by_qa, llm, args.top_k, args.mode): item
            for item in items
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            completed += 1
            print(f"  [{completed}/{len(items)}] {result['qa_id']}: "
                  f"gold_in={result.get('gold_in_candidates')} hit={result.get('selection_hit')} "
                  f"selected={result.get('selected', [])}")

    results.sort(key=lambda x: x["qa_id"])

    total = len(results)
    selection_accuracy = sum(1 for r in results if r.get("selection_hit")) / total
    gold_in_count = sum(1 for r in results if r.get("gold_in_candidates"))
    conditional_hits = sum(1 for r in results if r.get("gold_in_candidates") and r.get("selection_hit"))
    conditional_accuracy = conditional_hits / gold_in_count if gold_in_count else 0

    print(f"\n{'='*60}")
    print(f"Stage 1 Function-Level Selection: mode={args.mode}")
    print(f"{'='*60}")
    print(f"总题数: {total}")
    print(f"Gold 在 Top-{args.top_k}: {gold_in_count}/{total} ({gold_in_count/total*100:.1f}%)")
    print(f"Selection Accuracy: {sum(1 for r in results if r.get('selection_hit'))}/{total} ({selection_accuracy*100:.1f}%)")
    print(f"Conditional Selection Accuracy: {conditional_hits}/{gold_in_count} ({conditional_accuracy*100:.1f}%)")

    output = {
        "mode": args.mode,
        "top_k": args.top_k,
        "summary": {
            "total": total,
            "gold_in_count": gold_in_count,
            "gold_in_rate": gold_in_count / total,
            "selection_accuracy": selection_accuracy,
            "conditional_accuracy": conditional_accuracy,
            "conditional_hits": conditional_hits,
        },
        "per_item": results,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
