#!/usr/bin/env python3
"""
跑 posthoc_audit_benchmark_v2.json (50题) — 新 QA Pipeline
并行 workers，支持 checkpoint
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))

os.environ.setdefault("LLM_MODEL", "deepseek-v4-pro")
os.environ.setdefault("LLM_DEBUG_LOG", "1")
# 确保 rg 在 PATH 中（服务器重启后可能丢失）
_rg_path = "/data/users/zzy/miniconda3/bin"
if _rg_path not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _rg_path + ":" + os.environ.get("PATH", "")

from config import REPO_ROOT
from src.qa.pipeline import QAPipeline
from src.qa.expansion import CodeExpander
from src.qa.retrievers.grep import GrepRetriever
from src.qa.retrievers.embedding import EmbeddingRetriever
from src.qa.retrievers.graph import GraphRetriever
from src.core.neo4j_client import get_neo4j_driver
from src.qa.runner import QARunner
from src.core.llm_client import get_debug_calls, clear_debug_calls


def build_pipeline(model: str) -> QAPipeline:
    retrievers: list = []
    
    # Grep
    if REPO_ROOT:
        retrievers.append(GrepRetriever(REPO_ROOT, enabled=True))
    
    # Embedding
    emb = EmbeddingRetriever(enabled=True)
    if emb.is_available():
        retrievers.append(emb)
    
    # Graph
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="datasets/posthoc_audit_benchmark_v2.json")
    parser.add_argument("--model", default="deepseek-v4-pro")
    parser.add_argument("-o", "--output", default="results/posthoc_v2_benchmark_results.json")
    parser.add_argument("-w", "--workers", type=int, default=20)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--offset", type=int, default=0)
    args = parser.parse_args()
    
    # 加载数据
    with open(args.dataset, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    items = data.get("items", [])
    print(f"Benchmark: {args.dataset}")
    print(f"总题数: {len(items)}")
    
    # 转换为 runner 期望的格式
    questions = []
    for item in items:
        questions.append({
            "id": item.get("qa_id", ""),
            "question": item.get("question", ""),
            "reference_answer": item.get("reference_answer", ""),
            "target_symbol": item.get("target_symbol", ""),
            "category_level_1": item.get("category", {}).get("level_1", ""),
            "category_level_2": item.get("category", {}).get("level_2", ""),
        })
    
    # 构建 pipeline 和 runner
    pipeline = build_pipeline(args.model)
    runner = QARunner(pipeline, output_dir="./results")
    
    print(f"模型: {args.model}")
    print(f"并行 workers: {args.workers}")
    print(f"输出: {args.output}")
    print()
    
    # 跑 benchmark
    results = runner.run_benchmark(
        questions=questions,
        output_path=args.output,
        workers=args.workers,
        checkpoint_every=5,
        limit=args.limit,
        offset=args.offset,
    )
    
    # 保存 LLM 调用日志
    debug_calls = get_debug_calls()
    if debug_calls:
        debug_path = Path(args.output).with_suffix(".debug.json")
        with open(debug_path, "w", encoding="utf-8") as f:
            json.dump(debug_calls, f, ensure_ascii=False, indent=2)
        print(f"LLM 调用日志: {len(debug_calls)} 条，保存至: {debug_path}")
    
    print(f"\n完成！共处理 {len(results)} 题，结果保存至: {args.output}")


if __name__ == "__main__":
    main()
