#!/usr/bin/env python3
"""
QA V2: 目录驱动的 ReAct Agent（废弃 A/B/C 硬分类）

融合了 agent_qa.py 的完整 tools，采用人类-like 的探索策略：
1. 目录概览 → 2. 关键词定位 → 3. 模块深入 → 4. 函数/调用链分析

用法：
  python run_qa_v2.py --csv results/qav2_test.csv --output results/v2_output.json --workers 4
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

from config import NEO4J_DATABASE, OPENAI_API_KEY, OPENAI_BASE_URL, LLM_MODEL
from src.neo4j_writer import get_driver
from tools.agent_qa import (
    tool_find_module_by_keyword,
    tool_get_directory_tree,
    tool_get_module_overview,
    tool_search_functions,
    tool_search_functions_by_content,
    tool_get_function_detail,
    tool_get_callers,
    tool_get_callees,
    tool_get_file_functions,
    tool_search_variables,
    tool_search_attributes,
    tool_read_file_lines,
    TOOL_MAP,
    MAX_STEPS,
    SYSTEM_PROMPT,
)
from openai import OpenAI

TOOL_RESULT_MAX = 1500


# ============================================================================
# 目录驱动的 Agent 核心
# ============================================================================

def extract_keywords(client, question: str) -> list[str]:
    """从问题提取关键词（技术术语、模块名、函数名等）"""
    prompt = f"""从问题中提取 2-4 个核心关键词（技术术语、模块名、函数名等），用于代码检索。
只需输出关键词列表，用逗号分隔。

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
        return [k for k in keywords if k and len(k) >= 2][:4]
    except Exception:
        # Fallback: 简单分词
        words = [w for w in question.split() if len(w) >= 3 and w not in 
                ["如何", "什么", "怎么", "为什么", "的", "了", "是", "在"]]
        return words[:3]


def run_directory_driven_agent(driver, client, question: str) -> dict:
    """
    目录驱动的 ReAct Agent：
    1. 提取关键词
    2. 获取顶层目录概览
    3. 用关键词定位候选目录/模块
    4. 深入探索每个模块（函数、调用链）
    5. 生成答案
    """
    collected_info = {
        "keywords": [],
        "explored_modules": [],
        "functions": [],
        "call_chains": [],
        "issues": []
    }
    
    # Step 1: 提取关键词
    keywords = extract_keywords(client, question)
    collected_info["keywords"] = keywords
    
    # Step 2: 尝试用每个关键词定位模块
    candidate_modules = []
    for kw in keywords:
        result = tool_find_module_by_keyword(driver, kw)
        if "未找到" not in result:
            candidate_modules.append({"keyword": kw, "info": result})
            if len(candidate_modules) >= 3:
                break
    
    collected_info["explored_modules"] = candidate_modules
    
    # Step 3: 如果有关键词匹配的函数，获取详情
    found_functions = set()
    for kw in keywords:
        funcs_result = tool_search_functions(driver, kw, limit=5)
        if "未找到" not in funcs_result:
            lines = funcs_result.split("\n")
            for line in lines[:3]:
                if "(" in line:
                    func_name = line.split("(")[0].strip()
                    if func_name and func_name not in found_functions:
                        found_functions.add(func_name)
                        detail = tool_get_function_detail(driver, func_name)
                        callers = tool_get_callers(driver, func_name, limit=3)
                        callees = tool_get_callees(driver, func_name, limit=3)
                        collected_info["functions"].append({
                            "name": func_name,
                            "detail": detail,
                            "callers": callers,
                            "callees": callees
                        })
    
    # Step 4: 如果信息不足，尝试语义搜索
    if len(collected_info["functions"]) < 2:
        try:
            from tools.agent_qa import tool_search_by_embedding
            semantic_result = tool_search_by_embedding(driver, question, limit=3)
            if "失败" not in semantic_result:
                collected_info["semantic_search"] = semantic_result
        except Exception:
            pass
    
    return collected_info


def generate_answer(client, question: str, info: dict, reference: str = "") -> str:
    """基于收集的信息生成答案"""
    # 格式化收集的信息
    context_parts = []
    
    if info.get("keywords"):
        context_parts.append(f"【关键词】{', '.join(info['keywords'])}")
    
    if info.get("explored_modules"):
        context_parts.append("【模块探索】")
        for m in info["explored_modules"]:
            context_parts.append(f"关键词 '{m['keyword']}':")
            for line in m["info"].split("\n")[:15]:
                context_parts.append(f"  {line}")
    
    if info.get("functions"):
        context_parts.append("【函数详情】")
        for fn in info["functions"][:3]:
            context_parts.append(f"\n函数: {fn['name']}")
            detail_lines = fn["detail"].split("\n")[:5]
            context_parts.extend([f"  {l}" for l in detail_lines])
            if "调用" in fn.get("callers", ""):
                context_parts.append(f"  调用者: {fn['callers'].split(chr(10))[1] if chr(10) in fn['callers'] else '...'}")
    
    if info.get("semantic_search"):
        context_parts.append("【语义搜索结果】")
        for line in info["semantic_search"].split("\n")[:10]:
            context_parts.append(f"  {line}")
    
    context = "\n".join(context_parts)
    
    prompt = f"""你是 llama.cpp 代码专家。基于以下从代码图中检索到的信息，回答问题。
如果信息不足以回答问题，请明确说明"信息不足"。

【检索到的信息】
{context}

【问题】
{question}

请用中文回答："""
    
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


# ============================================================================
# LLM ReAct Agent（带工具调用）
# ============================================================================

def run_llm_agent(driver, client, question: str) -> dict:
    """
    使用 LLM 进行 ReAct 决策，调用 tools
    """
    from tools.agent_qa import TOOLS
    
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    
    collected_steps = []
    
    for step in range(MAX_STEPS):
        try:
            resp = client.chat.completions.create(
                model=LLM_MODEL,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                timeout=60
            )
        except Exception as e:
            collected_steps.append({"error": str(e)})
            break
        
        choice = resp.choices[0]
        
        # 如果有最终答案
        if not choice.message.tool_calls:
            return {
                "answer": choice.message.content or "(无答案)",
                "steps": collected_steps,
                "step_count": step + 1
            }
        
        # 执行工具调用
        messages.append({
            "role": "assistant",
            "content": choice.message.content or "",
            "tool_calls": [tc.model_dump() for tc in choice.message.tool_calls]
        })
        
        for tc in choice.message.tool_calls:
            func_name = tc.function.name
            try:
                args = json.loads(tc.function.arguments)
            except Exception:
                args = {}
            
            # 执行工具
            if func_name in TOOL_MAP:
                try:
                    result = TOOL_MAP[func_name](driver, **args)
                    # 截断过长的结果
                    if len(result) > TOOL_RESULT_MAX:
                        result = result[:TOOL_RESULT_MAX] + "\n...(truncated)"
                except Exception as e:
                    result = f"工具执行错误: {e}"
            else:
                result = f"未知工具: {func_name}"
            
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result
            })
            
            collected_steps.append({
                "tool": func_name,
                "args": args,
                "result_preview": result[:200] if len(result) > 200 else result
            })
    
    # 达到最大步数，强制生成答案
    messages.append({
        "role": "user",
        "content": "基于以上工具调用结果，请生成最终答案。"
    })
    
    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=messages,
            max_tokens=800,
            timeout=60
        )
        return {
            "answer": resp.choices[0].message.content or "(无答案)",
            "steps": collected_steps,
            "step_count": len(collected_steps)
        }
    except Exception as e:
        return {
            "answer": f"生成答案失败: {e}",
            "steps": collected_steps,
            "step_count": len(collected_steps)
        }


# ============================================================================
# 主流程
# ============================================================================

def process_single(driver, client, row: dict, idx: int, use_llm_agent: bool = False) -> dict:
    """处理单个问题"""
    question = row.get("具体问题", "")
    reference = row.get("答案", "")
    
    print(f"  [{idx}] {question[:50]}...", flush=True)
    
    t0 = time.time()
    try:
        if use_llm_agent:
            # 使用 LLM ReAct Agent（带工具调用）
            result = run_llm_agent(driver, client, question)
            answer = result["answer"]
            steps = result["steps"]
            step_count = result["step_count"]
            info = {"agent_mode": "llm_react", "steps": steps}
        else:
            # 使用目录驱动 Agent（程序化决策）
            info = run_directory_driven_agent(driver, client, question)
            answer = generate_answer(client, question, info, reference)
            steps = []
            step_count = len(info.get("explored_modules", [])) + len(info.get("functions", []))
        
        latency = time.time() - t0
        
        return {
            "index": idx,
            "具体问题": question,
            "参考答案": reference,
            "生成答案": answer,
            "路由类型": "V2_DirectoryDriven",
            "检索结果": info,
            "工具调用步数": step_count,
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
            "路由类型": "V2_DirectoryDriven",
            "检索结果": {},
            "工具调用步数": 0,
            "延迟_s": latency,
            "错误": str(e)
        }


def main():
    parser = argparse.ArgumentParser(description="QA V2: 目录驱动的 ReAct Agent")
    parser.add_argument("--csv", type=Path, required=True, help="输入 CSV 文件")
    parser.add_argument("--output", type=Path, required=True, help="输出 JSON 文件")
    parser.add_argument("--workers", type=int, default=4, help="并行数")
    parser.add_argument("--llm-agent", action="store_true", help="使用 LLM ReAct Agent（带工具调用）")
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
            executor.submit(process_single, driver, client, row, i, args.llm_agent): i 
            for i, row in enumerate(rows)
        }
        
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            
            if len(results) % 10 == 0:
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
