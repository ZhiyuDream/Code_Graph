#!/usr/bin/env python3
"""
QA V7: ReAct Agent - 迭代深入检索

核心思路：
1. 初始检索获取候选函数
2. LLM 判断信息是否足够
3. 不够则扩展调用链或深入相关文件
4. 最多 3 轮迭代，逐步深入

废弃 A/B/C 硬分类，采用统一的 ReAct 决策模式。
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Dict, Any

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
    tool_get_file_functions,
    _load_issue_index,
)
from openai import OpenAI

MAX_STEPS = 3
_rag_index = None
_issue_index = None


def search_code_embedding(client, query: str, top_k: int = 5) -> List[Dict]:
    """Embedding 语义搜索代码"""
    global _rag_index
    if _rag_index is None:
        _rag_index = _load_rag_index()
    if _rag_index is None:
        return []
    
    try:
        resp = client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=[query[:500]]
        )
        query_emb = resp.data[0].embedding
    except Exception:
        return []
    
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
            "score": sim,
            "text": chunk.get("text", "")[:400],
            "source": "embedding"
        })
    
    return results


def search_issues(client, query: str, top_k: int = 3) -> List[Dict]:
    """Issue 语义搜索"""
    global _issue_index
    if _issue_index is None:
        _issue_index = _load_issue_index()
    if _issue_index is None:
        return []
    
    try:
        resp = client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=[query[:500]]
        )
        query_emb = resp.data[0].embedding
    except Exception:
        return []
    
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
            "number": issue.get("number", ""),
            "title": issue.get("title", ""),
            "score": sim,
            "body": (issue.get("body", "") or "")[:300]
        })
    
    return results


def react_decide(client, question: str, collected: Dict, step: int) -> Dict:
    """
    ReAct 决策：判断信息是否足够，决定下一步行动
    
    返回: {
        "thought": "思考过程",
        "sufficient": True/False,
        "action": "expand_callers|expand_callees|explore_file|sufficient",
        "target": "目标函数名或文件名"
    }
    """
    # 格式化当前收集的信息
    context = f"""问题: {question}

当前已收集的信息（第 {step} 轮）：

【已检索到的函数】({len(collected.get('functions', []))} 个)
"""
    for i, fn in enumerate(collected.get('functions', [])[:5]):
        context += f"{i+1}. {fn['name']} @ {fn['file']} (相似度: {fn.get('score', 0):.3f})\n"
    
    if collected.get('issues'):
        context += f"\n【已检索到的 Issue】({len(collected['issues'])} 个)\n"
        for i, issue in enumerate(collected['issues'][:3]):
            context += f"{i+1}. Issue #{issue['number']}: {issue['title']}\n"
    
    if collected.get('call_chains'):
        context += f"\n【已扩展的调用链】({len(collected['call_chains'])} 条)\n"
    
    prompt = f"""{context}

---

作为代码专家助手，请判断当前信息是否足够回答问题。

如果信息足够，回答：
{{
    "thought": "现有信息已覆盖问题的关键方面，包括...",
    "sufficient": true,
    "action": "sufficient",
    "target": ""
}}

如果信息不足，选择下一步行动：
- "expand_callers": 扩展某个函数的调用者（谁调用了它）
- "expand_callees": 扩展某个函数的被调用者（它调用了谁）
- "explore_file": 深入探索某个文件中的其他函数

回答格式：
{{
    "thought": "当前信息缺少...，需要了解...",
    "sufficient": false,
    "action": "expand_callers|expand_callees|explore_file",
    "target": "具体的函数名或文件名"
}}

只输出 JSON，不要其他文字。"""
    
    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
            timeout=30
        )
        text = resp.choices[0].message.content.strip()
        
        # 提取 JSON
        if '```json' in text:
            text = text.split('```json')[1].split('```')[0]
        elif '```' in text:
            text = text.split('```')[1].split('```')[0]
        
        return json.loads(text)
    except Exception as e:
        # 默认继续扩展第一个函数
        funcs = collected.get('functions', [])
        if funcs:
            return {
                "thought": f"解析错误，默认扩展: {e}",
                "sufficient": False,
                "action": "expand_callees",
                "target": funcs[0]['name']
            }
        return {"sufficient": True, "action": "sufficient", "target": ""}


def expand_call_chain(driver, func_name: str, direction: str) -> Dict:
    """扩展调用链"""
    if direction == "callers":
        result = tool_get_callers(driver, func_name, limit=5)
    else:
        result = tool_get_callees(driver, func_name, limit=5)
    
    # 解析结果
    functions = []
    for line in result.split('\n')[1:]:  # 跳过标题
        if '(' in line:
            name = line.split('(')[0].strip()
            file = line.split('(')[1].split(')')[0] if '(' in line else ""
            if name and name != func_name:
                functions.append({"name": name, "file": file})
    
    return {"functions": functions, "raw": result}


def explore_file(driver, file_path: str) -> List[Dict]:
    """探索文件中的其他函数"""
    result = tool_get_file_functions(driver, file_path, limit=10)
    
    functions = []
    for line in result.split('\n')[1:]:
        if 'fan_in=' in line:
            # 解析 "func_name fan_in=x fan_out=y"
            parts = line.split()
            if parts:
                name = parts[0]
                functions.append({"name": name, "file": file_path})
    
    return functions


def react_search(driver, client, question: str) -> Dict:
    """
    ReAct 迭代检索主流程
    """
    collected = {
        "functions": [],
        "issues": [],
        "call_chains": [],
        "explored_files": set(),
        "steps": []
    }
    
    # Step 1: 初始检索
    print(f"    [Step 1] 初始检索...", flush=True)
    code_results = search_code_embedding(client, question, top_k=5)
    issue_results = search_issues(client, question, top_k=3)
    
    collected["functions"] = code_results
    collected["issues"] = issue_results
    collected["steps"].append({"step": 1, "action": "initial_search", "found": len(code_results)})
    
    # Step 2-3: 迭代扩展
    for step in range(2, MAX_STEPS + 1):
        print(f"    [Step {step}] ReAct 决策...", flush=True)
        
        decision = react_decide(client, question, collected, step)
        print(f"      思考: {decision.get('thought', '')[:80]}...")
        print(f"      行动: {decision.get('action')} -> {decision.get('target', '')}")
        
        if decision.get("sufficient"):
            print(f"      ✓ 信息充足，停止迭代")
            break
        
        action = decision.get("action")
        target = decision.get("target", "")
        
        if action in ["expand_callers", "expand_callees"] and target:
            direction = "callers" if action == "expand_callers" else "callees"
            print(f"      扩展 {target} 的{direction}...")
            chain = expand_call_chain(driver, target, direction)
            
            # 添加新发现的函数
            for fn in chain["functions"]:
                if not any(f['name'] == fn['name'] for f in collected["functions"]):
                    fn['score'] = 0.5  # 扩展发现的函数给一个基础分
                    fn['source'] = f'{direction}_of_{target}'
                    collected["functions"].append(fn)
            
            collected["call_chains"].append({
                "from": target,
                "direction": direction,
                "found": len(chain["functions"])
            })
            
        elif action == "explore_file" and target:
            print(f"      深入探索文件 {target}...")
            if target not in collected["explored_files"]:
                funcs = explore_file(driver, target)
                for fn in funcs:
                    if not any(f['name'] == fn['name'] for f in collected["functions"]):
                        fn['score'] = 0.4
                        fn['source'] = f'file_{target}'
                        collected["functions"].append(fn)
                collected["explored_files"].add(target)
        
        collected["steps"].append({
            "step": step,
            "action": action,
            "target": target
        })
    
    return collected


def generate_answer(client, question: str, collected: Dict) -> str:
    """基于收集的信息生成答案"""
    lines = []
    
    # 函数信息
    if collected.get("functions"):
        lines.append("【检索到的函数】")
        for i, fn in enumerate(collected["functions"][:8]):
            source = fn.get('source', 'embedding')
            score = fn.get('score', 0)
            lines.append(f"{i+1}. {fn['name']} @ {fn['file']} [来源:{source}, 相似度:{score:.3f}]")
    
    # Issue 信息
    if collected.get("issues"):
        lines.append("\n【相关 Issue】")
        for i, issue in enumerate(collected["issues"][:3]):
            lines.append(f"{i+1}. Issue #{issue['number']}: {issue['title']}")
            lines.append(f"   {issue['body'][:200]}")
    
    # 迭代步骤
    if collected.get("steps"):
        lines.append(f"\n【迭代检索过程】({len(collected['steps'])} 轮)")
        for step in collected["steps"]:
            lines.append(f"  Step {step['step']}: {step['action']} {step.get('target', '')}")
    
    context_text = "\n".join(lines)
    
    prompt = f"""你是 llama.cpp 代码专家。基于以下通过 ReAct 迭代检索收集到的信息，回答问题。

【检索信息】
{context_text}

【问题】
{question}

请用中文回答，说明推理过程："""
    
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
        collected = react_search(driver, client, question)
        answer = generate_answer(client, question, collected)
        latency = time.time() - t0
        
        return {
            "index": idx,
            "具体问题": question,
            "参考答案": reference,
            "生成答案": answer,
            "路由类型": "V7_ReAct",
            "检索结果": {
                "function_count": len(collected.get("functions", [])),
                "issue_count": len(collected.get("issues", [])),
                "step_count": len(collected.get("steps", [])),
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
            "路由类型": "V7_ReAct",
            "检索结果": {},
            "延迟_s": latency,
            "错误": str(e)
        }


def main():
    parser = argparse.ArgumentParser(description="QA V7: ReAct Agent 迭代检索")
    parser.add_argument("--csv", type=Path, required=True, help="输入 CSV 文件")
    parser.add_argument("--output", type=Path, required=True, help="输出 JSON 文件")
    parser.add_argument("--workers", type=int, default=3, help="并行数（ReAct 每题多轮，建议用少一点）")
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
    print("索引加载完成")
    
    # OpenAI 客户端
    client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)
    
    # 并行处理（ReAct 每题多轮，workers 不宜过多）
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
