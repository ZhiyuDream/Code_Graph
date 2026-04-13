#!/usr/bin/env python3
"""
QA V3: 混合检索（Hybrid Search）

检索策略：
1. 关键词搜索（BM25-like）：目录名、文件名、函数名匹配
2. 语义搜索（Embedding）：基于功能描述的向量相似度
3. 图遍历（Graph Traversal）：从目录 → 文件 → 函数 → 调用链
4. 重排序（Reciprocal Rank Fusion）：合并多路召回结果

废弃 A/B/C 硬分类，全部采用统一检索流水线。

用法：
  python run_qa_v3.py --csv results/qav2_test.csv --output results/v3_output.json --workers 4
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Dict, Tuple, Any

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

from config import NEO4J_DATABASE, OPENAI_API_KEY, OPENAI_BASE_URL, LLM_MODEL, EMBEDDING_MODEL
from src.neo4j_writer import get_driver
from tools.agent_qa import (
    tool_find_module_by_keyword,
    tool_search_functions,
    tool_search_functions_by_content,
    tool_get_function_detail,
    tool_get_callers,
    tool_get_callees,
    tool_get_file_functions,
    tool_search_variables,
    _cosine_sim,
    _load_rag_index,
)
from openai import OpenAI

# 混合检索权重
WEIGHT_KEYWORD = 0.3      # 关键词匹配权重
WEIGHT_SEMANTIC = 0.5     # 语义搜索权重
WEIGHT_GRAPH = 0.2        # 图遍历权重

# RRF 参数
RRF_K = 60


def _run(driver, cypher: str, params: dict = None) -> list[dict]:
    """执行 Cypher 查询"""
    with driver.session(database=NEO4J_DATABASE) as s:
        r = s.run(cypher, params or {})
        return [dict(rec) for rec in r]


# ============================================================================
# 1. 关键词检索（多字段匹配）
# ============================================================================

def keyword_search(driver, keywords: List[str], top_k: int = 10) -> List[Dict]:
    """
    多字段关键词搜索：
    - 目录名匹配
    - 文件名匹配
    - 函数名匹配
    - 文件路径匹配
    """
    results = []
    
    for kw in keywords:
        kw_lower = kw.lower()
        
        # 1a. 函数名匹配（高权重）
        funcs = _run(driver, """
            MATCH (fn:Function)
            WHERE toLower(fn.name) CONTAINS $kw
            RETURN fn.name AS name, fn.file_path AS file, 
                   fn.fan_in AS fan_in, fn.fan_out AS fan_out,
                   1.0 AS score
            ORDER BY fn.fan_in DESC
            LIMIT 5
        """, {"kw": kw_lower})
        
        for f in funcs:
            f["source"] = "keyword_function_name"
            f["match_key"] = kw
            results.append(f)
        
        # 1b. 文件路径匹配
        files = _run(driver, """
            MATCH (f:File)-[:CONTAINS]->(fn:Function)
            WHERE toLower(f.path) CONTAINS $kw
            RETURN fn.name AS name, fn.file_path AS file,
                   fn.fan_in AS fan_in, fn.fan_out AS fan_out,
                   0.8 AS score
            ORDER BY fn.fan_in DESC
            LIMIT 5
        """, {"kw": kw_lower})
        
        for f in files:
            f["source"] = "keyword_file_path"
            f["match_key"] = kw
            results.append(f)
        
        # 1c. 目录名匹配 → 获取目录下核心函数
        dirs = _run(driver, """
            MATCH (d:Directory)
            WHERE toLower(d.name) CONTAINS $kw OR toLower(d.path) CONTAINS $kw
            MATCH (d)-[:CONTAINS*1..2]->(f:File)-[:CONTAINS]->(fn:Function)
            RETURN fn.name AS name, fn.file_path AS file,
                   fn.fan_in AS fan_in, fn.fan_out AS fan_out,
                   0.6 AS score
            ORDER BY fn.fan_in DESC
            LIMIT 5
        """, {"kw": kw_lower})
        
        for f in dirs:
            f["source"] = "keyword_directory"
            f["match_key"] = kw
            results.append(f)
    
    # 去重并排序
    seen = set()
    unique = []
    for r in sorted(results, key=lambda x: -x.get("score", 0)):
        key = (r.get("name"), r.get("file"))
        if key not in seen:
            seen.add(key)
            unique.append(r)
    
    return unique[:top_k]


# ============================================================================
# 2. 语义检索（Embedding）
# ============================================================================

def semantic_search(client, query: str, top_k: int = 10) -> List[Dict]:
    """
    基于预计算 embedding 的语义搜索
    """
    idx = _load_rag_index()
    if idx is None:
        return []
    
    try:
        emb_resp = client.embeddings.create(
            model=EMBEDDING_MODEL, 
            input=[query[:500]]
        )
        query_emb = emb_resp.data[0].embedding
    except Exception as e:
        print(f"  Embedding API 错误: {e}")
        return []
    
    # 计算相似度
    func_chunks = [(i, c) for i, c in enumerate(idx["chunks"]) if c["type"] == "function"]
    scores = []
    
    for i, chunk in func_chunks:
        sim = _cosine_sim(query_emb, idx["embeddings"][i])
        scores.append((sim, chunk))
    
    scores.sort(key=lambda x: -x[0])
    
    results = []
    for sim, chunk in scores[:top_k]:
        meta = chunk.get("meta", {})
        results.append({
            "name": meta.get("name", ""),
            "file": meta.get("file", ""),
            "score": sim,
            "source": "semantic_embedding",
            "text_preview": chunk.get("text", "")[:200]
        })
    
    return results


# ============================================================================
# 3. 图遍历检索（从目录到函数）
# ============================================================================

def graph_traversal_search(driver, keywords: List[str], top_k: int = 10) -> List[Dict]:
    """
    图遍历：从关键词匹配的目录出发，找到核心函数及其调用关系
    """
    results = []
    
    for kw in keywords:
        kw_lower = kw.lower()
        
        # 3a. 找到相关目录
        dirs = _run(driver, """
            MATCH (d:Directory)
            WHERE toLower(d.name) CONTAINS $kw OR toLower(d.path) CONTAINS $kw
            RETURN d.path AS path
            LIMIT 3
        """, {"kw": kw_lower})
        
        # 3b. 对每个目录，找到高 fan_in 的入口函数
        for d in dirs:
            entry_funcs = _run(driver, """
                MATCH (d:Directory {path: $path})-[:CONTAINS*1..2]->(f:File)-[:CONTAINS]->(fn:Function)
                WHERE fn.fan_in > 0
                RETURN fn.name AS name, fn.file_path AS file,
                       fn.fan_in AS fan_in, fn.fan_out AS fan_out,
                       0.5 + (fn.fan_in / 100.0) AS score
                ORDER BY fn.fan_in DESC
                LIMIT 5
            """, {"path": d["path"]})
            
            for f in entry_funcs:
                f["source"] = "graph_directory_entry"
                f["match_key"] = d["path"]
                results.append(f)
        
        # 3c. 找到函数的调用者（影响分析）
        funcs = _run(driver, """
            MATCH (fn:Function)
            WHERE toLower(fn.name) CONTAINS $kw
            RETURN fn.name AS name
            LIMIT 3
        """, {"kw": kw_lower})
        
        for fn in funcs:
            callers = _run(driver, """
                MATCH (caller:Function)-[:CALLS]->(f:Function {name: $name})
                RETURN caller.name AS name, caller.file_path AS file,
                       caller.fan_in AS fan_in, caller.fan_out AS fan_out,
                       0.4 AS score
                LIMIT 3
            """, {"name": fn["name"]})
            
            for c in callers:
                c["source"] = "graph_callers"
                c["match_key"] = fn["name"]
                results.append(c)
    
    # 去重
    seen = set()
    unique = []
    for r in sorted(results, key=lambda x: -x.get("score", 0)):
        key = (r.get("name"), r.get("file"))
        if key not in seen:
            seen.add(key)
            unique.append(r)
    
    return unique[:top_k]


# ============================================================================
# 4. 重排序（Reciprocal Rank Fusion）
# ============================================================================

def reciprocal_rank_fusion(
    keyword_results: List[Dict],
    semantic_results: List[Dict],
    graph_results: List[Dict],
    top_k: int = 10
) -> List[Dict]:
    """
    RRF: 合并多路召回结果
    score = Σ 1 / (k + rank)
    """
    scores = {}  # (name, file) -> rrf_score
    sources = {}  # (name, file) -> [source]
    
    # 合并各路结果
    for rank, r in enumerate(keyword_results):
        key = (r.get("name"), r.get("file"))
        scores[key] = scores.get(key, 0) + 1.0 / (RRF_K + rank + 1)
        sources.setdefault(key, []).append(r.get("source", "keyword"))
    
    for rank, r in enumerate(semantic_results):
        key = (r.get("name"), r.get("file"))
        scores[key] = scores.get(key, 0) + 1.0 / (RRF_K + rank + 1)
        sources.setdefault(key, []).append(r.get("source", "semantic"))
    
    for rank, r in enumerate(graph_results):
        key = (r.get("name"), r.get("file"))
        scores[key] = scores.get(key, 0) + 1.0 / (RRF_K + rank + 1)
        sources.setdefault(key, []).append(r.get("source", "graph"))
    
    # 排序
    ranked = sorted(scores.items(), key=lambda x: -x[1])
    
    results = []
    for (name, file), score in ranked[:top_k]:
        results.append({
            "name": name,
            "file": file,
            "rrf_score": score,
            "sources": sources.get((name, file), [])
        })
    
    return results


# ============================================================================
# 主流程
# ============================================================================

def extract_keywords(client, question: str) -> List[str]:
    """提取关键词，并扩展同义词/相关词"""
    prompt = f"""从问题中提取 2-5 个核心关键词（技术术语、模块名、函数名等），用于代码检索。
同时考虑可能的同义词或缩写。只需输出关键词列表，用逗号分隔。

【问题】{question}

关键词："""
    
    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=50,
            timeout=10
        )
        text = resp.choices[0].message.content or ""
        keywords = [k.strip() for k in text.replace("，", ",").split(",")]
        return [k for k in keywords if k and len(k) >= 2][:5]
    except Exception:
        words = [w for w in question.split() if len(w) >= 3]
        return words[:3]


def enrich_functions(driver, functions: List[Dict]) -> List[Dict]:
    """为检索到的函数添加详情和调用链"""
    enriched = []
    
    for fn in functions:
        name = fn.get("name")
        if not name:
            continue
        
        # 获取详情
        detail = tool_get_function_detail(driver, name)
        
        # 获取调用关系
        callers = tool_get_callers(driver, name, limit=3)
        callees = tool_get_callees(driver, name, limit=3)
        
        fn["detail"] = detail
        fn["callers"] = callers
        fn["callees"] = callees
        enriched.append(fn)
    
    return enriched


def generate_answer(client, question: str, context: Dict) -> str:
    """基于混合检索结果生成答案"""
    # 格式化上下文
    lines = []
    
    if context.get("keywords"):
        lines.append(f"【关键词】{', '.join(context['keywords'])}")
    
    if context.get("retrieved_functions"):
        lines.append("\n【检索到的函数】")
        for fn in context["retrieved_functions"][:5]:
            lines.append(f"\n函数: {fn.get('name')} @ {fn.get('file', 'unknown')}")
            lines.append(f"  来源: {', '.join(fn.get('sources', ['unknown']))}")
            lines.append(f"  RRF分数: {fn.get('rrf_score', 0):.3f}")
            
            detail = fn.get("detail", "")
            if detail and "未找到" not in detail:
                detail_lines = detail.split("\n")[:4]
                lines.extend([f"  {l}" for l in detail_lines])
            
            if fn.get("callers") and "未找到" not in fn["callers"]:
                caller_lines = fn["callers"].split("\n")[:3]
                lines.extend([f"  调用者: {l}" for l in caller_lines])
    
    context_text = "\n".join(lines)
    
    prompt = f"""你是 llama.cpp 代码专家。基于以下从代码图中检索到的信息，回答问题。
如果信息不足以回答问题，请明确说明"信息不足"。

【检索信息】
{context_text}

【问题】
{question}

请用中文回答，并引用具体函数名作为证据："""
    
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


def run_hybrid_search(driver, client, question: str) -> Dict:
    """
    混合检索主流程
    """
    print(f"    [Hybrid] 提取关键词...", flush=True)
    keywords = extract_keywords(client, question)
    
    print(f"    [Hybrid] 关键词: {keywords}", flush=True)
    
    # 并行执行三路检索
    print(f"    [Hybrid] 开始三路检索...", flush=True)
    
    keyword_results = keyword_search(driver, keywords, top_k=10)
    print(f"    [Hybrid] 关键词检索: {len(keyword_results)} 条", flush=True)
    
    semantic_results = semantic_search(client, question, top_k=10)
    print(f"    [Hybrid] 语义检索: {len(semantic_results)} 条", flush=True)
    
    graph_results = graph_traversal_search(driver, keywords, top_k=10)
    print(f"    [Hybrid] 图遍历检索: {len(graph_results)} 条", flush=True)
    
    # RRF 重排序
    fused = reciprocal_rank_fusion(keyword_results, semantic_results, graph_results, top_k=8)
    print(f"    [Hybrid] RRF 融合后: {len(fused)} 条", flush=True)
    
    # 富化函数信息
    enriched = enrich_functions(driver, fused)
    
    return {
        "keywords": keywords,
        "keyword_results": keyword_results,
        "semantic_results": semantic_results,
        "graph_results": graph_results,
        "retrieved_functions": enriched
    }


def process_single(driver, client, row: dict, idx: int) -> dict:
    """处理单个问题"""
    question = row.get("具体问题", "")
    reference = row.get("答案", "")
    
    print(f"  [{idx}] {question[:50]}...", flush=True)
    
    t0 = time.time()
    try:
        # 混合检索
        context = run_hybrid_search(driver, client, question)
        
        # 生成答案
        answer = generate_answer(client, question, context)
        
        latency = time.time() - t0
        
        return {
            "index": idx,
            "具体问题": question,
            "参考答案": reference,
            "生成答案": answer,
            "路由类型": "V3_HybridSearch",
            "检索结果": {
                "keywords": context["keywords"],
                "function_count": len(context["retrieved_functions"]),
                "sources": list(set(
                    s for fn in context["retrieved_functions"] for s in fn.get("sources", [])
                ))
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
            "路由类型": "V3_HybridSearch",
            "检索结果": {},
            "延迟_s": latency,
            "错误": str(e)
        }


def main():
    parser = argparse.ArgumentParser(description="QA V3: 混合检索（BM25 + Embedding + Graph）")
    parser.add_argument("--csv", type=Path, required=True, help="输入 CSV 文件")
    parser.add_argument("--output", type=Path, required=True, help="输出 JSON 文件")
    parser.add_argument("--workers", type=int, default=2, help="并行数（建议2，embedding API 有限速）")
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
    
    # OpenAI 客户端
    client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)
    
    # 串行或并行处理（embedding API 限速，建议 workers=2）
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(process_single, driver, client, row, i): i 
            for i, row in enumerate(rows)
        }
        
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            
            if len(results) % 5 == 0 or len(results) == len(rows):
                print(f"  已完成 {len(results)}/{len(rows)} 题...")
                # 保存进度
                with open(args.output, "w", encoding="utf-8") as f:
                    json.dump(results, f, ensure_ascii=False, indent=2)
    
    # 最终保存
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    
    print(f"\n完成！结果保存至: {args.output}")
    driver.close()


if __name__ == "__main__":
    main()
