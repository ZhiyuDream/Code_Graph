#!/usr/bin/env python3
"""
三组实验对比：
1. Baseline: Top20 → Answer（已有结果，直接读取）
2. Top20 + ReAct: Top20 → Agent 逐步调查 → Answer
3. Pure ReAct: Question → search_symbol → read → ... → Answer

用法:
    python experiments/run_react_comparison.py \
        --benchmark datasets/benchmark_hard.json \
        --baseline results/benchmark_hard_20260607_200601.json \
        --mode pure_react \
        --range 0,5 \
        -o results/react_pure_0_5.json

    python experiments/run_react_comparison.py \
        --benchmark datasets/posthoc_audit_benchmark_v2.json \
        --baseline results/benchmark_symbol_fastpath_20260607_131010.json \
        --mode top20_react \
        --range easy \
        -o results/react_top20_easy.json
"""
import json
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.qa.react_agent import ReactInvestigationAgent


def load_data(benchmark_path: Path, baseline_path: Path, range_str: str):
    """加载 benchmark 和 baseline 结果。"""
    with open(benchmark_path, "r", encoding="utf-8") as f:
        bench = json.load(f)
    with open(baseline_path, "r", encoding="utf-8") as f:
        baseline = json.load(f)

    if isinstance(bench, dict) and "items" in bench:
        bench_items = bench["items"]
    elif isinstance(bench, list):
        bench_items = bench
    else:
        raise ValueError("Unknown benchmark format")

    # Determine range
    if range_str == "easy":
        start, end = 0, min(50, len(bench_items))
    elif range_str == "hard":
        start, end = min(50, len(bench_items)), len(bench_items)
    elif "," in range_str:
        start, end = map(int, range_str.split(","))
    else:
        start, end = 0, len(bench_items)

    items = []
    for idx in range(start, end):
        bench_item = bench_items[idx]
        baseline_idx = idx - start if len(baseline) == (end - start) else idx
        if baseline_idx >= len(baseline):
            continue
        baseline_result = baseline[baseline_idx]

        # Extract initial files from baseline's retrieved_functions
        initial_files = []
        for fn in baseline_result.get("retrieved_functions", []):
            fp = fn.get("file_path", fn.get("metadata", {}).get("file_path", ""))
            if fp:
                initial_files.append(fp)

        gold_files = sorted(set(
            ev["file"] for ev in bench_item.get("gold_evidence", [])
            if not ev["file"].endswith((".h", ".hpp"))
        ))

        items.append({
            "qa_id": bench_item.get("qa_id", f"q{idx}"),
            "question": bench_item.get("question", ""),
            "reference_answer": bench_item.get("reference_answer", ""),
            "gold_files": gold_files,
            "initial_files": initial_files,
            "baseline_answer": baseline_result.get("answer", ""),
            "baseline_retrieved": baseline_result.get("retrieved_functions", []),
        })

    return items


def run_single(item: dict, mode: str, max_steps: int) -> dict:
    """跑一道题。"""
    agent = ReactInvestigationAgent(mode=mode, max_steps=max_steps)
    result = agent.investigate(
        question=item["question"],
        qa_id=item["qa_id"],
        initial_files=item["initial_files"] if mode == "top20_react" else None,
    )

    return {
        "qa_id": item["qa_id"],
        "question": item["question"],
        "mode": mode,
        "gold_files": item["gold_files"],
        "baseline_answer": item["baseline_answer"],
        "react_answer": result.answer,
        "steps": [
            {
                "step": s.step,
                "action": s.action,
                "action_input": s.action_input,
                "thought": s.thought,
                "observation": s.observation[:500],
                "files_accessed": s.files_accessed,
            }
            for s in result.steps
        ],
        "visited_files": result.visited_files,
        "candidate_files": result.candidate_files,
        "num_steps": len(result.steps),
        "num_visited": len(result.visited_files),
        "baseline_num_retrieved": len(item["initial_files"]),
    }


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--mode", choices=["pure_react", "top20_react"], required=True)
    parser.add_argument("--range", default="easy", help="easy|hard|0,5|0,50")
    parser.add_argument("--max-steps", type=int, default=10)
    parser.add_argument("-w", "--workers", type=int, default=5)
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args()

    items = load_data(args.benchmark, args.baseline, args.range)
    print(f"加载 {len(items)} 题，模式: {args.mode}, max_steps: {args.max_steps}, workers: {args.workers}")

    results = []
    completed = 0

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(run_single, item, args.mode, args.max_steps): item
            for item in items
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            completed += 1
            if completed % 1 == 0:
                print(f"  [{completed}/{len(items)}] {result['qa_id']}: {result['num_steps']} steps, {result['num_visited']} files")

    results.sort(key=lambda x: x["qa_id"])

    # Summary
    avg_steps = sum(r["num_steps"] for r in results) / len(results)
    avg_visited = sum(r["num_visited"] for r in results) / len(results)
    avg_baseline_retrieved = sum(r["baseline_num_retrieved"] for r in results) / len(results)

    print(f"\n{'='*60}")
    print("实验结果汇总")
    print(f"{'='*60}")
    print(f"模式: {args.mode}")
    print(f"总题数: {len(results)}")
    print(f"平均步数: {avg_steps:.1f}")
    print(f"平均访问文件数: {avg_visited:.1f}")
    print(f"Baseline 平均检索文件数: {avg_baseline_retrieved:.1f}")

    # Per-question detail
    print(f"\n{'='*60}")
    print("逐题详情")
    print(f"{'='*60}")
    for r in results:
        print(f"{r['qa_id']}: {r['num_steps']} steps, visited {r['num_visited']} files "
              f"(baseline retrieved {r['baseline_num_retrieved']})")
        print(f"  Actions: {' → '.join(s['action'] for s in r['steps'])}")

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
