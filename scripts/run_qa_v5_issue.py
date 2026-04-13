#!/usr/bin/env python3
"""
QA V5: 代码图 + Issue/PR 知识库混合检索

核心思路：
1. 判断问题类型（Bug/Feature/Design → 查 Issue，Architecture → 查代码）
2. 并行检索代码 + Issue
3. 合并结果生成答案

用法：
  python run_qa_v5_issue.py --csv results/qav2_test.csv --output results/v5_output.json --workers 4
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
)
from openai import OpenAI

# Issue 索引缓存
_ISSUE_INDEX = None


def _load_issue_index():
    """加载 Issue RAG 索引"""
    global _ISSUE_INDEX
    if _ISSUE_INDEX is None:
        idx_path = _ROOT / "data" / "issue_rag_index.json"
        if idx_path.exists():
            with open(idx_path, encoding="utf-8") as f:
                _ISSUE_INDEX = json.load(f)
    return _ISSUE_INDEX


def classify_question(client, question: str) -> dict:
    """
    判断问题类型，决定检索策略
    返回: {'needs_code': bool, 'needs_issue': bool, 'keywords': list}
    """
    prompt = f'''分析以下问题，判断需要检索哪些信息源：

问题: {question}

判断：
1. 这个问题涉及代码实现细节吗？（如函数、类、调用关系）→ needs_code
2. 这个问题涉及 Bug、Feature、设计决策、性能问题吗？→ needs_issue
3. 提取 2-3 个核心关键词用于检索

只输出 JSON 格式：
{{"needs_code": true/false, "needs_issue": true/false, "keywords": ["keyword1", "keyword2"]}}'''

    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=100,
            timeout=10
        )
        text = resp.choices[0].message.content.strip()
        # 提取 JSON
        if '```json' in text:
            text = text.split('```json')[1].split('```')[0]
        elif '```' in text:
            text = text.split('```')[1].split('```')[0]
        return json.loads(text)
    except Exception:
        # 默认都查
        return {'needs_code': True, 'needs_issue': True, 'keywords': question.split()[:3]}


def search_code(client, query: str, top_k: int = 5) -> List[Dict]:
    """代码语义搜索（复用 V4 逻辑）"""
    idx = _load_rag_index()
    if idx is None:
        return []
    
    try:
        resp = client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=[query[:500]]
        )
        query_emb = resp.data[0].embedding
    except Exception:
        return []
    
    # 计算相似度
    scores = []
    for i, chunk in enumerate(idx["chunks"]):
        if chunk["type"] == "function":
            sim = _cosine_sim(query_emb, idx["embeddings"][i])
            scores.append((sim, chunk))
    
    scores.sort(key=lambda x: -x[0])
    
    results = []
    for sim, chunk in scores[:top_k]:
        meta = chunk.get("meta", {})
        results.append({
            "type": "code",
            "name": meta.get("name", ""),
            "file": meta.get("file", ""),
            "score": sim,
            "text": chunk.get("text", "")[:400]
        })
    
    return results


def search_issues(client, query: str, top_k: int = 3) -> List[Dict]:
    """Issue 语义搜索"""
    idx = _load_issue_index()
    if idx is None:
        return []
    
    try:
        resp = client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=[query[:500]]
        )
        query_emb = resp.data[0].embedding
    except Exception:
        return []
    
    issues = idx.get("issues", [])
    embeddings = idx.get("embeddings", [])
    
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
    """
    混合检索：代码 + Issue
    """
    # 1. 判断问题类型
    classification = classify_question(client, question)
    needs_code = classification.get('needs_code', True)
    needs_issue = classification.get('needs_issue', True)
    keywords = classification.get('keywords', question.split()[:3])
    
    print(f"    问题类型: code={needs_code}, issue={needs_issue}, keywords={keywords}")
    
    context = {
        "classification": classification,
        "code_results": [],
        "issue_results": []
    }
    
    # 2. 并行检索
    if needs_code:
        print(f"    [检索代码...]")
        context["code_results"] = search_code(client, question, top_k=5)
    
    if needs_issue:
        print(f"    [检索 Issue...]")
        context["issue_results"] = search_issues(client, question, top_k=3)
    
    # 3. 扩展代码调用链（如果查代码）
    if context["code_results"]:
        top_func = context["code_results"][0]["name"]
        if top_func:
            callers = tool_get_callers(driver, top_func, limit=3)
            callees = tool_get_callees(driver, top_func, limit=3)
            context["call_chain"] = {"callers": callers, "callees": callees}
    
    return context


def generate_answer(client, question: str, context: Dict) -> str:
    """基于混合上下文生成答案"""
    lines = []
    
    # 格式化代码结果
    if context.get("code_results"):
        lines.append("【相关代码函数】")
        for r in context["code_results"][:4]:
            lines.append(f"\n{r['name']} @ {r['file']} [相似度: {r['score']:.3f}]")
            lines.append(r['text'][:300])
    
    # 格式化 Issue 结果
    if context.get("issue_results"):
        lines.append("\n【相关 GitHub Issue】")
        for r in context["issue_results"]:
            lines.append(f"\nIssue #{r['number']}: {r['title']} [相似度: {r['score']:.3f}]")
            lines.append(r['body'][:250])
    
    # 调用链
    if context.get("call_chain"):
        lines.append("\n【调用关系】")
        cc = context["call_chain"]
        if "未找到" not in cc.get("callers", ""):
            lines.append(f"调用者: {cc['callers'][:200]}")
        if "未找到" not in cc.get("callees", ""):
            lines.append(f"被调用: {cc['callees'][:200]}")
    
    context_text = "\n".join(lines)
    
    prompt = f"""你是 llama.cpp 代码专家。基于以下从代码库和 GitHub Issue 中检索到的信息，回答问题。
如果信息不足，请明确说明。优先引用 Issue 中的设计决策和 Bug 修复说明。

【检索信息】
{context_text}

【问题】
{question}

请用中文回答，并标注信息来源（如 "根据 Issue #12345" 或 "根据代码函数 xxx"）："""
    
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
            "路由类型": "V5_Code+Issue",
            "检索结果": {
                "code_count": len(context.get("code_results", [])),
                "issue_count": len(context.get("issue_results", [])),
            },
            "延迟_s": latency,
            "错误": None
        }
    except Exception as e:
        latency = time.time() - t0
        print(f"    ERROR: {e}")
        return {
            "index": idx,
            "具体问题": question,
            "参考答案": reference,
            "生成答案": "",
            "路由类型": "V5_Code+Issue",
            "检索结果": {},
            "延迟_s": latency,
            "错误": str(e)
        }


def main():
    parser = argparse.ArgumentParser(description="QA V5: 代码 + Issue 混合检索")
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
    
    # 预加载索引
    _load_rag_index()
    _load_issue_index()
    print("索引加载完成")
    
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
