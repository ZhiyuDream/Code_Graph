#!/usr/bin/env python3
"""
增强版单题调试：记录每一步的完整 prompt、LLM 返回、上下文内容。
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
from src.core import llm_client

# Hook call_llm 和 call_llm_json 以记录完整输入输出
_llm_log: list[dict] = []
_original_call_llm = llm_client.call_llm
_original_call_llm_json = llm_client.call_llm_json

def _hooked_call_llm(messages, max_tokens=2000, timeout=600, model=None, _usage_sink=None, **extra):
    start = time.time()
    result = _original_call_llm(messages, max_tokens, timeout, model=model, _usage_sink=_usage_sink, **extra)
    latency = time.time() - start
    _llm_log.append({
        "type": "call_llm",
        "model": model,
        "max_tokens": max_tokens,
        "prompt_preview": messages[0]["content"][:500] if messages else "",
        "prompt_len": len(messages[0]["content"]) if messages else 0,
        "response_preview": result[:500] if result else "",
        "response_len": len(result) if result else 0,
        "latency_ms": round(latency * 1000, 1),
    })
    return result

def _hooked_call_llm_json(messages, max_tokens=500, timeout=600, model=None, _usage_sink=None):
    start = time.time()
    result = _original_call_llm_json(messages, max_tokens, timeout, model=model, _usage_sink=_usage_sink)
    latency = time.time() - start
    prompt_text = messages[0]["content"] if messages else ""
    _llm_log.append({
        "type": "call_llm_json",
        "model": model,
        "max_tokens": max_tokens,
        "prompt_preview": prompt_text[:500],
        "prompt_len": len(prompt_text),
        "response": result,
        "latency_ms": round(latency * 1000, 1),
    })
    return result

llm_client.call_llm = _hooked_call_llm
llm_client.call_llm_json = _hooked_call_llm_json


def build_pipeline(model: str) -> QAPipeline:
    retrievers: list = []
    if REPO_ROOT:
        retrievers.append(GrepRetriever(REPO_ROOT, enabled=True))
    emb = EmbeddingRetriever(enabled=True)
    if emb.is_available():
        retrievers.append(emb)
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


def analyze_result(result, output_path: str):
    """详细分析并保存"""
    report = {
        "question": result.question,
        "answer": result.answer,
        "error": result.error,
        "total_tokens": result.total_tokens,
        "total_latency_ms": result.total_latency_ms,
        "llm_calls": _llm_log,
        "steps": [s.to_dict() for s in result.steps],
        "retrieved_functions": [f.to_dict() for f in result.retrieved_functions],
        "context_analysis": {},
    }

    # 分析每个函数的 body 长度和 expand_level
    func_analysis = []
    total_body = 0
    for f in result.retrieved_functions:
        body_len = len(f.body) if f.body else 0
        total_body += body_len
        func_analysis.append({
            "name": f.name,
            "file_path": f.file_path,
            "expand_level": f.expand_level.name,
            "body_len": body_len,
            "source": f.source,
            "score": f.score,
        })
    report["context_analysis"] = {
        "function_count": len(result.retrieved_functions),
        "total_body_chars": total_body,
        "functions": func_analysis,
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    # 打印摘要
    print("=" * 80)
    print(f"问题: {result.question[:100]}...")
    print(f"答案预览: {result.answer[:300]}...")
    print()
    print(f"召回函数数: {len(result.retrieved_functions)}")
    print(f"Body 总字符数: {total_body}")
    print(f"LLM 调用次数: {len(_llm_log)}")
    for i, log in enumerate(_llm_log):
        resp_info = log.get('response_len', len(str(log.get('response', ''))))
        print(f"  [{i+1}] {log['type']} model={log['model']} prompt={log['prompt_len']} resp={resp_info} ({log['latency_ms']}ms)")
    print(f"详细报告: {output_path}")
    print("=" * 80)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", type=int, required=True)
    parser.add_argument("--model", default="deepseek-v4-pro")
    parser.add_argument("--dataset", default="datasets/posthoc_audit_qa.json")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    data = json.load(open(args.dataset, "r", encoding="utf-8"))
    items = data if isinstance(data, list) else data.get("items", data.get("questions", []))
    item = items[args.index]
    question = item.get("question", "") or item.get("具体问题", "")

    print(f"运行 index={args.index}: {question[:80]}...")
    pipeline = build_pipeline(args.model)
    reset_usage_stats()
    _llm_log.clear()
    result = pipeline.run(question)

    output = args.output or f"/tmp/debug_verbose_{args.model.replace('-', '_')}_{args.index}.json"
    analyze_result(result, output)


if __name__ == "__main__":
    main()
