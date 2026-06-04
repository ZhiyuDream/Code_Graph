#!/usr/bin/env python3
"""
跑 posthoc_audit_benchmark_v2.json (50题) — 新 QA Pipeline
先串行初始化，再并行执行，避免20 worker同时初始化卡住
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))

os.environ.setdefault("LLM_MODEL", "deepseek-v4-pro")

from config import REPO_ROOT
from src.qa.pipeline import QAPipeline
from src.qa.expansion import CodeExpander
from src.qa.retrievers.grep import GrepRetriever
from src.qa.retrievers.embedding import EmbeddingRetriever
from src.qa.retrievers.graph import GraphRetriever
from src.core.neo4j_client import get_neo4j_driver
from src.core.llm_client import reset_usage_stats


def build_pipeline(model: str) -> QAPipeline:
    retrievers: list = []
    if REPO_ROOT:
        retrievers.append(GrepRetriever(REPO_ROOT, enabled=True))
    emb = EmbeddingRetriever(enabled=True)
    if emb.is_available():
        retrievers.append(emb)
    try:
        driver = get_neo4j_driver()
        graph = GraphRetriever(driver, REPO_ROOT or "")
        if graph.is_available():
            retrievers.append(graph)
    except Exception as e:
        print(f"[WARN] Graph retriever init failed: {e}")
    return QAPipeline(
        retrievers=retrievers,
        expander=CodeExpander(),
        enable_react=True,
        max_react_steps=5,
        model=model,
        repo_root=str(REPO_ROOT) if REPO_ROOT else "",
    )


def process_one(pipeline: QAPipeline, question: str, idx: int, item: dict) -> dict:
    reset_usage_stats()
    t0 = time.perf_counter()
    try:
        result = pipeline.run(question)
        latency = (time.perf_counter() - t0) * 1000
        return {
            "index": idx,
            "id": item.get("qa_id", f"qa_{idx}"),
            "question": question,
            "reference_answer": item.get("reference_answer", ""),
            "target_symbol": item.get("target_symbol", ""),
            "answer": result.answer,
            "retrieved_functions": [f.to_dict() for f in result.retrieved_functions],
            "steps": [s.to_dict() for s in result.steps],
            "total_latency_ms": round(result.total_latency_ms, 2),
            "total_tokens": result.total_tokens,
            "error": result.error,
            "latency_ms": round(latency, 2),
        }
    except Exception as e:
        import traceback
        return {
            "index": idx,
            "id": item.get("qa_id", f"qa_{idx}"),
            "question": question,
            "reference_answer": item.get("reference_answer", ""),
            "answer": "",
            "error": f"{e}\n{traceback.format_exc()}",
            "latency_ms": round((time.perf_counter() - t0) * 1000, 2),
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="datasets/posthoc_audit_benchmark_v2.json")
    parser.add_argument("--model", default="deepseek-v4-pro")
    parser.add_argument("-o", "--output", default="results/posthoc_v2_benchmark_results.json")
    parser.add_argument("-w", "--workers", type=int, default=20)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--offset", type=int, default=0)
    args = parser.parse_args()

    with open(args.dataset, "r", encoding="utf-8") as f:
        data = json.load(f)
    items = data.get("items", [])
    
    if args.offset > 0:
        items = items[args.offset:]
    if args.limit > 0:
        items = items[:args.limit]

    print(f"Benchmark: {args.dataset}")
    print(f"总题数: {len(items)}")
    print(f"模型: {args.model}")
    print(f"并行 workers: {args.workers}")
    print()

    # Step 1: 串行初始化 pipeline（关键：避免多线程同时初始化卡住）
    print("[1/3] 初始化 Pipeline...")
    pipeline = build_pipeline(args.model)
    print("[1/3] Pipeline 初始化完成")
    print()

    # Step 2: 先跑一题热身（确保所有 lazy-load 完成）
    print("[2/3] 热身运行（第1题）...")
    warm_item = items[0]
    warm_q = warm_item.get("question", "")
    _ = process_one(pipeline, warm_q, 0, warm_item)
    print("[2/3] 热身完成")
    print()

    # Step 3: 并行跑剩余题目
    print(f"[3/3] 并行跑题（workers={args.workers}）...")
    results = []
    completed = 0
    total = len(items)
    
    output_path = Path(args.output)

    if args.workers <= 1:
        for i, item in enumerate(items):
            q = item.get("question", "")
            r = process_one(pipeline, q, i, item)
            results.append(r)
            completed += 1
            if completed % 5 == 0 or completed == total:
                print(f"  已完成 {completed}/{total} 题...")
                _save(results, output_path)
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(process_one, pipeline, item.get("question", ""), i, item): i
                for i, item in enumerate(items)
            }
            for future in as_completed(futures):
                try:
                    result = future.result()
                    results.append(result)
                    completed += 1
                    if completed % 5 == 0 or completed == total:
                        print(f"  已完成 {completed}/{total} 题...")
                        _save(results, output_path)
                except Exception as e:
                    print(f"  Future error: {e}")
                    completed += 1

    results.sort(key=lambda x: x.get("index", 0))
    _save(results, output_path)
    print(f"\n完成！共处理 {len(results)} 题，结果保存至: {args.output}")


def _save(results: list, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
