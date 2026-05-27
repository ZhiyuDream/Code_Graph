#!/usr/bin/env python3
"""
Issue Benchmark - Smart Full-files
在 React Search Baseline 基础上，增加 LLM 按需决策的完整文件查看
"""
from __future__ import annotations

import sys
import json
import argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))

from config import LLM_MODEL
from tools.core import (
    get_neo4j_driver, close_neo4j_driver,
    call_llm_json, generate_answer
)
from tools.search import (
    search_functions_by_text, expand_call_chain,
    search_issues, extract_entities_from_question, grep_codebase,
    convert_grep_to_function_results, search_module_functions
)
from tools.core.full_file_selector import collect_full_files_smart

MAX_STEPS = 5
FALLBACK_THRESHOLD = 0.5


def initial_search(driver, client, question: str) -> dict:
    funcs = search_functions_by_text(question, top_k=5)
    max_score = max([f.get('score', 0) for f in funcs], default=0)
    fallback_triggered = False
    
    if max_score < FALLBACK_THRESHOLD:
        fallback_triggered = True
        entities = extract_entities_from_question(question)
        for entity in entities[:2]:
            if '-' in entity or entity.islower():
                module_funcs = search_module_functions(entity, limit=5)
                for fn in module_funcs:
                    if not any(f['name'] == fn['name'] for f in funcs):
                        funcs.append(fn)
            grep_results = grep_codebase(entity, limit=3)
            if grep_results:
                new_funcs = convert_grep_to_function_results(grep_results)
                for fn in new_funcs:
                    if not any(f['name'] == fn['name'] for f in funcs):
                        funcs.append(fn)
    
    issues = search_issues(question, top_k=3)
    steps = [{"step": 1, "action": "initial_search", "found": len(funcs), "fallback_triggered": fallback_triggered}]
    
    return {
        "functions": funcs,
        "issues": issues,
        "steps": steps,
        "call_chains": [],
        "tool_calls": [],
        "fallback_triggered": fallback_triggered
    }


def react_decide(client, question: str, collected: dict, step: int) -> dict:
    funcs = collected.get("functions", [])
    chains = collected.get("call_chains", [])
    expanded = [c['from'] for c in chains]
    
    context_lines = [f"问题: {question}", f"\n【已收集函数】(共{len(funcs)}个):"]
    for i, f in enumerate(funcs[:5]):
        source = f.get('source', 'embedding') if f.get('source') else 'embedding'
        score = f.get('score', 0)
        marker = " [已扩展]" if f['name'] in expanded else ""
        context_lines.append(f"{i+1}. {f['name']} ({source}, {score:.3f}){marker}")
    
    prompt = f"""{'\n'.join(context_lines)}

你是代码检索专家。请根据当前已收集的信息和问题类型，选择最合适的下一步行动。

【可用工具】
1. expand_callers - 扩展某个函数的调用者
2. expand_callees - 扩展某个函数的被调用者
3. sufficient - 信息充足，可以生成答案

返回JSON:
{{
    "thought": "分析",
    "sufficient": false,
    "action": "expand_callers|expand_callees|sufficient",
    "target": "目标函数名"
}}

只输出JSON:"""
    
    result = call_llm_json(messages=[{"role": "user", "content": prompt}], max_tokens=150)
    if result is None or step >= 4:
        return {"sufficient": True, "action": "sufficient", "target": ""}
    
    if result.get("sufficient") or result.get("action") == "sufficient":
        return {"sufficient": True, "action": "sufficient", "target": ""}
    
    action = result.get("action", "")
    target = result.get("target", "")
    valid = [f['name'] for f in funcs[:8]]
    if target not in valid:
        for f in funcs[:5]:
            if f['name'] not in expanded:
                target = f['name']
                break
        else:
            target = funcs[0]['name'] if funcs else ""
    
    final_action = action if action in ["expand_callers", "expand_callees"] else "expand_callees"
    return {"thought": result.get("thought", ""), "sufficient": False, "action": final_action, "target": target}


def react_search(driver, client, question: str) -> dict:
    collected = initial_search(driver, client, question)
    info_gain_history = []
    
    for step in range(2, MAX_STEPS + 1):
        decision = react_decide(client, question, collected, step)
        action = decision.get("action")
        target = decision.get("target", "")
        
        if decision.get("sufficient") or action == "sufficient":
            break
        
        if action in ["expand_callers", "expand_callees"] and target:
            direction = "callers" if action == "expand_callers" else "callees"
            chain = expand_call_chain(target, direction)
            new_count = 0
            for fn in chain["functions"]:
                if not any(f['name'] == fn['name'] for f in collected["functions"]):
                    fn['score'] = 0.5
                    fn['source'] = f'{direction}_of_{target}'
                    collected["functions"].append(fn)
                    new_count += 1
            
            collected["call_chains"].append({"from": target, "direction": direction, "found": len(chain["functions"]), "new": new_count})
            collected["steps"].append({"step": step, "action": action, "target": target, "found": len(chain["functions"]), "new": new_count})
            info_gain_history.append(new_count)
            if step >= 3 and len(info_gain_history) >= 2:
                if all(g <= 1 for g in info_gain_history[-2:]):
                    break
    
    return collected


def process_single(driver, client, item: dict, idx: int) -> dict:
    import time
    start_time = time.time()
    question = item.get('question', '')
    
    try:
        # 1. React Search
        collected = react_search(driver, client, question)
        
        # 2. Smart Full-files 决策
        collected = collect_full_files_smart(collected, question, client=client)
        
        # 3. 生成答案
        answer = generate_answer(question, collected)
        latency = time.time() - start_time
        
        return {
            "index": idx,
            "id": item.get('id', f"issue_{idx}"),
            "issue_number": item.get('issue_number'),
            "difficulty": item.get('difficulty'),
            "question_type": item.get('question_type'),
            "question": question,
            "answer": item.get('answer', ''),
            "generated": answer,
            "retrieval": {
                "function_count": len(collected.get("functions", [])),
                "step_count": len(collected.get("steps", [])),
                "full_files_count": len(collected.get("full_files", []))
            },
            "latency_s": latency
        }
    except Exception as e:
        return {
            "index": idx,
            "id": item.get('id', f"issue_{idx}"),
            "question": question,
            "generated": f"处理失败: {str(e)}",
            "error": str(e),
            "latency_s": time.time() - start_time
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, default=Path("datasets/llama_cpp_issue_benchmark.with_answers.json"))
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("-w", "--workers", type=int, default=10)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    
    with open(args.benchmark, encoding="utf-8") as f:
        data = json.load(f)
    questions = data.get("questions", [])
    if args.limit > 0:
        questions = questions[:args.limit]
    
    print(f"Issue Benchmark (Smart): {len(questions)} 题")
    
    driver = get_neo4j_driver()
    from tools.core.llm_client import get_llm_client
    client = get_llm_client()
    
    results = [None] * len(questions)
    completed = 0
    
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(process_single, driver, client, q, i): i
            for i, q in enumerate(questions)
        }
        for future in as_completed(futures):
            i = futures[future]
            try:
                res = future.result()
                results[i] = res
                completed += 1
                if completed % 10 == 0 or completed == len(questions):
                    print(f"  [{completed}/{len(questions)}] 已完成")
                    sorted_results = sorted([r for r in results if r], key=lambda x: x.get('index', 0))
                    with open(args.output, 'w', encoding='utf-8') as f:
                        json.dump(sorted_results, f, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f"  处理失败: {e}")
                completed += 1
    
    sorted_results = sorted([r for r in results if r], key=lambda x: x.get('index', 0))
    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(sorted_results, f, ensure_ascii=False, indent=2)
    
    print(f"\n完成！结果保存至: {args.output}")
    close_neo4j_driver()


if __name__ == "__main__":
    main()
