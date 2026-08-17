#!/usr/bin/env python3
"""
Top-20 functions → ReAct investigation experiment.

For each hard benchmark question:
  1. Retrieve top-20 functions via embedding
  2. Extract unique file paths as initial candidate files
  3. Run ReactInvestigationAgent in top20_react mode (max 10 steps)
  4. Evaluate coverage of gold evidence files

This tests whether sequential investigation with callers/callees/read/search
can locate gold evidence after embedding has returned relevant files.
"""
import json
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.qa.candidate_pool import build_function_pool
from src.qa.investigation.base import LLMClient
from src.qa.react_agent import ReactInvestigationAgent
from src.qa.retrievers.fast_embedding import FastEmbeddingRetriever


def normalize_path(path: str) -> str:
    return path.lstrip("./").lstrip("/")


def compute_coverage(gold_files: list[str], visited_files: list[str]) -> float:
    if not gold_files:
        return 1.0
    gold_norm = {normalize_path(f) for f in gold_files}
    visited_norm = {normalize_path(f) for f in visited_files}
    hits = len(gold_norm & visited_norm)
    return hits / len(gold_norm)


def run_item(item: dict, query_emb, retriever: FastEmbeddingRetriever,
             max_steps: int) -> dict:
    # Build top-20 function pool and extract unique file paths
    pool = build_function_pool(query_emb, retriever, max_pool_size=20)
    initial_files = []
    seen = set()
    for c in pool:
        fp = c.get("file_path", "")
        if fp and fp not in seen:
            seen.add(fp)
            initial_files.append(fp)

    agent = ReactInvestigationAgent(mode="top20_react", max_steps=max_steps)
    result = agent.investigate(
        question=item["question"],
        qa_id=item["qa_id"],
        initial_files=initial_files,
    )

    coverage = compute_coverage(item["gold_files"], result.visited_files)

    return {
        "qa_id": item["qa_id"],
        "question": item["question"],
        "gold_files": item["gold_files"],
        "initial_files": initial_files,
        "visited_files": result.visited_files,
        "coverage": coverage,
        "num_steps": len(result.steps),
        "num_visited": len(result.visited_files),
        "answer": result.answer,
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
    }


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, default=Path("datasets/benchmark_hard.json"))
    parser.add_argument("--range", default="0,50")
    parser.add_argument("--max-steps", type=int, default=10)
    parser.add_argument("-w", "--workers", type=int, default=3)
    parser.add_argument("-o", "--output", type=Path, default=Path("results/top20_react_investigation_hard_0_50.json"))
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
        gold_files = sorted(set(
            ev["file"] for ev in item.get("gold_evidence", [])
            if not ev["file"].endswith((".h", ".hpp"))
        ))
        items.append({
            "qa_id": item.get("qa_id", f"q{idx}"),
            "question": item.get("question", ""),
            "gold_files": gold_files,
        })

    retriever = FastEmbeddingRetriever()
    query_embs = retriever.encode_queries([it["question"] for it in items])

    results = []
    completed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(run_item, item, query_embs[i], retriever, args.max_steps): item
            for i, item in enumerate(items)
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            completed += 1
            print(f"  [{completed}/{len(items)}] {result['qa_id']}: "
                  f"coverage={result['coverage']*100:.0f}% "
                  f"steps={result['num_steps']} "
                  f"visited={result['num_visited']}")

    results.sort(key=lambda x: x["qa_id"])
    total = len(results)
    full_cov = sum(1 for r in results if r["coverage"] >= 1.0)
    avg_cov = sum(r["coverage"] for r in results) / total
    avg_steps = sum(r["num_steps"] for r in results) / total
    avg_visited = sum(r["num_visited"] for r in results) / total

    print(f"\n{'='*60}")
    print(f"Top-20 Functions → ReAct Investigation ({total} questions)")
    print(f"{'='*60}")
    print(f"Full coverage: {full_cov}/{total} = {full_cov/total*100:.1f}%")
    print(f"Avg coverage: {avg_cov*100:.1f}%")
    print(f"Avg steps: {avg_steps:.1f}")
    print(f"Avg visited files: {avg_visited:.1f}")

    output = {
        "config": {"max_steps": args.max_steps, "range": args.range},
        "summary": {
            "total": total,
            "full_coverage": full_cov,
            "full_coverage_rate": full_cov / total,
            "avg_coverage": avg_cov,
            "avg_steps": avg_steps,
            "avg_visited": avg_visited,
        },
        "per_item": results,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
