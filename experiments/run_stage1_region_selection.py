#!/usr/bin/env python3
"""
Stage 1: Region → Gold Entry Selection。

给定 Question + Embedding Top-K candidates，让 LLM 选择 1 个最相关的入口文件。

测量：
- Selection Accuracy：选中 gold file 的比例
- Conditional Selection Accuracy：在 gold 确实在 Top-K 中的题目里，选中 gold 的比例
- 也支持让 LLM 选择 Top-3（为 multi-branch 做准备）

用法:
    python experiments/run_stage1_region_selection.py \
        --benchmark datasets/benchmark_hard.json \
        --range 0,15 \
        --stage0 results/stage0_region_recall_0_50.json \
        --top-k 10 \
        --select-top 1 \
        -w 2 \
        -o results/stage1_region_selection_0_15.json
"""
import json
import re
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.qa.investigation.base import LLMClient, load_prompt
from src.search.code_reader import read_full_file


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


def read_region_files(candidates: list[str], max_chars: int = 3000) -> dict[str, str]:
    """读取候选文件内容，大文件截断。"""
    contents = {}
    for fp in candidates:
        try:
            content = read_full_file(fp)
            if len(content) > max_chars:
                half = max_chars // 2
                content = content[:half] + "\n\n... [中间内容省略] ...\n\n" + content[-(max_chars - half):]
            contents[fp] = content
        except Exception as e:
            contents[fp] = f"[Error reading file: {e}]"
    return contents


def llm_select_entry(question: str, candidates: list[str], contents: dict[str, str], llm: LLMClient, select_top: int) -> list[str]:
    """让 LLM 从 region 中选择最相关的入口文件。返回候选路径列表。"""
    prompt_template = load_prompt("stage1_select_entry")

    file_summaries = []
    for i, fp in enumerate(candidates, 1):
        file_summaries.append(f"[{i}] {fp}\n{contents.get(fp, '')}")

    prompt = prompt_template.format(
        question=question,
        select_top=select_top,
        file_summaries="\n\n".join(file_summaries),
    )

    text = llm.call(prompt).strip()

    # Parse selected IDs: find all integers in the response, keep valid candidate IDs
    all_ids = []
    for m in re.finditer(r'\d+', text):
        idx = int(m.group(0))
        if 1 <= idx <= len(candidates):
            all_ids.append(idx)

    # Remove duplicates while preserving order
    seen = set()
    unique_ids = []
    for idx in all_ids:
        if idx not in seen:
            unique_ids.append(idx)
            seen.add(idx)

    # Map IDs to candidates (1-based)
    matched = [candidates[idx - 1] for idx in unique_ids]
    return matched[:select_top]


def evaluate_selection(selected: list[str], gold_files: list[str], candidates: list[str]) -> dict:
    """评估选择结果。"""
    norm_selected = [normalize_path(s) for s in selected]
    norm_gold = [normalize_path(g) for g in gold_files]
    norm_candidates = [normalize_path(c) for c in candidates]

    # Is there any gold in candidates?
    gold_in_candidates = any(
        any(g == c or g in c or c in g for c in norm_candidates)
        for g in norm_gold
    )

    # Did LLM select a gold file?
    selected_gold = []
    for s in norm_selected:
        for g in norm_gold:
            if s == g or s in g or g in s:
                selected_gold.append(g)
                break

    return {
        "gold_in_candidates": gold_in_candidates,
        "selected_gold": sorted(set(selected_gold)),
        "selection_hit": len(selected_gold) > 0,
        "num_selected": len(norm_selected),
        "num_gold": len(norm_gold),
    }


def run_item(item: dict, stage0_data: dict, llm: LLMClient, top_k: int, select_top: int) -> dict:
    # Get candidates from stage0
    stage0_item = stage0_data.get(item["qa_id"])
    if not stage0_item:
        return {
            "qa_id": item["qa_id"],
            "error": "not found in stage0",
        }

    candidates = stage0_item["candidates"][:top_k]
    contents = read_region_files(candidates)
    selected = llm_select_entry(item["question"], candidates, contents, llm, select_top)
    eval_result = evaluate_selection(selected, item["gold_files"], candidates)

    return {
        "qa_id": item["qa_id"],
        "question": item["question"],
        "gold_files": item["gold_files"],
        "candidates": candidates,
        "selected_files": selected,
        "gold_in_candidates": eval_result["gold_in_candidates"],
        "selection_hit": eval_result["selection_hit"],
        "selected_gold": eval_result["selected_gold"],
    }


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--range", default="0,15")
    parser.add_argument("--stage0", type=Path, required=True, help="Stage 0 结果文件")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--select-top", type=int, default=1, help="LLM 选择几个文件")
    parser.add_argument("-w", "--workers", type=int, default=2)
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args()

    items = load_items(args.benchmark, args.range)
    print(f"加载 {len(items)} 题，top_k={args.top_k}, select_top={args.select_top}, workers={args.workers}")

    # Load stage0 per-item results
    with open(args.stage0, "r", encoding="utf-8") as f:
        stage0 = json.load(f)
    stage0_by_qa = {r["qa_id"]: r for r in stage0["per_item"]}

    llm = LLMClient()
    results = []
    completed = 0

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(run_item, item, stage0_by_qa, llm, args.top_k, args.select_top): item
            for item in items
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            completed += 1
            hit = result.get("selection_hit", False)
            gold_in = result.get("gold_in_candidates", False)
            print(f"  [{completed}/{len(items)}] {result['qa_id']}: "
                  f"gold_in_candidates={gold_in} selected_hit={hit} selected={result.get('selected_files', [])}")

    results.sort(key=lambda x: x["qa_id"])

    # Summary
    total = len(results)
    selection_accuracy = sum(1 for r in results if r.get("selection_hit")) / total
    gold_in_candidates_count = sum(1 for r in results if r.get("gold_in_candidates"))
    conditional_hits = sum(
        1 for r in results
        if r.get("gold_in_candidates") and r.get("selection_hit")
    )
    conditional_accuracy = conditional_hits / gold_in_candidates_count if gold_in_candidates_count else 0

    print(f"\n{'='*60}")
    print(f"Stage 1: Region → Gold Entry Selection (Top-K={args.top_k}, Select-Top={args.select_top})")
    print(f"{'='*60}")
    print(f"总题数: {total}")
    print(f"Gold 在 Top-{args.top_k} 中的题目数: {gold_in_candidates_count}/{total} ({gold_in_candidates_count/total*100:.1f}%)")
    print(f"Selection Accuracy: {sum(1 for r in results if r.get('selection_hit'))}/{total} ({selection_accuracy*100:.1f}%)")
    print(f"Conditional Selection Accuracy: {conditional_hits}/{gold_in_candidates_count} ({conditional_accuracy*100:.1f}%)")

    output = {
        "top_k": args.top_k,
        "select_top": args.select_top,
        "summary": {
            "total": total,
            "gold_in_candidates_count": gold_in_candidates_count,
            "gold_in_candidates_rate": gold_in_candidates_count / total,
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
