#!/usr/bin/env python3
"""
Stage 1 Ablation: LLM 选择入口时输入什么信息最有用？

对比三种输入：
- filename_only: 只给文件名
- filename_symbol: 给文件名 + 文件中提取的关键 symbols
- full_content: 给文件名 + 截断的代码内容（Stage 1 v3 默认）

用法:
    python experiments/run_stage1_ablation.py \
        --benchmark datasets/benchmark_hard.json \
        --range 0,15 \
        --stage0 results/stage0_region_recall_0_50.json \
        --mode filename_only \
        --top-k 10 \
        -w 2 \
        -o results/stage1_ablation_filename_only_0_15.json
"""
import json
import re
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.qa.investigation.base import LLMClient, load_prompt, BaseInvestigator
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


def extract_symbols_from_file(file_path: str) -> list[str]:
    """从文件中提取关键 symbols。"""
    try:
        content = read_full_file(file_path)
    except Exception:
        return []
    # Use BaseInvestigator's symbol extraction
    investigator = BaseInvestigator.__new__(BaseInvestigator)
    return investigator.extract_symbols(content)[:15]


def build_file_summaries(candidates: list[str], mode: str, max_chars: int = 3000) -> str:
    """根据 mode 构建给 LLM 的候选文件描述。"""
    summaries = []
    for i, fp in enumerate(candidates, 1):
        if mode == "filename_only":
            summaries.append(f"[{i}] {fp}")
        elif mode == "filename_symbol":
            symbols = extract_symbols_from_file(fp)
            symbol_text = "\n  symbols: " + ", ".join(symbols) if symbols else ""
            summaries.append(f"[{i}] {fp}{symbol_text}")
        elif mode == "full_content":
            try:
                content = read_full_file(fp)
                if len(content) > max_chars:
                    half = max_chars // 2
                    content = content[:half] + "\n\n... [中间内容省略] ...\n\n" + content[-(max_chars - half):]
            except Exception as e:
                content = f"[Error reading file: {e}]"
            summaries.append(f"[{i}] {fp}\n{content}")
        else:
            raise ValueError(f"Unknown mode: {mode}")
    return "\n\n".join(summaries)


def llm_select_entry(question: str, candidates: list[str], mode: str, llm: LLMClient) -> list[str]:
    """让 LLM 从 region 中选择最相关的入口文件。"""
    prompt_template = load_prompt("stage1_select_entry_ablation")
    file_summaries = build_file_summaries(candidates, mode)

    prompt = prompt_template.format(
        question=question,
        file_summaries=file_summaries,
    )

    text = llm.call(prompt).strip()

    # Parse selected IDs: find all integers in response, keep valid candidate IDs
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


def evaluate_selection(selected: list[str], gold_files: list[str], candidates: list[str]) -> dict:
    norm_selected = [normalize_path(s) for s in selected]
    norm_gold = [normalize_path(g) for g in gold_files]
    norm_candidates = [normalize_path(c) for c in candidates]

    gold_in_candidates = any(
        any(g == c or g in c or c in g for c in norm_candidates)
        for g in norm_gold
    )

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
    }


def run_item(item: dict, stage0_data: dict, llm: LLMClient, top_k: int, mode: str) -> dict:
    stage0_item = stage0_data.get(item["qa_id"])
    if not stage0_item:
        return {"qa_id": item["qa_id"], "error": "not found in stage0"}

    candidates = stage0_item["candidates"][:top_k]
    selected = llm_select_entry(item["question"], candidates, mode, llm)
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
    parser.add_argument("--stage0", type=Path, required=True)
    parser.add_argument("--mode", choices=["filename_only", "filename_symbol", "full_content"], required=True)
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
                  f"gold_in={result.get('gold_in_candidates')} hit={result.get('selection_hit')} selected={result.get('selected_files', [])}")

    results.sort(key=lambda x: x["qa_id"])

    total = len(results)
    selection_accuracy = sum(1 for r in results if r.get("selection_hit")) / total
    gold_in_count = sum(1 for r in results if r.get("gold_in_candidates"))
    conditional_hits = sum(1 for r in results if r.get("gold_in_candidates") and r.get("selection_hit"))
    conditional_accuracy = conditional_hits / gold_in_count if gold_in_count else 0

    print(f"\n{'='*60}")
    print(f"Stage 1 Ablation: mode={args.mode}")
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
