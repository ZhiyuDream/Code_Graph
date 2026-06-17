#!/usr/bin/env python3
"""
单题深度调试：用 DeepSeek v4 pro 跑单个样本，打印每一步的完整 trace。

用法：
    python debug_run_single.py --index 0 --model deepseek-v4-pro
    python debug_run_single.py --keyword "llama_model_chat_template"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
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
        graph = GraphRetriever(driver, "neo4j")
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


def print_trace(result):
    """打印详细的 trace 信息"""
    print("=" * 80)
    print(f"问题: {result.question}")
    print(f"模型: {result.model if hasattr(result, 'model') else 'N/A'}")
    if result.error:
        print(f"错误: {result.error}")
    print()
    
    print("--- 召回函数列表 ---")
    for i, f in enumerate(result.retrieved_functions[:15], 1):
        body_preview = f.body[:200].replace('\n', ' ') if f.body else "(空)"
        print(f"  {i}. {f.name} @ {f.file_path}:{f.start_line}-{f.end_line}")
        print(f"     level={f.expand_level.name} score={f.score:.3f} source={f.source}")
        print(f"     body_preview={body_preview[:120]}")
        print()
    if len(result.retrieved_functions) > 15:
        print(f"  ... 还有 {len(result.retrieved_functions) - 15} 个")
    print()
    
    print("--- Steps Trace ---")
    for step in result.steps:
        print(f"Step {step.step}: phase={step.phase} action={step.action}")
        print(f"  query={step.query[:80] if step.query else ''}")
        print(f"  retrieved={step.retrieved}")
        print(f"  info_gain={step.info_gain} latency={step.latency_ms:.0f}ms")
        print(f"  tokens={step.token_usage}")
        print()
    
    print("--- 生成答案 ---")
    print(result.answer[:2000])
    if len(result.answer) > 2000:
        print(f"\n... (truncated, total {len(result.answer)} chars)")
    print()
    
    print("--- Token 统计 ---")
    print(f"total_tokens: {result.total_tokens}")
    print(f"total_latency_ms: {result.total_latency_ms:.0f}")
    print("=" * 80)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", type=int, help="题目索引")
    parser.add_argument("--keyword", help="按关键词匹配题目")
    parser.add_argument("--model", default="deepseek-v4-pro", help="模型名称")
    parser.add_argument("--dataset", default="datasets/posthoc_audit_qa.json")
    args = parser.parse_args()
    
    # 加载数据集
    data = json.load(open(args.dataset, "r", encoding="utf-8"))
    if isinstance(data, dict) and "questions" in data:
        questions = data["questions"]
    elif isinstance(data, dict) and "items" in data:
        questions = data["items"]
    else:
        questions = data
    
    # 找题目
    if args.keyword:
        matches = [q for q in questions if args.keyword.lower() in q.get("question", "").lower()]
        if not matches:
            print(f"未找到包含 '{args.keyword}' 的题目")
            return
        item = matches[0]
    elif args.index is not None:
        item = questions[args.index]
    else:
        print("请指定 --index 或 --keyword")
        return
    
    question = item.get("question", "") or item.get("具体问题", "")
    print(f"选中题目: {question[:80]}...")
    print(f"使用模型: {args.model}")
    print()
    
    # 构建 pipeline
    pipeline = build_pipeline(args.model)
    
    # 运行
    reset_usage_stats()
    start = time.time()
    result = pipeline.run(question)
    latency = time.time() - start
    
    # 打印 trace
    print_trace(result)
    
    # 保存详细结果
    output_path = f"/tmp/debug_{args.model.replace('-','_')}_{args.index or args.keyword}.json"
    with open(output_path, "w", encoding="utf-8") as f:
        # 简化序列化：只保存必要字段
        out = {
            "question": result.question,
            "answer": result.answer,
            "error": result.error,
            "steps": [s.to_dict() for s in result.steps],
            "retrieved_functions": [f.to_dict() for f in result.retrieved_functions],
            "total_latency_ms": latency * 1000,
            "total_tokens": result.total_tokens,
        }
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n详细结果保存至: {output_path}")


if __name__ == "__main__":
    main()
