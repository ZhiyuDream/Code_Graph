#!/usr/bin/env python3
"""
QA V6: 混合检索 (BM25 + Embedding + RRF)

核心思路：
1. BM25 关键词检索：精确匹配函数名、文件名
2. Embedding 语义检索：匹配功能描述
3. Reciprocal Rank Fusion (RRF)：融合多路结果

废弃单独依赖 embedding，采用多路召回 + 重排序
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import re
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Dict, Tuple
from collections import defaultdict

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
    tool_get_function_detail,
    tool_get_callers,
    tool_get_callees,
    _load_issue_index,
)
from openai import OpenAI

# RRF 常数
RRF_K = 60

# 全局缓存
_rag_index = None
_issue_index = None
_embedding_cache = {}


def get_embedding(client, text: str) -> List[float]:
    """获取 embedding（带缓存）"""
    cache_key = text[:100]
    if cache_key in _embedding_cache:
        return _embedding_cache[cache_key]
    
    resp = client.embeddings.create(
        model=EMBEDDING_MODEL,
        input=[text[:500]]
    )
    emb = resp.data[0].embedding
    _embedding_cache[cache_key] = emb
    return emb


def bm25_search(query: str, top_k: int = 10) -> List[Dict]:
    """
    BM25 风格的关键词检索
    匹配函数名、文件名中的关键词
    """
    global _rag_index
    if _rag_index is None:
        _rag_index = _load_rag_index()
    if _rag_index is None:
        return []
    
    # 提取查询关键词
    query_lower = query.lower()
    keywords = [w for w in re.findall(r'\b[a-z_][a-z0-9_]*\b', query_lower) 
                if len(w) >= 3 and w not in ['the', 'and', 'for', 'are', 'this', 'that', 'with']]
    
    if not keywords:
        keywords = [query_lower[:20]]
    
    # 计算 BM25 风格分数
    scores = []
    for i, chunk in enumerate(_rag_index["chunks"]):
        if chunk["type"] != "function":
            continue
        
        meta = chunk.get("meta", {})
        name = meta.get("name", "").lower()
        file = meta.get("file", "").lower()
        text = chunk.get("text", "").lower()
        
        # 计算匹配分数
        score = 0
        for kw in keywords:
            # 函数名完全匹配权重最高
            if kw == name:
                score += 10
            elif kw in name:
                score += 5
            # 文件名匹配
            if kw in file:
                score += 3
            # 代码文本匹配
            if kw in text:
                score += 1
        
        if score > 0:
            scores.append((score, chunk))
    
    scores.sort(key=lambda x: -x[0])
    
    results = []
    for score, chunk in scores[:top_k]:
        meta = chunk.get("meta", {})
        results.append({
            "type": "bm25",
            "name": meta.get("name", ""),
            "file": meta.get("file", ""),
            "score": score,
            "text": chunk.get("text", "")[:300]
        })
    
    return results


def embedding_search(client, query: str, top_k: int = 10) -> List[Dict]:
    """Embedding 语义检索"""
    global _rag_index
    if _rag_index is None:
        _rag_index = _load_rag_index()
    if _rag_index is None:
        return []
    
    query_emb = get_embedding(client, query)
    
    scores = []
    for i, chunk in enumerate(_rag_index["chunks"]):
        if chunk["type"] != "function":
            continue
        sim = _cosine_sim(query_emb, _rag_index["embeddings"][i])
        scores.append((sim, chunk))
    
    scores.sort(key=lambda x: -x[0])
    
    results = []
    for sim, chunk in scores[:top_k]:
        meta = chunk.get("meta", {})
        results.append({
            "type": "embedding",
            "name": meta.get("name", ""),
            "file": meta.get("file", ""),
            "score": sim,
            "text": chunk.get("text", "")[:300]
        })
    
    return results


def reciprocal_rank_fusion(bm25_results: List[Dict], emb_results: List[Dict], top_k: int = 8) -> List[Dict]:
    """
    RRF: Reciprocal Rank Fusion
    score = Σ 1/(k + rank)
    """
    scores = defaultdict(float)
    sources = defaultdict(list)
    
    # BM25 结果
    for rank, r in enumerate(bm25_results):
        key = (r["name"], r["file"])
        scores[key] += 1.0 / (RRF_K + rank + 1)
        sources[key].append("bm25")
    
    # Embedding 结果
    for rank, r in enumerate(emb_results):
        key = (r["name"], r["file"])
        scores[key] += 1.0 / (RRF_K + rank + 1)
        sources[key].append("embedding")
    
    # 排序
    ranked = sorted(scores.items(), key=lambda x: -x[1])
    
    # 构建结果
    results = []
    for (name, file), rrf_score in ranked[:top_k]:
        # 找到原始结果
        text = ""
        for r in bm25_results + emb_results:
            if r["name"] == name and r["file"] == file:
                text = r["text"]
                break
        
        results.append({
            "name": name,
            "file": file,
            "rrf_score": rrf_score,
            "sources": sources[(name, file)],
            "text": text
        })
    
    return results


def search_issues(client, query: str, top_k: int = 3) -> List[Dict]:
    """Issue 语义搜索"""
    global _issue_index
    if _issue_index is None:
        _issue_index = _load_issue_index()
    if _issue_index is None:
        return []
    
    query_emb = get_embedding(client, query)
    
    issues = _issue_index.get("issues", [])
    embeddings = _issue_index.get("embeddings", [])
    
    scores = []
    for i, issue in enumerate(issues):
        if i < len(embeddings):
            sim = _cosine_sim(query_emb, embeddings[i])
            scores.append((sim, issue))
    
    scores.sort(key=lambda x: -x[0])
    
    results = []
    for sim, issue in scores[:top_k]:
        results.append({
            "type": "issue",
            "number": issue.get("number", ""),
            "title": issue.get("title", ""),
            "score": sim,
            "body": (issue.get("body", "") or "")[:300]
        })
    
    return results


def build_context(driver, client, question: str) -> Dict:
    """混合检索：BM25 + Embedding + RRF + Issue"""
    print(f"    [BM25 检索...]", flush=True)
    bm25_results = bm25_search(question, top_k=10)
    
    print(f"    [Embedding 检索...]", flush=True)
    emb_results = embedding_search(client, question, top_k=10)
    
    print(f"    [RRF 融合...]", flush=True)
    fused_results = reciprocal_rank_fusion(bm25_results, emb_results, top_k=8)
    
    print(f"    [Issue 检索...]", flush=True)
    issue_results = search_issues(client, question, top_k=3)
    
    # 扩展调用链
    call_chain = {}
    if fused_results:
        top_func = fused_results[0]["name"]
        if top_func:
            callers = tool_get_callers(driver, top_func, limit=3)
            callees = tool_get_callees(driver, top_func, limit=3)
            call_chain = {"callers": callers, "callees": callees}
    
    return {
        "bm25_count": len(bm25_results),
        "emb_count": len(emb_results),
        "fused_count": len(fused_results),
        "issue_count": len(issue_results),
        "fused_results": fused_results,
        "issue_results": issue_results,
        "call_chain": call_chain
    }


def generate_answer(client, question: str, context: Dict) -> str:
    """生成答案"""
    lines = []
    
    # RRF 融合结果
    if context.get("fused_results"):
        lines.append("【检索到的函数 (RRF 融合)】")
        for r in context["fused_results"][:5]:
            lines.append(f"\n{r['name']} @ {r['file']} [RRF={r['rrf_score']:.3f}, 来源={','.join(r['sources'])}]")
            lines.append(r['text'][:250])
    
    # Issue 结果
    if context.get("issue_results"):
        lines.append("\n【相关 Issue】")
        for r in context["issue_results"]:
            lines.append(f"\nIssue #{r['number']}: {r['title']} [相似度={r['score']:.3f}]")
            lines.append(r['body'][:200])
    
    # 调用链
    if context.get("call_chain"):
        lines.append("\n【调用关系】")
        cc = context["call_chain"]
        if "未找到" not in cc.get("callers", ""):
            lines.append(f"调用者: {cc['callers'][:150]}")
        if "未找到" not in cc.get("callees", ""):
            lines.append(f"被调用: {cc['callees'][:150]}")
    
    context_text = "\n".join(lines)
    
    prompt = f"""你是 llama.cpp 代码专家。基于以下混合检索结果（BM25关键词 + Embedding语义 + RRF融合 + Issue），回答问题。

【检索信息】
{context_text}

【问题】
{question}

请用中文回答，并说明信息来源（如 "根据BM25检索到的函数xxx"、"根据Issue #123" 等）："""
    
    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=800,
            timeout=60
        )
        return resp.choices[0].message.content or "(无答案)"
    except Exception as e:
        return f"生成答案失败: {e}"


def process_single(driver, client, row: dict, idx: int) -> dict:
    """处理单个问题"""
    question = row.get("具体问题", "")
    reference = row.get("答案", "")
    
    print(f"[{idx}] {question[:50]}...", flush=True)
    
    t0 = time.time()
    try:
        context = build_context(driver, client, question)
        answer = generate_answer(client, question, context)
        latency = time.time() - t0
        
        return {
            "index": idx,
            "具体问题": question,
            "参考答案": reference,
            "生成答案": answer,
            "路由类型": "V6_Hybrid",
            "检索结果": {
                "bm25_count": context["bm25_count"],
                "emb_count": context["emb_count"],
                "fused_count": context["fused_count"],
                "issue_count": context["issue_count"],
            },
            "延迟_s": latency,
            "错误": None
        }
    except Exception as e:
        latency = time.time() - t0
        print(f"    ERROR: {e}")
        import traceback
        traceback.print_exc()
        return {
            "index": idx,
            "具体问题": question,
            "参考答案": reference,
            "生成答案": "",
            "路由类型": "V6_Hybrid",
            "检索结果": {},
            "延迟_s": latency,
            "错误": str(e)
        }


def main():
    parser = argparse.ArgumentParser(description="QA V6: 混合检索 (BM25 + Embedding + RRF)")
    parser.add_argument("--csv", type=Path, required=True, help="输入 CSV 文件")
    parser.add_argument("--output", type=Path, required=True, help="输出 JSON 文件")
    parser.add_argument("--workers", type=int, default=5, help="并行数")
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
    
    # 预加载索引
    global _rag_index, _issue_index
    _rag_index = _load_rag_index()
    _issue_index = _load_issue_index()
    print(f"索引加载完成: {len(_rag_index['chunks']) if _rag_index else 0} chunks")
    
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
            
            if len(results) % 10 == 0 or len(results) == len(rows):
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
