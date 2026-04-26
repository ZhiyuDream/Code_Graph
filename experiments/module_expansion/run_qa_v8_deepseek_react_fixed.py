#!/usr/bin/env python3
"""
V8 + DeepSeek-v4-pro + ReAct代码Agent（修复版）

核心改进：
1. Prompt 文件化管理（prompts/ 目录）
2. File Expansion 后移为 ReAct action（P2）
3. ReAct action 扩展：caller/callee/same_file/same_class（P3）
4. 完整检索轨迹记录

数据集: results/qav2_test_cleaned.csv
模型: deepseek-v4-pro (max_tokens=8192)
评估: gpt-4.1-mini
"""
from __future__ import annotations

import os
import sys
import json
import csv
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

os.environ['LLM_MODEL'] = 'deepseek-v4-pro'

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))

from tools.core import get_neo4j_driver, close_neo4j_driver, generate_answer
from tools.core.llm_client import call_llm_json
from tools.core.prompt_loader import load_prompt, format_actions_for_prompt, get_action_names, get_action_impl
from tools.search import (
    search_functions_by_text, expand_call_chain,
    expand_same_file, expand_same_class,
    search_issues, extract_entities_from_question,
    convert_grep_to_function_results, search_module_functions,
)
from tools.search.grep_search_v2 import grep_codebase
from tools.search.semantic_search import _load_rag_index

MAX_STEPS = 5
FALLBACK_THRESHOLD = 0.5

_rag_index_cache = None

def get_rag_index():
    global _rag_index_cache
    if _rag_index_cache is None:
        _rag_index_cache = _load_rag_index()
    return _rag_index_cache


def initial_search(driver, question: str) -> tuple[dict, dict]:
    """
    初始检索（不做 file expansion，保持上下文干净）
    返回: (collected_dict, trace_dict)
    """
    trace = {"phase": "initial_search", "question": question}
    
    # 1. Embedding 检索
    funcs = search_functions_by_text(question, top_k=10)
    trace["embedding_search"] = [
        {"name": f.get('name'), "file": f.get('file'), "score": f.get('score')} 
        for f in funcs
    ]
    
    # 2. Fallback（低相似度时激活）
    max_score = max([f.get('score', 0) for f in funcs], default=0)
    fallback_triggered = False
    fallback_results = []
    
    if max_score < FALLBACK_THRESHOLD:
        fallback_triggered = True
        entities = extract_entities_from_question(question)
        trace["extracted_entities"] = entities
        
        for entity in entities[:2]:
            entity_results = {"entity": entity}
            
            if '-' in entity or entity.islower():
                module_funcs = search_module_functions(entity, limit=5)
                if module_funcs:
                    entity_results["module_search"] = len(module_funcs)
                    for fn in module_funcs:
                        if not any(f['name'] == fn['name'] for f in funcs):
                            funcs.append(fn)
            
            grep_results = grep_codebase(entity, limit=3)
            if grep_results:
                entity_results["grep_search"] = len(grep_results)
                new_funcs = convert_grep_to_function_results(grep_results)
                for fn in new_funcs:
                    if not any(f['name'] == fn['name'] for f in funcs):
                        funcs.append(fn)
            
            fallback_results.append(entity_results)
    
    trace["fallback_triggered"] = fallback_triggered
    trace["fallback_results"] = fallback_results
    
    # 3. Issue 检索
    issues = search_issues(question, top_k=3)
    trace["issues"] = [
        {"number": i.get('number'), "title": i.get('title')[:80]} for i in issues
    ]
    
    collected = {
        "functions": funcs,
        "issues": issues,
        "steps": [{"step": 1, "action": "initial_search", "found": len(funcs)}],
        "call_chains": [],
        "fallback_triggered": fallback_triggered,
    }
    
    return collected, trace


def react_decide(question: str, collected: dict, step: int) -> tuple[dict, dict]:
    """
    ReAct 决策（使用 prompt 文件）
    返回: (decision_dict, decision_trace)
    """
    funcs = collected.get("functions", [])
    chains = collected.get("call_chains", [])
    
    # 构建函数列表文本
    expanded = [c['from'] for c in chains]
    func_lines = []
    for i, f in enumerate(funcs[:8]):
        source = f.get('source', 'embedding') if f.get('source') else 'embedding'
        score = f.get('score', 0)
        marker = " [已扩展]" if f['name'] in expanded else ""
        func_lines.append(f"{i+1}. {f['name']} ({source}, {score:.3f}){marker}")
    
    # 如果有更多函数，简略展示
    if len(funcs) > 8:
        func_lines.append(f"   ... 还有 {len(funcs)-8} 个函数")
    
    # Issue 列表
    issue_lines = []
    if collected.get("issues"):
        for i, issue in enumerate(collected["issues"][:2]):
            issue_lines.append(f"{i+1}. #{issue['number']}: {issue['title'][:50]}")
    else:
        issue_lines.append("无")
    
    # 调用链列表
    chain_lines = []
    if chains:
        for c in chains[-3:]:
            chain_lines.append(f"  - {c['from']}: {c['direction']} (找到{c['found']}个, 新增{c['new']}个)")
    else:
        chain_lines.append("无")
    
    # 加载 action 定义
    actions_text = format_actions_for_prompt()
    action_names = get_action_names()
    action_choices = "|".join(action_names)
    
    # 从文件加载 prompt
    prompt = load_prompt(
        "react_decide",
        question=question,
        function_count=len(funcs),
        function_list="\n".join(func_lines),
        issue_count=len(collected.get("issues", [])),
        issue_list="\n".join(issue_lines),
        chain_count=len(chains),
        chain_list="\n".join(chain_lines),
        actions=actions_text,
        action_choices=action_choices,
    )
    
    # 调用 DeepSeek 决策
    result = call_llm_json(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=300,  # call_llm_json 会自动调整为1200
        timeout=60,
        model='deepseek-v4-pro',
        provider='deepseek'
    )
    
    # 决策 trace
    decision_trace = {
        "step": step,
        "prompt": prompt[:500],
        "raw_response": result,
    }
    
    # 解析决策
    if result is None:
        decision_trace["fallback"] = "json_parse_failed"
        return {"sufficient": True, "action": "sufficient", "target": ""}, decision_trace
    
    if step >= 4:
        decision_trace["fallback"] = "max_steps_reached"
        return {"sufficient": True, "action": "sufficient", "target": ""}, decision_trace
    
    if result.get("sufficient") or result.get("action") == "sufficient":
        return {"sufficient": True, "action": "sufficient", "target": ""}, decision_trace
    
    action = result.get("action", "")
    target = result.get("target", "")
    
    # 验证 target 是否在前 8 个函数中
    valid_targets = [f['name'] for f in funcs[:8]]
    if target not in valid_targets:
        # 选择第一个未扩展的函数作为 fallback
        for f in funcs[:5]:
            if f['name'] not in expanded:
                target = f['name']
                decision_trace["target_fallback"] = f"original '{target}' not in valid list, fallback to '{f['name']}'"
                break
        else:
            target = funcs[0]['name'] if funcs else ""
            decision_trace["target_fallback"] = "fallback to first function"
    
    # 验证 action 是否有效
    valid_actions = get_action_names()
    if action not in valid_actions:
        action = "expand_callees"
        decision_trace["action_fallback"] = f"invalid action, fallback to expand_callees"
    
    return {
        "thought": result.get("thought", ""),
        "sufficient": False,
        "action": action,
        "target": target
    }, decision_trace


def execute_action(action: str, target: str) -> dict:
    """执行 ReAct action"""
    impl = get_action_impl(action)
    
    if action == "expand_callers":
        return expand_call_chain(target, "callers")
    elif action == "expand_callees":
        return expand_call_chain(target, "callees")
    elif action == "expand_same_file":
        return expand_same_file(target)
    elif action == "expand_same_class":
        return expand_same_class(target)
    else:
        return {"functions": [], "source": target, "type": "unknown"}


def react_search(driver, question: str, trace: dict) -> tuple[dict, dict]:
    """
    ReAct 搜索循环
    返回: (collected, trace)
    """
    collected, initial_trace = initial_search(driver, question)
    trace["initial"] = initial_trace
    
    info_gain_history = []
    react_steps = []
    
    for step in range(2, MAX_STEPS + 1):
        decision, decision_trace = react_decide(question, collected, step)
        
        action = decision.get("action")
        target = decision.get("target", "")
        
        step_trace = {
            "step": step,
            "decision": decision_trace,
        }
        
        if decision.get("sufficient") or action == "sufficient":
            step_trace["result"] = "sufficient - stop"
            react_steps.append(step_trace)
            break
        
        # 执行 action
        if target:
            expansion = execute_action(action, target)
            new_count = 0
            for fn in expansion.get("functions", []):
                if not any(f['name'] == fn['name'] for f in collected["functions"]):
                    fn['score'] = 0.5
                    fn['source'] = f'{action}_of_{target}'
                    collected["functions"].append(fn)
                    new_count += 1
            
            collected["call_chains"].append({
                "from": target,
                "direction": action,
                "found": len(expansion.get("functions", [])),
                "new": new_count
            })
            collected["steps"].append({
                "step": step,
                "action": action,
                "target": target,
                "found": len(expansion.get("functions", [])),
                "new": new_count
            })
            
            step_trace["expansion"] = {
                "action": action,
                "target": target,
                "found": len(expansion.get("functions", [])),
                "new": new_count,
            }
            
            info_gain_history.append(new_count)
            react_steps.append(step_trace)
            
            # 信息增益过低提前停止
            if step >= 3 and len(info_gain_history) >= 2:
                if all(g <= 1 for g in info_gain_history[-2:]):
                    trace["early_stop"] = "info gain too low"
                    break
        else:
            step_trace["result"] = "no target - stop"
            react_steps.append(step_trace)
            break
    
    trace["react_steps"] = react_steps
    trace["final_stats"] = {
        "function_count": len(collected["functions"]),
        "issue_count": len(collected.get("issues", [])),
        "step_count": len(collected["steps"]),
    }
    
    return collected, trace


def process_single(driver, client, row: dict, idx: int) -> dict:
    if idx % 20 == 0:
        print(f"[{idx}] {row.get('具体问题', 'N/A')[:50]}...")
    
    start_time = time.time()
    question = row.get('具体问题', '')
    
    try:
        # ReAct 检索 + 轨迹记录
        trace = {"index": idx, "question": question}
        collected, trace = react_search(driver, question, trace)
        
        # 生成答案
        answer = generate_answer(
            question=question,
            collected=collected,
            max_tokens=8192,
            model='deepseek-v4-pro',
            provider='deepseek'
        )
        latency = time.time() - start_time
        
        return {
            "index": idx,
            "具体问题": question,
            "参考答案": row.get('答案', ''),
            "生成答案": answer,
            "路由类型": "V8_DeepSeek_ReAct_Agent",
            "检索结果": {
                "function_count": len(collected.get("functions", [])),
                "issue_count": len(collected.get("issues", [])),
                "step_count": len(collected.get("steps", [])),
            },
            "检索轨迹": trace,
            "延迟_s": latency
        }
    except Exception as e:
        import traceback
        return {
            "index": idx,
            "具体问题": question,
            "生成答案": f"处理失败: {str(e)}\n{traceback.format_exc()}",
            "路由类型": "V8_DeepSeek_ReAct_Agent",
            "错误": str(e),
            "检索轨迹": trace if 'trace' in dir() else {},
            "延迟_s": time.time() - start_time
        }


def main():
    rows = []
    with open('results/qav2_test_cleaned.csv', 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    
    print(f"DeepSeek ReAct Agent 实验: {len(rows)} 题")
    print(f"模型: deepseek-v4-pro")
    print(f"max_tokens: 8192")
    print(f"ReAct actions: {', '.join(get_action_names())}")
    print(f"File expansion: 后移为 ReAct action")
    print(f"并行: 20 workers")
    print(f"估算耗时: ~{len(rows) * 60 / 20 / 60:.0f} 分钟")
    print()
    
    driver = get_neo4j_driver()
    from tools.core.llm_client import get_llm_client
    client = get_llm_client(provider='deepseek')
    
    results = []
    completed = 0
    
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = {
            executor.submit(process_single, driver, client, row, i): i
            for i, row in enumerate(rows)
        }
        
        for future in as_completed(futures):
            try:
                result = future.result()
                results.append(result)
                completed += 1
                
                if completed % 20 == 0 or completed == len(rows):
                    print(f"  已完成 {completed}/{len(rows)} 题...")
                    sorted_results = sorted(results, key=lambda x: x.get('index', 0))
                    with open('results/v8_deepseek_react_fixed.json', 'w', encoding='utf-8') as f:
                        json.dump(sorted_results, f, ensure_ascii=False, indent=2)
                        
            except Exception as e:
                print(f"  处理题目时出错: {e}")
                completed += 1
    
    sorted_results = sorted(results, key=lambda x: x.get('index', 0))
    with open('results/v8_deepseek_react_fixed.json', 'w', encoding='utf-8') as f:
        json.dump(sorted_results, f, ensure_ascii=False, indent=2)
    
    print(f"\n完成！共处理 {len(results)}/{len(rows)} 题")
    print(f"结果保存至: results/v8_deepseek_react_fixed.json")
    
    close_neo4j_driver()


if __name__ == "__main__":
    main()
