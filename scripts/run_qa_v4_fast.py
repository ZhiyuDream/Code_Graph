#!/usr/bin/env python3
"""
QA V4 Fast: 优化版本（减少 API 调用和 Neo4j 查询）

优化点：
1. 批量获取函数详情（减少 Neo4j 往返）
2. 限制调用链扩展（只扩展 top 3 函数）
3. 减少 embedding 调用（缓存）

用法：
  python run_qa_v4_fast.py --csv results/qav2_test.csv --output results/v4_output.json --workers 4
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Dict
from functools import lru_cache

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

from config import (
    NEO4J_DATABASE, OPENAI_API_KEY, OPENAI_BASE_URL, 
    LLM_MODEL, EMBEDDING_MODEL
)
from src.neo4j_writer import get_driver
from tools.agent_qa import (
    _load_rag_index,
    _cosine_sim,
    _run as neo4j_run,
)
from openai import OpenAI

# 全局缓存
_embedding_cache = {}
_rag_index = None


def get_embedding(client, text: str) -> List[float]:
    """获取 embedding（带缓存）"""
    cache_key = text[:100]  # 缓存前100字符
    if cache_key in _embedding_cache:
        return _embedding_cache[cache_key]
    
    resp = client.embeddings.create(
        model=EMBEDDING_MODEL,
        input=[text[:500]]
    )
    emb = resp.data[0].embedding
    _embedding_cache[cache_key] = emb
    return emb


def semantic_search_fast(client, query: str, top_k: int = 5) -> List[Dict]:
    """快速语义搜索（使用全局索引）"""
    global _rag_index
    if _rag_index is None:
        _rag_index = _load_rag_index()
    
    if _rag_index is None:
        return []
    
    query_emb = get_embedding(client, query)
    
    # 只搜索 function 类型
    scores = []
    for i, chunk in enumerate(_rag_index["chunks"]):
        if chunk["type"] == "function":
            sim = _cosine_sim(query_emb, _rag_index["embeddings"][i])
            scores.append((sim, chunk))
    
    scores.sort(key=lambda x: -x[0])
    
    results = []
    for sim, chunk in scores[:top_k]:
        meta = chunk.get("meta", {})
        results.append({
            "name": meta.get("name", ""),
            "file": meta.get("file", ""),
            "similarity": sim,
            "text": chunk.get("text", "")[:400]
        })
    
    return results


def batch_get_function_details(driver, func_names: List[str]) -> Dict[str, str]:
    """批量获取函数详情"""
    if not func_names:
        return {}
    
    # 使用单个查询批量获取
    results = neo4j_run(driver, """
        MATCH (f:Function)
        WHERE f.name IN $names
        RETURN f.name AS name, f.file_path AS file, 
               f.fan_in AS fan_in, f.fan_out AS fan_out,
               f.annotation_json AS ann
    """, {"names": func_names[:15]})  # 限制数量
    
    details = {}
    for r in results:
        name = r.get("name", "")
        ann = ""
        if r.get("ann"):
            try:
                d = json.loads(r["ann"]) if isinstance(r["ann"], str) else r["ann"]
                ann = d.get("summary", "")[:200]
            except Exception:
                ann = str(r["ann"])[:200]
        
        details[name] = f"函数: {name}\n文件: {r.get('file', '')}\nfan_in={r.get('fan_in', 0)}\n注解: {ann}"
    
    return details


def batch_get_call_relations(driver, func_names: List[str]) -> Dict[str, Dict]:
    """批量获取调用关系"""
    if not func_names:
        return {}
    
    # 批量查询调用者
    caller_results = neo4j_run(driver, """
        MATCH (caller:Function)-[:CALLS]->(callee:Function)
        WHERE callee.name IN $names
        RETURN callee.name AS target, collect(caller.name)[0..3] AS callers
    """, {"names": func_names[:10]})
    
    # 批量查询被调用者
    callee_results = neo4j_run(driver, """
        MATCH (caller:Function)-[:CALLS]->(callee:Function)
        WHERE caller.name IN $names
        RETURN caller.name AS target, collect(callee.name)[0..3] AS callees
    """, {"names": func_names[:10]})
    
    relations = {name: {"callers": [], "callees": []} for name in func_names}
    
    for r in caller_results:
        target = r.get("target", "")
        if target in relations:
            relations[target]["callers"] = r.get("callers", [])
    
    for r in callee_results:
        target = r.get("target", "")
        if target in relations:
            relations[target]["callees"] = r.get("callees", [])
    
    return relations


def build_context_fast(driver, client, question: str) -> Dict:
    """快速构建上下文"""
    # 1. 语义搜索
    semantic_funcs = semantic_search_fast(client, question, top_k=5)
    
    if not semantic_funcs:
        return {"semantic_funcs": [], "details": {}, "relations": {}}
    
    # 2. 批量获取详情（只取 top 3）
    top_func_names = [fn["name"] for fn in semantic_funcs[:3] if fn.get("name")]
    details = batch_get_function_details(driver, top_func_names)
    
    # 3. 批量获取调用关系
    relations = batch_get_call_relations(driver, top_func_names)
    
    return {
        "semantic_funcs": semantic_funcs,
        "details": details,
        "relations": relations
    }


def generate_answer(client, question: str, context: Dict) -> str:
    """生成答案"""
    lines = []
    
    if context.get("semantic_funcs"):
        lines.append("【相关函数】")
        for fn in context["semantic_funcs"]:
            lines.append(f"\n{fn['name']} @ {fn['file']} [相似度: {fn['similarity']:.3f}]")
            if fn.get("text"):
                lines.append(fn["text"][:300])
    
    if context.get("details"):
        lines.append("\n【函数详情】")
        for name, detail in list(context["details"].items())[:3]:
            lines.append(f"\n{detail}")
    
    if context.get("relations"):
        lines.append("\n【调用关系】")
        for name, rel in list(context["relations"].items())[:3]:
            if rel["callers"]:
                lines.append(f"{name} 被调用者: {', '.join(rel['callers'])}")
            if rel["callees"]:
                lines.append(f"{name} 调用: {', '.join(rel['callees'])}")
    
    context_text = "\n".join(lines)
    
    prompt = f"""你是 llama.cpp 代码专家。基于以下信息回答问题：

{context_text}

问题: {question}

用中文回答，引用函数名："""
    
    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=600,
            timeout=30
        )
        return resp.choices[0].message.content or "(无答案)"
    except Exception as e:
        return f"生成失败: {e}"


def process_single(driver, client, row: dict, idx: int) -> dict:
    """处理单个问题"""
    question = row.get("具体问题", "")
    reference = row.get("答案", "")
    
    if idx % 10 == 0:
        print(f"  [{idx}] {question[:40]}...", flush=True)
    
    t0 = time.time()
    try:
        context = build_context_fast(driver, client, question)
        answer = generate_answer(client, question, context)
        latency = time.time() - t0
        
        return {
            "index": idx,
            "具体问题": question,
            "参考答案": reference,
            "生成答案": answer,
            "路由类型": "V4_Fast",
            "延迟_s": latency,
            "错误": None
        }
    except Exception as e:
        latency = time.time() - t0
        return {
            "index": idx,
            "具体问题": question,
            "参考答案": reference,
            "生成答案": "",
            "路由类型": "V4_Fast",
            "延迟_s": latency,
            "错误": str(e)
        }


def main():
    parser = argparse.ArgumentParser(description="QA V4 Fast: 优化版本")
    parser.add_argument("--csv", type=Path, required=True, help="输入 CSV 文件")
    parser.add_argument("--output", type=Path, required=True, help="输出 JSON 文件")
    parser.add_argument("--workers", type=int, default=4, help="并行数")
    args = parser.parse_args()
    
    if not OPENAI_API_KEY:
        print("ERROR: OPENAI_API_KEY 未设置", file=sys.stderr)
        sys.exit(1)
    
    # 读取 CSV
    rows = []
    with open(args.csv, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    print(f"共 {len(rows)} 题需要处理")
    
    # 连接 Neo4j
    driver = get_driver()
    driver.verify_connectivity()
    print("Neo4j 连接成功")
    
    # 预加载 RAG 索引
    global _rag_index
    _rag_index = _load_rag_index()
    print(f"RAG 索引加载完成: {len(_rag_index['chunks']) if _rag_index else 0} chunks")
    
    # OpenAI 客户端
    client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)
    
    # 并行处理
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(process_single, driver, client, row, i): i 
            for i, row in enumerate(rows)
        }
        
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            
            if len(results) % 50 == 0 or len(results) == len(rows):
                print(f"  已完成 {len(results)}/{len(rows)} 题...")
                with open(args.output, "w", encoding="utf-8") as f:
                    json.dump(results, f, ensure_ascii=False, indent=2)
    
    # 最终保存
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    
    print(f"\n完成！结果保存至: {args.output}")
    driver.close()


if __name__ == "__main__":
    main()
