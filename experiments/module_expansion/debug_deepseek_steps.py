#!/usr/bin/env python3
"""DeepSeek 错题调试 - 记录每一步中间结果"""
from __future__ import annotations

import os
import sys
import json
import csv
import time
from pathlib import Path

os.environ['LLM_MODEL'] = 'deepseek-v4-pro'

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))

from tools.core import get_neo4j_driver, close_neo4j_driver, generate_answer
from tools.core.llm_client import call_llm_json
from tools.search import (
    search_functions_by_text, expand_call_chain,
    search_issues, extract_entities_from_question,
    convert_grep_to_function_results, search_module_functions,
)
from tools.search.grep_search_v2 import grep_codebase
from tools.search.semantic_search import _load_rag_index

MAX_STEPS = 5
FALLBACK_THRESHOLD = 0.5
FILE_EXPANSION_MAX = 50

_rag_index_cache = None

def get_rag_index():
    global _rag_index_cache
    if _rag_index_cache is None:
        _rag_index_cache = _load_rag_index()
    return _rag_index_cache

def expand_by_file_level(initial_funcs: list, max_total: int = FILE_EXPANSION_MAX) -> list:
    rag_index = get_rag_index()
    if not rag_index:
        return initial_funcs
    files_hit = set()
    initial_ids = set()
    for fn in initial_funcs:
        file_path = fn.get('file', '')
        if file_path:
            files_hit.add(file_path)
        initial_ids.add(f"{fn.get('name', '')}:{file_path}")
    if not files_hit:
        return initial_funcs
    expanded_funcs = list(initial_funcs)
    for chunk in rag_index.get("chunks", []):
        if len(expanded_funcs) >= max_total:
            break
        if chunk.get("type") != "function":
            continue
        meta = chunk.get("meta", {})
        func_name = meta.get("name", "")
        file_path = meta.get("file", "")
        func_id = f"{func_name}:{file_path}"
        if func_id in initial_ids:
            continue
        if file_path in files_hit:
            expanded_funcs.append({
                'name': func_name,
                'file': file_path,
                'text': chunk.get("text", "")[:800],
                'score': 0.3,
                'source': 'file_expansion'
            })
            initial_ids.add(func_id)
    return expanded_funcs

def initial_search(driver, question: str, log: dict) -> dict:
    log["step1_initial"] = {"question": question}
    
    funcs = search_functions_by_text(question, top_k=5)
    log["step1_initial"]["embedding_search"] = [
        {"name": f.get('name'), "file": f.get('file'), "score": f.get('score')} 
        for f in funcs
    ]
    
    max_score = max([f.get('score', 0) for f in funcs], default=0)
    fallback_triggered = False
    if max_score < FALLBACK_THRESHOLD:
        fallback_triggered = True
        entities = extract_entities_from_question(question)
        log["step1_initial"]["extracted_entities"] = entities
        for entity in entities[:2]:
            if '-' in entity or entity.islower():
                module_funcs = search_module_functions(entity, limit=5)
                if module_funcs:
                    log["step1_initial"][f"module_search_{entity}"] = [
                        {"name": f.get('name'), "file": f.get('file')} for f in module_funcs
                    ]
                    for fn in module_funcs:
                        if not any(f['name'] == fn['name'] for f in funcs):
                            funcs.append(fn)
            grep_results = grep_codebase(entity, limit=3)
            if grep_results:
                log["step1_initial"][f"grep_search_{entity}"] = grep_results
                new_funcs = convert_grep_to_function_results(grep_results)
                for fn in new_funcs:
                    if not any(f['name'] == fn['name'] for f in funcs):
                        funcs.append(fn)
    
    issues = search_issues(question, top_k=3)
    log["step1_initial"]["issues"] = [
        {"number": i.get('number'), "title": i.get('title')[:80]} for i in issues
    ]
    
    file_expansion_count = 0
    if funcs:
        original_count = len(funcs)
        funcs = expand_by_file_level(funcs, max_total=FILE_EXPANSION_MAX)
        file_expansion_count = len(funcs) - original_count
    
    log["step1_initial"]["final_function_count"] = len(funcs)
    log["step1_initial"]["file_expansion_count"] = file_expansion_count
    log["step1_initial"]["fallback_triggered"] = fallback_triggered
    
    return {
        "functions": funcs,
        "issues": issues,
        "steps": [{"step": 1, "action": "initial_search", "found": len(funcs)}],
        "call_chains": [],
        "tool_calls": [],
        "fallback_triggered": fallback_triggered,
        "file_expansion_count": file_expansion_count
    }

def react_decide(question: str, collected: dict, step: int, log: dict) -> dict:
    funcs = collected.get("functions", [])
    chains = collected.get("call_chains", [])
    context_lines = [f"问题: {question}"]
    context_lines.append(f"\n【已收集函数】(共{len(funcs)}个，按相似度排序):")
    expanded = [c['from'] for c in chains]
    for i, f in enumerate(funcs[:5]):
        source = f.get('source', 'embedding') if f.get('source') else 'embedding'
        score = f.get('score', 0)
        marker = " [已扩展]" if f['name'] in expanded else ""
        context_lines.append(f"{i+1}. {f['name']} ({source}, {score:.3f}){marker}")
    if collected.get("issues"):
        context_lines.append(f"\n【相关Issue】(共{len(collected['issues'])}个):")
        for i, issue in enumerate(collected["issues"][:2]):
            context_lines.append(f"{i+1}. #{issue['number']}: {issue['title'][:50]}")
    if chains:
        context_lines.append(f"\n【已扩展调用链】(共{len(chains)}条):")
        for c in chains[-3:]:
            context_lines.append(f"  - {c['from']}: {c['direction']} (找到{c['found']}个, 新增{c['new']}个)")
    context = '\n'.join(context_lines)
    prompt = f"""{context}

---

你是代码检索专家。请根据当前已收集的信息和问题类型，选择最合适的下一步行动。

【可用工具】
1. expand_callers - 扩展某个函数的调用者（谁调用了它）
2. expand_callees - 扩展某个函数的被调用者（它调用了谁）
3. sufficient - 信息充足，可以生成答案

【决策原则】
- 调用链/依赖问题 → 用 expand_callers/callees
- 信息已足够 → 用 sufficient

【重要】不要重复扩展已标记[已扩展]的函数。

返回JSON:
{{
    "thought": "分析现有信息和下一步计划",
    "sufficient": false,
    "action": "expand_callers|expand_callees|sufficient",
    "target": "目标函数名"
}}

只输出JSON:"""
    
    result = call_llm_json(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=300,
        timeout=60,
        model='deepseek-v4-pro',
        provider='deepseek'
    )
    
    log[f"step{step}_decision"] = {
        "prompt": prompt[:500],
        "raw_response": result
    }
    
    if result is None:
        return {"sufficient": True, "action": "sufficient", "target": ""}
    if step >= 4:
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
    return {
        "thought": result.get("thought", ""),
        "sufficient": False,
        "action": final_action,
        "target": target
    }

def react_search(driver, question: str, log: dict) -> dict:
    collected = initial_search(driver, question, log)
    info_gain_history = []
    for step in range(2, MAX_STEPS + 1):
        decision = react_decide(question, collected, step, log)
        action = decision.get("action")
        target = decision.get("target", "")
        if decision.get("sufficient") or action == "sufficient":
            log[f"step{step}_decision"]["final"] = "sufficient - stop"
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
            collected["call_chains"].append({
                "from": target,
                "direction": direction,
                "found": len(chain["functions"]),
                "new": new_count
            })
            log[f"step{step}_expansion"] = {
                "target": target,
                "direction": direction,
                "found": len(chain["functions"]),
                "new": new_count,
                "new_functions": [{"name": f.get('name'), "file": f.get('file')} for f in chain["functions"]]
            }
            info_gain_history.append(new_count)
            if step >= 3 and len(info_gain_history) >= 2:
                if all(g <= 1 for g in info_gain_history[-2:]):
                    log["early_stop"] = "info gain too low"
                    break
    return collected

def debug_single(driver, row: dict, idx: int, max_tokens: int) -> dict:
    question = row.get('具体问题', '')
    log = {"index": idx, "question": question, "max_tokens": max_tokens}
    
    print(f"\n{'='*60}")
    print(f"题目 [{idx}]: {question[:80]}...")
    print(f"max_tokens={max_tokens}")
    print(f"{'='*60}")
    
    # 1. 检索
    collected = react_search(driver, question, log)
    print(f"\n[检索完成] 共找到 {len(collected['functions'])} 个函数, {len(collected['issues'])} 个issue")
    print(f"  fallback_triggered={collected['fallback_triggered']}, file_expansion={collected['file_expansion_count']}")
    
    # 2. 生成答案
    start = time.time()
    answer = generate_answer(question, collected, max_tokens=max_tokens, model='deepseek-v4-pro', provider='deepseek')
    latency = time.time() - start
    log["answer"] = answer
    log["answer_len"] = len(answer)
    log["latency_s"] = latency
    
    print(f"\n[生成完成] {len(answer)} 字, 耗时 {latency:.1f}s")
    print(f"  开头: {answer[:100]}")
    print(f"  结尾: ...{answer[-100:]}")
    
    return log

def main():
    indices = json.load(open('/tmp/deepseek_debug_indices.json'))
    
    rows = []
    with open('results/qav2_test_cleaned.csv', 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if int(row['index']) in indices:
                rows.append(row)
    
    driver = get_neo4j_driver()
    
    all_logs = []
    for row in rows:
        idx = int(row['index'])
        for max_t in [1500, 8192]:
            log = debug_single(driver, row, idx, max_t)
            all_logs.append(log)
    
    close_neo4j_driver()
    
    # 保存详细日志
    output_path = 'results/deepseek_debug_steps.json'
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(all_logs, f, ensure_ascii=False, indent=2)
    
    print(f"\n{'='*60}")
    print(f"调试完成！日志保存至: {output_path}")
    print(f"{'='*60}")

if __name__ == "__main__":
    main()
