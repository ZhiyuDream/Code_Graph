#!/usr/bin/env python3
"""
DeepSeek 小样本测试脚本（5题）
验证 deepseek-v4-pro 在 V8 + FileExp + GrepV2 配置下是否正常工作
"""
from __future__ import annotations

import os
import sys
import json
import csv
from pathlib import Path

# 强制使用 DeepSeek
os.environ['LLM_MODEL'] = 'deepseek-v4-pro'
os.environ['LLM_PROVIDER'] = 'deepseek'

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))

from pathlib import Path
from tools.core import (
    get_neo4j_driver, close_neo4j_driver, generate_answer
)
from tools.search import (
    search_functions_by_text, expand_call_chain,
    search_issues, extract_entities_from_question,
    convert_grep_to_function_results, search_module_functions,
    semantic_search as semantic_search_func
)
from tools.search.grep_search_v2 import grep_codebase
from tools.search.semantic_search import _load_rag_index

# 常量
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
                if module_funcs:
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
    file_expansion_count = 0
    if funcs:
        original_count = len(funcs)
        funcs = expand_by_file_level(funcs, max_total=FILE_EXPANSION_MAX)
        file_expansion_count = len(funcs) - original_count
    steps = [{
        "step": 1,
        "action": "initial_search",
        "found": len(funcs),
        "fallback_triggered": fallback_triggered,
        "file_expansion": file_expansion_count
    }]
    return {
        "functions": funcs,
        "issues": issues,
        "steps": steps,
        "call_chains": [],
        "tool_calls": [],
        "fallback_triggered": fallback_triggered,
        "file_expansion_count": file_expansion_count
    }

def react_decide(client, question: str, collected: dict, step: int) -> dict:
    funcs = collected.get("functions", [])
    chains = collected.get("call_chains", [])
    if not hasattr(react_decide, 'stats'):
        react_decide.stats = {"total": 0, "default_used": 0, "actions": {}}
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
   适用：调用链、依赖关系、流程分析问题
   
2. expand_callees - 扩展某个函数的被调用者（它调用了谁）
   适用：执行流程、内部实现细节问题
   
3. sufficient - 信息充足，可以生成答案
   适用：已有足够证据回答问题

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
    from tools.core.llm_client import call_llm_json
    result = call_llm_json(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=300,
        timeout=60,
        model='deepseek-v4-pro',
        provider='deepseek'
    )
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
    react_decide.stats["total"] += 1
    final_action = action if action in ["expand_callers", "expand_callees"] else "expand_callees"
    if final_action != action:
        react_decide.stats["default_used"] += 1
    react_decide.stats["actions"][final_action] = react_decide.stats["actions"].get(final_action, 0) + 1
    return {
        "thought": result.get("thought", ""),
        "sufficient": False,
        "action": final_action,
        "target": target
    }

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
            collected["call_chains"].append({
                "from": target,
                "direction": direction,
                "found": len(chain["functions"]),
                "new": new_count
            })
            collected["steps"].append({
                "step": step,
                "action": action,
                "target": target,
                "found": len(chain["functions"]),
                "new": new_count
            })
            info_gain_history.append(new_count)
            if step >= 3 and len(info_gain_history) >= 2:
                if all(g <= 1 for g in info_gain_history[-2:]):
                    break
    return collected

def process_single(driver, client, row: dict, idx: int) -> dict:
    print(f"[{idx}] {row.get('具体问题', 'N/A')[:50]}...")
    import time as time_mod
    start_time = time_mod.time()
    question = row.get('具体问题', '')
    try:
        collected = react_search(driver, client, question)
        answer = generate_answer(question, collected, max_tokens=1500, model='deepseek-v4-pro', provider='deepseek')
        latency = time_mod.time() - start_time
        return {
            "index": idx,
            "具体问题": question,
            "参考答案": row.get('答案', ''),
            "生成答案": answer,
            "路由类型": "V8_DeepSeek_FileExp",
            "检索结果": {
                "function_count": len(collected.get("functions", [])),
                "issue_count": len(collected.get("issues", [])),
                "step_count": len(collected.get("steps", [])),
                "file_expansion_count": collected.get("file_expansion_count", 0)
            },
            "延迟_s": latency
        }
    except Exception as e:
        import traceback
        return {
            "index": idx,
            "具体问题": question,
            "生成答案": f"处理失败: {str(e)}\n{traceback.format_exc()}",
            "路由类型": "V8_DeepSeek_FileExp",
            "错误": str(e),
            "延迟_s": time_mod.time() - start_time
        }

def main():
    # 读取清洁版CSV（前5题）
    rows = []
    with open('results/qav2_test_cleaned.csv', 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            if i >= 3:
                break
            rows.append(row)
    
    print(f"DeepSeek 小样本测试: {len(rows)} 题")
    print(f"模型: deepseek-v4-pro")
    print()
    
    driver = get_neo4j_driver()
    from tools.core.llm_client import get_llm_client
    client = get_llm_client(provider='deepseek')
    
    results = []
    for i, row in enumerate(rows):
        result = process_single(driver, client, row, i)
        results.append(result)
        print(f"  答案(前100字): {result['生成答案'][:100]}")
        print()
    
    with open('results/deepseek_test_5.json', 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    
    print(f"结果保存至: results/deepseek_test_5.json")
    close_neo4j_driver()

if __name__ == "__main__":
    main()
