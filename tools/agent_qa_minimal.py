#!/usr/bin/env python3
"""
简化版 Graph-Agent（用于跑评测对比）：
- 直接根据问题类型选择工具调用
- 支持 search_issues / search_functions / get_file_functions / semantic_search
- 最多 4 步工具调用
"""
from __future__ import annotations

import sys
import json
import time
import argparse
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

from config import NEO4J_DATABASE, OPENAI_API_KEY, OPENAI_BASE_URL, LLM_MODEL
from neo4j_writer import get_driver
from openai import OpenAI


# ── 工具 ─────────────────────────────────────────────────────────────
def _run(driver, cypher, params=None):
    with driver.session(database=NEO4J_DATABASE) as s:
        r = s.run(cypher, params or {})
        return [dict(rec) for rec in r]


def search_functions(driver, kw, limit=8):
    rows = _run(driver, """
        MATCH (f:Function)
        WHERE f.name CONTAINS $kw
          AND f.annotation_json IS NOT NULL
        RETURN f.name AS name, f.file_path AS file, f.fan_in AS fan_in, f.fan_out AS fan_out
        ORDER BY f.fan_in DESC LIMIT $lim
    """, {"kw": kw, "lim": limit})
    if not rows:
        rows = _run(driver, """
            MATCH (f:Function)
            WHERE f.name CONTAINS $kw
            RETURN f.name AS name, f.file_path AS file, f.fan_in AS fan_in, f.fan_out AS fan_out
            ORDER BY f.fan_in DESC LIMIT $lim
        """, {"kw": kw, "lim": limit})
    lines = [f"{r['name']} ({r['file']})" for r in rows]
    return "\n".join(lines) if lines else f"未找到包含 '{kw}' 的函数"


def search_issues(driver, kw, limit=5):
    rows = _run(driver, """
        MATCH (i:Issue)
        WHERE i.title CONTAINS $kw OR i.body CONTAINS $kw
        RETURN i.number AS num, i.title AS title, i.labels AS labels
        ORDER BY i.ranking_score DESC LIMIT $lim
    """, {"kw": kw, "lim": limit})
    lines = [f"Issue #{r['num']}: {r['title']}" for r in rows]
    return "\n".join(lines) if lines else f"未找到包含 '{kw}' 的 Issue"


def get_file_functions(driver, fp_kw, limit=15):
    rows = _run(driver, """
        MATCH (f:Function)
        WHERE f.file_path CONTAINS $fp
        RETURN f.name AS name, f.fan_in AS fan_in, f.fan_out AS fan_out
        ORDER BY f.fan_in DESC LIMIT $lim
    """, {"fp": fp_kw, "lim": limit})
    lines = [f"{r['name']}" for r in rows]
    return "\n".join(lines) if lines else f"未找到路径包含 '{fp_kw}' 的文件"


def semantic_search(driver, query, limit=6):
    kws = [w for w in re.split(r'[，。、？\s]', query) if len(w) >= 2 and w not in {
        '的', '了', '是', '在', '有', '和', '为', '什么', '如何', '怎么', '哪些', '哪个', '主要', '包含', '哪些', '这个', '整个', '代码'
    }]
    if not kws:
        return f"查询 '{query}' 无有效关键词"
    rows = _run(driver, """
        MATCH (f:Function)
        WHERE f.name CONTAINS $kw
          AND f.annotation_json IS NOT NULL
        RETURN f.name AS name, f.file_path AS file, f.fan_in AS fan_in, f.annotation_json AS ann
        ORDER BY f.fan_in DESC LIMIT $lim
    """, {"kw": kws[0], "lim": limit})
    if not rows:
        return f"语义搜索 '{query}' 未找到相关函数"
    lines = []
    for r in rows:
        ann = r['ann'] or "{}"
        if isinstance(ann, str):
            try:
                ann = json.loads(ann)
            except:
                ann = {}
        summary = ann.get('summary', '') if isinstance(ann, dict) else ''
        lines.append(f"{r['name']} ({r['file']}): {summary[:80]}")
    return "\n".join(lines)


def get_callers(driver, func_name, limit=10):
    rows = _run(driver, """
        MATCH (caller:Function)-[:CALLS]->(callee:Function {name: $name})
        RETURN caller.name AS name, caller.file_path AS file
        LIMIT $lim
    """, {"name": func_name, "lim": limit})
    lines = [f"{r['name']} ({r['file']})" for r in rows]
    return "\n".join(lines) if lines else f"没有找到调用 '{func_name}' 的函数"


import re


# ── Agent 核心 ──────────────────────────────────────────────────────
MAX_STEPS = 4


def run_agent(driver, question: str) -> tuple[str, list, int, dict]:
    """
    返回 (answer, trace, steps, token_usage)
    """
    client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)

    # 从问题中提取关键词
    entity_match = re.search(r'[#\w]+', question)
    entity = entity_match.group(0) if entity_match else ""

    # 判断问题类型
    q_lower = question.lower()
    is_issue = any(kw in q_lower for kw in ['issue', '问题', 'bug', 'pr #', '这个', '遇到'])
    is_module = any(kw in q_lower for kw in ['模块', '包含哪些', '子模块', '协作', '组织', '架构', '主要功能'])
    is_func = any(kw in q_lower for kw in ['函数', '功能', '实现', '怎么', '如何', '作用', '做什么'])

    trace = []
    collected_info = []
    used_tools = set()

    # Step 1: 关键词搜索函数
    kw = entity if len(entity) > 2 else "llama"
    result = search_functions(driver, kw, limit=8)
    trace.append({"tool": "search_functions", "args": {"keyword": kw}, "result": result[:200]})
    collected_info.append(f"【函数搜索({kw})】\n{result[:300]}")
    used_tools.add("search_functions")

    # Step 2: 根据类型继续
    if is_issue:
        result = search_issues(driver, kw, limit=5)
        trace.append({"tool": "search_issues", "args": {"keyword": kw}, "result": result[:200]})
        collected_info.append(f"【Issue搜索({kw})】\n{result[:300]}")
        used_tools.add("search_issues")
    elif is_module:
        result = get_file_functions(driver, kw, limit=15)
        trace.append({"tool": "get_file_functions", "args": {"file_keyword": kw}, "result": result[:200]})
        collected_info.append(f"【文件函数({kw})】\n{result[:300]}")
        used_tools.add("get_file_functions")
    else:
        result = semantic_search(driver, question, limit=6)
        trace.append({"tool": "semantic_search", "args": {"query": question}, "result": result[:200]})
        collected_info.append(f"【语义搜索】\n{result[:300]}")
        used_tools.add("semantic_search")

    # Step 3: 尝试获取调用者
    if len(entity) > 3:
        result = get_callers(driver, entity, limit=5)
        trace.append({"tool": "get_callers", "args": {"func_name": entity}, "result": result[:200]})
        collected_info.append(f"【调用者({entity})】\n{result[:200]}")
        used_tools.add("get_callers")

    # Step 4: 语义搜索补充
    if len(used_tools) < 3:
        result = semantic_search(driver, question, limit=6)
        trace.append({"tool": "semantic_search", "args": {"query": question}, "result": result[:200]})
        collected_info.append(f"【语义补充】\n{result[:300]}")
        used_tools.add("semantic_search")

    # 生成答案
    context = "\n\n".join(collected_info)
    if len(context) > 3000:
        context = context[:3000] + "\n...(已截断)"

    prompt = f"""你是 llama.cpp 代码库的专家助手。请根据下面收集到的信息，用中文回答问题。

【收集的信息】
{context}

【问题】
{question}

请根据收集的信息生成回答。如果信息不足，请如实说明。
回答格式：【你的回答】"""

    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=700,
            timeout=60,
        )
        answer = (resp.choices[0].message.content or "").strip()
        token_usage = resp.usage.model_dump() if resp.usage else {}
    except Exception as e:
        answer = f"生成答案时出错: {e}"
        token_usage = {}

    return answer, trace, len(trace), token_usage


# ── CLI ──────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    import pandas as pd
    df = pd.read_csv(args.csv, encoding="utf-8")
    if args.limit > 0:
        df = df.head(args.limit)

    driver = get_driver()
    driver.verify_connectivity()

    results = []
    for i, row in df.iterrows():
        idx = row.get("index", i)
        question = row.get("具体问题", "")
        print(f"[{i+1}/{len(df)}] {row.get('类别', '')}/{row.get('子类', '')}: {question[:50]}...", flush=True)
        t0 = time.time()
        try:
            answer, trace, steps, token_usage = run_agent(driver, question)
        except Exception as e:
            answer = ""
            trace = []
            steps = 0
            token_usage = {}
        latency = time.time() - t0

        results.append({
            "index": int(idx),
            "类别": row.get("类别", ""),
            "子类": row.get("子类", ""),
            "具体问题": question,
            "参考答案": row.get("答案", ""),
            "生成答案": answer,
            "Evidence": row.get("Evidence", ""),
            "工具调用步数": steps,
            "工具轨迹": trace,
            "延迟_s": latency,
            "token_usage": token_usage,
            "错误": None,
        })

    driver.close()

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"完成: {args.output}")


if __name__ == "__main__":
    main()
