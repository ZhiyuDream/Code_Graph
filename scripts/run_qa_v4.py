#!/usr/bin/env python3
"""
QA V4: Embedding-RAG + 调用链扩展（简化但有效）

核心策略：
1. 直接用语义搜索找到最相关的函数（embedding）
2. 扩展这些函数的调用者/被调用者（2 层 CALL 链）
3. 用收集到的所有函数信息生成答案

废弃 A/B/C，统一使用 embedding-based 检索。

用法：
  python run_qa_v4.py --csv results/qav2_test.csv --output results/v4_output.json --workers 4
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
    tool_search_issues,
)
from openai import OpenAI


def semantic_search_functions(client, query: str, top_k: int = 8) -> List[Dict]:
    """基于预计算 embedding 找到最相关的函数"""
    idx = _load_rag_index()
    if idx is None:
        return []
    
    # 获取 query 的 embedding
    try:
        resp = client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=[query[:500]]
        )
        query_emb = resp.data[0].embedding
    except Exception as e:
        print(f"    Embedding API 错误: {e}")
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
            "similarity": sim,
            "text": chunk.get("text", "")[:500]
        })
    
    return results


def expand_call_chain(driver, func_name: str, depth: int = 1) -> Dict:
    """扩展函数的调用链（caller 和 callee）"""
    if not func_name:
        return {"callers": [], "callees": []}
    
    callers = []
    callees = []
    
    try:
        # 获取调用者
        caller_result = tool_get_callers(driver, func_name, limit=5)
        if "未找到" not in caller_result and "没有找到" not in caller_result:
            for line in caller_result.split("\n")[1:]:  # 跳过标题
                if "(" in line:
                    name = line.split("(")[0].strip()
                    if name:
                        callers.append(name)
        
        # 获取被调用者
        callee_result = tool_get_callees(driver, func_name, limit=5)
        if "未找到" not in callee_result and "没有调用" not in callee_result:
            for line in callee_result.split("\n")[1:]:
                if "(" in line:
                    name = line.split("(")[0].strip()
                    if name:
                        callees.append(name)
    except Exception as e:
        pass
    
    return {"callers": callers, "callees": callees}


def build_context(driver, client, question: str) -> Dict:
    """
    构建上下文：
    1. 语义搜索找相关函数
    2. 扩展调用链
    3. 获取函数详情
    """
    print(f"    [1/3] 语义搜索...", flush=True)
    semantic_funcs = semantic_search_functions(client, question, top_k=6)
    
    if not semantic_funcs:
        print(f"    语义搜索无结果，尝试 Issue 搜索...", flush=True)
        # Fallback: 尝试 Issue 搜索
        issue_result = tool_search_issues(driver, question.split()[0], limit=3)
        return {
            "semantic_funcs": [],
            "call_chains": {},
            "issue_result": issue_result if "未找到" not in issue_result else ""
        }
    
    print(f"    [2/3] 找到 {len(semantic_funcs)} 个相关函数，扩展调用链...", flush=True)
    
    # 收集所有相关函数名
    all_funcs = set()
    for fn in semantic_funcs:
        if fn.get("name"):
            all_funcs.add(fn["name"])
    
    # 扩展调用链
    call_chains = {}
    for fn in semantic_funcs:
        name = fn.get("name")
        if not name:
            continue
        
        chain = expand_call_chain(driver, name, depth=1)
        call_chains[name] = chain
        
        # 将调用链中的函数也加入集合
        all_funcs.update(chain["callers"])
        all_funcs.update(chain["callees"])
    
    print(f"    [3/3] 获取 {len(all_funcs)} 个函数的详情...", flush=True)
    
    # 获取所有函数的详情
    func_details = {}
    for fname in list(all_funcs)[:15]:  # 限制数量
        try:
            detail = tool_get_function_detail(driver, fname)
            if "未找到" not in detail:
                func_details[fname] = detail
        except Exception:
            pass
    
    return {
        "semantic_funcs": semantic_funcs,
        "call_chains": call_chains,
        "func_details": func_details,
        "issue_result": ""
    }


def generate_answer(client, question: str, context: Dict) -> str:
    """基于上下文生成答案"""
    # 格式化上下文
    lines = []
    
    # 语义搜索结果
    if context.get("semantic_funcs"):
        lines.append("【语义检索到的函数】")
        for fn in context["semantic_funcs"]:
            lines.append(f"\n函数: {fn['name']} @ {fn['file']} [相似度: {fn['similarity']:.3f}]")
            if fn.get("text"):
                lines.append(f"代码:\n{fn['text'][:400]}")
    
    # 调用链
    if context.get("call_chains"):
        lines.append("\n【调用关系】")
        for name, chain in list(context["call_chains"].items())[:3]:
            if chain["callers"]:
                lines.append(f"{name} 被调用者: {', '.join(chain['callers'][:3])}")
            if chain["callees"]:
                lines.append(f"{name} 调用: {', '.join(chain['callees'][:3])}")
    
    # Issue 结果
    if context.get("issue_result"):
        lines.append(f"\n【相关 Issue】\n{context['issue_result'][:500]}")
    
    context_text = "\n".join(lines)
    
    prompt = f"""你是 llama.cpp 代码专家。基于以下从代码库中检索到的信息，回答问题。
如果信息不足以完整回答，请基于已有信息给出最合理的推断，并说明信息不足之处。

【检索到的信息】
{context_text}

【问题】
{question}

请用中文回答，引用具体函数名作为证据："""
    
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
    
    print(f"  [{idx}] {question[:50]}...", flush=True)
    
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
            "路由类型": "V4_EmbeddingRAG",
            "检索结果": {
                "semantic_count": len(context.get("semantic_funcs", [])),
                "call_chain_count": len(context.get("call_chains", {})),
                "detail_count": len(context.get("func_details", {})),
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
            "路由类型": "V4_EmbeddingRAG",
            "检索结果": {},
            "延迟_s": latency,
            "错误": str(e)
        }


def main():
    parser = argparse.ArgumentParser(description="QA V4: Embedding-RAG + 调用链扩展")
    parser.add_argument("--csv", type=Path, required=True, help="输入 CSV 文件")
    parser.add_argument("--output", type=Path, required=True, help="输出 JSON 文件")
    parser.add_argument("--workers", type=int, default=2, help="并行数")
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
            
            if len(results) % 5 == 0 or len(results) == len(rows):
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
