#!/usr/bin/env python3
"""
Prompt A/B 测试：对比 baseline react_decide vs GPT-5.4-style react_decide
用法:
    # Baseline 已有结果，直接用前50题
    # GPT-5.4 style:
    python run_qa_prompt_ablation.py --prompt gpt54 --limit 50 -o results/ablation_gpt54_50.json
"""
from __future__ import annotations

import sys
import json
import argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

from tools.core import (
    get_neo4j_driver, close_neo4j_driver,
    call_llm_json, generate_answer
)
from tools.search import (
    search_functions_by_text, expand_call_chain,
    search_issues, extract_entities_from_question, grep_codebase,
    convert_grep_to_function_results, search_module_functions
)
from tools.core.prompt_loader import load_prompt

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


def build_react_decide_prompt(question: str, collected: dict, prompt_template: str) -> str:
    funcs = collected.get("functions", [])
    chains = collected.get("call_chains", [])
    issues = collected.get("issues", [])
    
    expanded = [c['from'] for c in chains]
    
    function_lines = []
    for i, f in enumerate(funcs[:8]):
        source = f.get('source', 'embedding') if f.get('source') else 'embedding'
        score = f.get('score', 0)
        marker = " [已扩展]" if f['name'] in expanded else ""
        function_lines.append(f"{i+1}. {f['name']} ({source}, {score:.3f}){marker}")
    
    issue_lines = []
    if issues:
        for i, issue in enumerate(issues[:2]):
            issue_lines.append(f"{i+1}. #{issue['number']}: {issue['title'][:50]}")
    else:
        issue_lines.append("无")
    
    chain_lines = []
    if chains:
        for c in chains[-3:]:
            chain_lines.append(f"  - {c['from']}: {c['direction']} (找到{c['found']}个, 新增{c['new']}个)")
    else:
        chain_lines.append("无")
    
    actions = [
        "1. expand_callers - 扩展某个函数的调用者（谁调用了它）",
        "2. expand_callees - 扩展某个函数的被调用者（它调用了谁）",
        "3. expand_same_file - 扩展同一文件内的其他函数",
        "4. sufficient - 信息充足，可以生成答案"
    ]
    
    return load_prompt(
        prompt_template,
        question=question,
        function_count=len(funcs),
        function_list="\n".join(function_lines),
        issue_count=len(issues),
        issue_list="\n".join(issue_lines),
        chain_count=len(chains),
        chain_list="\n".join(chain_lines),
        actions="\n".join(actions),
        action_choices="expand_callers|expand_callees|expand_same_file|sufficient"
    )


def react_decide(client, question: str, collected: dict, step: int, prompt_template: str) -> dict:
    prompt = build_react_decide_prompt(question, collected, prompt_template)
    
    result = call_llm_json(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=150
    )
    
    if result is None or step >= 4:
        return {"sufficient": True, "action": "sufficient", "target": ""}
    
    if result.get("sufficient") or result.get("action") == "sufficient":
        return {"sufficient": True, "action": "sufficient", "target": ""}
    
    action = result.get("action", "")
    target = result.get("target", "")
    
    funcs = collected.get("functions", [])
    expanded = [c['from'] for c in collected.get("call_chains", [])]
    valid = [f['name'] for f in funcs[:8]]
    
    if target not in valid:
        for f in funcs[:5]:
            if f['name'] not in expanded:
                target = f['name']
                break
        else:
            target = funcs[0]['name'] if funcs else ""
    
    final_action = action if action in ["expand_callers", "expand_callees", "expand_same_file"] else "expand_same_file"
    
    return {
        "thought": result.get("thought", ""),
        "sufficient": False,
        "action": final_action,
        "target": target
    }


def react_search(driver, client, question: str, prompt_template: str) -> dict:
    collected = initial_search(driver, client, question)
    info_gain_history = []
    
    for step in range(2, MAX_STEPS + 1):
        decision = react_decide(client, question, collected, step, prompt_template)
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
        
        elif action == "expand_same_file" and target:
            target_func = next((f for f in collected["functions"] if f['name'] == target), None)
            if target_func and target_func.get('file'):
                same_file_funcs = search_module_functions(target_func['file'], limit=5)
                new_count = 0
                for fn in same_file_funcs:
                    if not any(f['name'] == fn['name'] for f in collected["functions"]):
                        fn['score'] = 0.4
                        fn['source'] = f'same_file_as_{target}'
                        collected["functions"].append(fn)
                        new_count += 1
                
                collected["call_chains"].append({"from": target, "direction": "same_file", "found": len(same_file_funcs), "new": new_count})
                collected["steps"].append({"step": step, "action": action, "target": target, "found": len(same_file_funcs), "new": new_count})
                info_gain_history.append(new_count)
    
    return collected


def process_single(driver, client, item: dict, idx: int, prompt_template: str) -> dict:
    import time
    start_time = time.time()
    question = item.get('question', '')
    
    try:
        collected = react_search(driver, client, question, prompt_template)
        answer = generate_answer(question, collected)
        latency = time.time() - start_time
        
        return {
            "index": idx,
            "id": item.get('id', f'qa_{idx}'),
            "question": question,
            "answer": item.get('answer', ''),
            "generated": answer,
            "prompt_version": prompt_template,
            "retrieval": {
                "function_count": len(collected.get("functions", [])),
                "step_count": len(collected.get("steps", []))
            },
            "react_steps": collected.get("steps", []),
            "call_chains": collected.get("call_chains", []),
            "latency_s": latency
        }
    except Exception as e:
        import traceback
        return {
            "index": idx,
            "id": item.get('id', f'qa_{idx}'),
            "question": question,
            "generated": f"处理失败: {str(e)}",
            "prompt_version": prompt_template,
            "error": str(e) + "\n" + traceback.format_exc(),
            "latency_s": time.time() - start_time
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, default=Path("datasets/llama_cpp_QA_cleaned.json"))
    parser.add_argument("--prompt", choices=["baseline", "gpt54"], default="baseline")
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("-w", "--workers", type=int, default=25)
    args = parser.parse_args()
    
    prompt_template = "react_decide" if args.prompt == "baseline" else "react_decide_gpt54_style"
    
    with open(args.benchmark, encoding="utf-8") as f:
        data = json.load(f)
    questions = data.get("questions", [])[:args.limit]
    
    print(f"Prompt: {args.prompt} ({prompt_template})")
    print(f"数据源: {args.benchmark} ({len(questions)}题)")
    
    driver = get_neo4j_driver()
    from tools.core.llm_client import get_llm_client
    client = get_llm_client()
    
    results = [None] * len(questions)
    completed = 0
    
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(process_single, driver, client, q, i, prompt_template): i
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
                    sorted_results = [r for r in results if r]
                    with open(args.output, 'w', encoding='utf-8') as f:
                        json.dump(sorted_results, f, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f"  处理失败: {e}")
                completed += 1
    
    sorted_results = [r for r in results if r]
    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(sorted_results, f, ensure_ascii=False, indent=2)
    
    print(f"\n完成！结果保存至: {args.output}")
    print(f"下一步: python tools/eval_with_model.py -i {args.output} -o {args.output.with_suffix('.eval.json')} -m gpt-4.1-mini -w 20")
    close_neo4j_driver()


if __name__ == "__main__":
    main()
