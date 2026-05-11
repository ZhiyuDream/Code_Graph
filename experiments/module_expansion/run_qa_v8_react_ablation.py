#!/usr/bin/env python3
"""
V8 ReAct Agent - 组件化消融实验版

支持可选检索器组合：embedding, grep, graph, issue
支持自定义 benchmark (JSON) 和模型
"""
from __future__ import annotations

import os
import sys
import json
import time
import argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

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
from qa_framework.retrievers.graph_retriever import GraphRetriever
from config import NEO4J_DATABASE, REPO_ROOT, LLM_MODEL, OPENAI_BASE_URL

MAX_STEPS = 5
FALLBACK_THRESHOLD = 0.5

_rag_index_cache = None

def get_rag_index():
    global _rag_index_cache
    if _rag_index_cache is None:
        _rag_index_cache = _load_rag_index()
    return _rag_index_cache


def initial_search(driver, question: str, retrievers: set, repo_root: str) -> tuple[dict, dict]:
    """
    初始检索（组件化）
    retrievers: {'embedding', 'grep', 'graph', 'issue'}
    """
    trace = {"phase": "initial_search", "question": question, "retrievers": list(retrievers)}
    funcs = []
    fallback_triggered = False
    fallback_results = []
    
    # 1. Embedding 检索
    if "embedding" in retrievers:
        emb_funcs = search_functions_by_text(question, top_k=20)
        trace["embedding_search"] = [
            {"name": f.get('name'), "file": f.get('file'), "score": f.get('score')} 
            for f in emb_funcs
        ]
        for fn in emb_funcs:
            if not any(f['name'] == fn['name'] for f in funcs):
                funcs.append(fn)
    
    # 2. Graph 检索（关键词匹配函数名 + CALLS扩展）
    if "graph" in retrievers:
        graph_retriever = GraphRetriever(
            driver=driver,
            repo_root=repo_root,
            database=NEO4J_DATABASE,
            enabled=True,
            expand_calls_depth=1,
        )
        graph_results = graph_retriever.retrieve(question, top_k=10)
        trace["graph_search"] = len(graph_results)
        for r in graph_results:
            if r.type == "function":
                fn = {
                    'name': r.id.split(":")[-1] if ":" in r.id else r.id,
                    'file': r.metadata.get('file_path', ''),
                    'text': r.content,
                    'score': r.score,
                    'source': 'graph',
                }
                if not any(f['name'] == fn['name'] for f in funcs):
                    funcs.append(fn)
    
    # 3. Grep 检索
    if "grep" in retrievers:
        max_score = max([f.get('score', 0) for f in funcs], default=0)
        if max_score < FALLBACK_THRESHOLD or "embedding" not in retrievers:
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
    
    # 4. Issue 检索
    issues = []
    if "issue" in retrievers:
        issues = search_issues(question, top_k=3)
    trace["issues"] = [
        {"number": i.get('number'), "title": (i.get('title') or '')[:80]} for i in issues
    ]
    
    collected = {
        "functions": funcs,
        "issues": issues,
        "steps": [{"step": 1, "action": "initial_search", "found": len(funcs)}],
        "call_chains": [],
        "fallback_triggered": fallback_triggered,
    }
    
    return collected, trace


def react_decide(question: str, collected: dict, step: int, model: str, provider: str) -> tuple[dict, dict]:
    """ReAct 决策"""
    funcs = collected.get("functions", [])
    chains = collected.get("call_chains", [])
    
    expanded = [c['from'] for c in chains]
    func_lines = []
    for i, f in enumerate(funcs[:8]):
        source = f.get('source', 'embedding') if f.get('source') else 'embedding'
        score = f.get('score', 0)
        marker = " [已扩展]" if f['name'] in expanded else ""
        func_lines.append(f"{i+1}. {f['name']} ({source}, {score:.3f}){marker}")
    
    if len(funcs) > 8:
        func_lines.append(f"   ... 还有 {len(funcs)-8} 个函数")
    
    issue_lines = []
    if collected.get("issues"):
        for i, issue in enumerate(collected["issues"][:2]):
            issue_lines.append(f"{i+1}. #{issue['number']}: {issue['title'][:50]}")
    else:
        issue_lines.append("无")
    
    chain_lines = []
    if chains:
        for c in chains[-3:]:
            chain_lines.append(f"  - {c['from']}: {c['direction']} (找到{c['found']}个, 新增{c['new']}个)")
    else:
        chain_lines.append("无")
    
    actions_text = format_actions_for_prompt()
    action_names = get_action_names()
    action_choices = "|".join(action_names)
    
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
    
    result = call_llm_json(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=300,
        timeout=600,
        model=model,
        provider=provider,
    )
    
    decision_trace = {
        "step": step,
        "prompt": prompt[:500],
        "raw_response": result,
    }
    
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
    
    valid_targets = [f['name'] for f in funcs[:8]]
    if target not in valid_targets:
        for f in funcs[:5]:
            if f['name'] not in expanded:
                target = f['name']
                decision_trace["target_fallback"] = f"fallback to '{f['name']}'"
                break
        else:
            target = funcs[0]['name'] if funcs else ""
            decision_trace["target_fallback"] = "fallback to first function"
    
    valid_actions = get_action_names()
    if action not in valid_actions:
        action = "expand_callees"
        decision_trace["action_fallback"] = "invalid action, fallback to expand_callees"
    
    return {
        "thought": result.get("thought", ""),
        "sufficient": False,
        "action": action,
        "target": target
    }, decision_trace


def execute_action(action: str, target: str) -> dict:
    """执行 ReAct action"""
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


def react_search(driver, question: str, trace: dict, retrievers: set, repo_root: str, model: str, provider: str) -> tuple[dict, dict]:
    """ReAct 搜索循环"""
    collected, initial_trace = initial_search(driver, question, retrievers, repo_root)
    trace["initial"] = initial_trace
    
    info_gain_history = []
    react_steps = []
    
    for step in range(2, MAX_STEPS + 1):
        decision, decision_trace = react_decide(question, collected, step, model, provider)
        
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


def process_single(driver, row: dict, idx: int, retrievers: set, repo_root: str, model: str, provider: str) -> dict:
    if idx % 20 == 0:
        print(f"[{idx}] {row.get('question', 'N/A')[:50]}...")
    
    # 重置 token usage 统计
    from tools.core.llm_client import reset_usage_stats, get_usage_stats
    reset_usage_stats()
    
    start_time = time.time()
    question = row.get('question', '')
    
    try:
        trace = {"index": idx, "question": question}
        collected, trace = react_search(driver, question, trace, retrievers, repo_root, model, provider)
        
        answer = generate_answer(
            question=question,
            collected=collected,
            max_tokens=8192,
            model=model,
            provider=provider,
        )
        latency = time.time() - start_time
        
        # 收集工具调用统计
        tool_stats = {}
        for step in collected.get("steps", []):
            action = step.get("action", "")
            if action not in tool_stats:
                tool_stats[action] = {"count": 0, "total_found": 0, "total_new": 0}
            tool_stats[action]["count"] += 1
            tool_stats[action]["total_found"] += step.get("found", 0)
            tool_stats[action]["total_new"] += step.get("new", 0)
        
        # 获取 token usage
        usage_stats = get_usage_stats()
        
        return {
            "index": idx,
            "id": row.get('id', f'qa_{idx}'),
            "question": question,
            "reference": row.get('answer', ''),
            "generated": answer,
            "router": "V8_ReAct_Agent",
            "retrievers": list(retrievers),
            "retrieval": {
                "function_count": len(collected.get("functions", [])),
                "issue_count": len(collected.get("issues", [])),
                "step_count": len(collected.get("steps", [])),
            },
            "trace": trace,
            "latency_s": latency,
            "usage": usage_stats,
            "tool_stats": tool_stats,
        }
    except Exception as e:
        import traceback
        usage_stats = get_usage_stats()
        return {
            "index": idx,
            "id": row.get('id', f'qa_{idx}'),
            "question": question,
            "generated": f"处理失败: {str(e)}\n{traceback.format_exc()}",
            "router": "V8_ReAct_Agent",
            "retrievers": list(retrievers),
            "error": str(e),
            "trace": trace if 'trace' in dir() else {},
            "latency_s": time.time() - start_time,
            "usage": usage_stats,
            "tool_stats": {},
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, required=True, help="Benchmark JSON 路径")
    parser.add_argument("--retrievers", default="embedding,issue", help="检索器组合: embedding,grep,graph,issue")
    parser.add_argument("-o", "--output", type=Path, required=True, help="输出结果 JSON")
    parser.add_argument("--model", default=LLM_MODEL, help="生成模型")
    parser.add_argument("--provider", default="openai", help="模型 provider")
    parser.add_argument("-w", "--workers", type=int, default=20, help="并行 worker 数")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条")
    parser.add_argument("--offset", type=int, default=0, help="从第 N 条开始跑")
    args = parser.parse_args()
    
    # 加载 benchmark
    with open(args.benchmark, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    rows = data.get('questions', [])
    if args.offset > 0:
        rows = rows[args.offset:]
    if args.limit > 0:
        rows = rows[:args.limit]
    
    retrievers = set(args.retrievers.split(","))
    repo_root = REPO_ROOT or "/root/data/zzy/llama.cpp"
    
    print(f"V8 ReAct Agent 消融实验")
    print(f"Benchmark: {args.benchmark} ({len(rows)} 题)")
    print(f"检索器: {', '.join(retrievers)}")
    print(f"模型: {args.model} ({args.provider})")
    print(f"ReAct actions: {', '.join(get_action_names())}")
    print(f"并行: {args.workers} workers")
    print(f"估算耗时: ~{len(rows) * 60 / args.workers / 60:.0f} 分钟")
    print()
    
    driver = get_neo4j_driver()
    
    results = []
    completed = 0
    
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(process_single, driver, row, i, retrievers, repo_root, args.model, args.provider): i
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
                    args.output.parent.mkdir(parents=True, exist_ok=True)
                    with open(args.output, 'w', encoding='utf-8') as f:
                        json.dump(sorted_results, f, ensure_ascii=False, indent=2)
                        
            except Exception as e:
                print(f"  处理题目时出错: {e}")
                completed += 1
    
    sorted_results = sorted(results, key=lambda x: x.get('index', 0))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(sorted_results, f, ensure_ascii=False, indent=2)
    
    print(f"\n完成！共处理 {len(results)}/{len(rows)} 题")
    print(f"结果保存至: {args.output}")
    
    close_neo4j_driver()


if __name__ == "__main__":
    main()
