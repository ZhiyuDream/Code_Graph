#!/usr/bin/env python3
"""
V8 - 无 Callers/Callees 扩展 实验脚本

与 V7 Minimal 的区别：
- V7 Minimal: 硬编码跳过 callers/callees（Agent 选择扩展但执行空操作 → 步骤空转）
- 本版本: 从 Agent 的可选动作中**完全删除** expand_callers/expand_callees
  Agent 只能选择 sufficient（停止），不会产生空转步骤

用法:
    # 无 callers/callees + 文件扩展
    python run_qa_v8_no_callers_callees.py --csv results/qav2_test.csv --output results/v8_no_cc.json --workers 20 --file-expansion
    
    # 无 callers/callees 纯 baseline
    python run_qa_v8_no_callers_callees.py --csv results/qav2_test.csv --output results/v8_no_cc_baseline.json --workers 20
"""
from __future__ import annotations

import sys
import json
import csv
import argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# 添加项目根目录到路径
_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))

from tools.core import (
    get_neo4j_driver, close_neo4j_driver, run_cypher,
    call_llm, call_llm_json, generate_answer
)
from tools.search import (
    search_functions_by_text, expand_call_chain,
    search_issues, extract_entities_from_question, grep_codebase,
    convert_grep_to_function_results, search_module_functions,
    semantic_search as semantic_search_func
)
from tools.search.semantic_search import _load_rag_index

# 常量
MAX_STEPS = 5
FALLBACK_THRESHOLD = 0.5
FILE_EXPANSION_MAX = 50

# 全局 RAG index 缓存
_rag_index_cache = None


def get_rag_index():
    """获取全局 RAG index"""
    global _rag_index_cache
    if _rag_index_cache is None:
        _rag_index_cache = _load_rag_index()
    return _rag_index_cache


def expand_by_file_level(initial_funcs: list, max_total: int = FILE_EXPANSION_MAX) -> list:
    """文件级扩展：从初始召回的函数中，扩展到同文件的其他函数"""
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
    added_count = 0
    
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
            added_count += 1
    
    return expanded_funcs


def initial_search(driver, client, question: str, enable_file_expansion: bool = False) -> dict:
    """初始检索：语义搜索 + Grep Fallback + Issue搜索 + 可选文件扩展"""
    funcs = search_functions_by_text(question, top_k=5)
    
    max_score = max([f.get('score', 0) for f in funcs], default=0)
    fallback_triggered = False
    
    if max_score < FALLBACK_THRESHOLD:
        fallback_triggered = True
        print(f"      Embedding最高相似度{max_score:.2f} < {FALLBACK_THRESHOLD}，触发主动搜索...")
        
        entities = extract_entities_from_question(question)
        print(f"      提取实体: {entities}")
        
        for entity in entities[:2]:
            if '-' in entity or entity.islower():
                print(f"      模块搜索: {entity}...")
                module_funcs = search_module_functions(entity, limit=5)
                if module_funcs:
                    print(f"        找到 {len(module_funcs)} 个模块函数")
                    for fn in module_funcs:
                        if not any(f['name'] == fn['name'] for f in funcs):
                            funcs.append(fn)
            
            print(f"      Grep搜索: {entity}...")
            grep_results = grep_codebase(entity, limit=3)
            if grep_results:
                print(f"        找到 {len(grep_results)} 个相关函数")
                new_funcs = convert_grep_to_function_results(grep_results)
                for fn in new_funcs:
                    if not any(f['name'] == fn['name'] for f in funcs):
                        funcs.append(fn)
    
    issues = search_issues(question, top_k=3)
    
    file_expansion_count = 0
    if enable_file_expansion and funcs:
        original_count = len(funcs)
        funcs = expand_by_file_level(funcs, max_total=FILE_EXPANSION_MAX)
        file_expansion_count = len(funcs) - original_count
        print(f"      文件级扩展: {original_count} → {len(funcs)} (+{file_expansion_count})")
    
    steps = [{
        "step": 1,
        "action": "initial_search",
        "found": len(funcs),
        "fallback_triggered": fallback_triggered,
        "file_expansion": file_expansion_count if enable_file_expansion else 0
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


def react_decide_no_cc(client, question: str, collected: dict, step: int) -> dict:
    """
    ReAct决策：从可选动作中完全删除 expand_callers/expand_callees
    Agent 只能选择 sufficient（停止）
    """
    funcs = collected.get("functions", [])
    
    if not hasattr(react_decide_no_cc, 'stats'):
        react_decide_no_cc.stats = {"total": 0, "stop_early": 0}
    
    context_lines = [f"问题: {question}"]
    context_lines.append(f"\n【已收集函数】(共{len(funcs)}个，按相似度排序):")
    
    for i, f in enumerate(funcs[:5]):
        source = f.get('source', 'embedding') if f.get('source') else 'embedding'
        score = f.get('score', 0)
        context_lines.append(f"{i+1}. {f['name']} ({source}, {score:.3f})")
    
    issues = collected.get("issues", [])
    if issues:
        context_lines.append(f"\n【相关Issue】(共{len(issues)}个):")
        for i, issue in enumerate(issues[:2]):
            context_lines.append(f"{i+1}. #{issue['number']}: {issue['title'][:50]}")
    
    context = '\n'.join(context_lines)
    
    prompt = f"""{context}

---

你是代码检索专家。请根据当前已收集的信息和问题类型，判断信息是否充足。

【可用工具】
- sufficient - 信息充足，可以生成答案
  适用：已有足够证据回答问题

【决策原则】
- 如果已收集的函数和Issue足以回答问题 → sufficient
- 如果信息明显不足 → sufficient（因为没有其他可用工具）

返回JSON:
{{
    "thought": "分析现有信息是否充足",
    "sufficient": true
}}

只输出JSON:"""
    
    result = call_llm_json(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=150
    )
    
    react_decide_no_cc.stats["total"] += 1
    
    if result is None:
        react_decide_no_cc.stats["stop_early"] += 1
        return {"sufficient": True, "action": "sufficient", "target": ""}
    
    # 强制停止（本实验无扩展动作，任何情况都停止）
    react_decide_no_cc.stats["stop_early"] += 1
    return {"sufficient": True, "action": "sufficient", "target": ""}


def react_search_no_cc(driver, client, question: str, enable_file_expansion: bool = False) -> dict:
    """
    ReAct迭代检索主流程 - 无 callers/callees 版本
    
    与原始版本的区别：
    - 初始检索后，Agent 只能选择 sufficient（停止）
    - 不会产生 expand_callers/expand_callees 的空转步骤
    """
    # 初始检索（包含Grep Fallback和文件扩展）
    collected = initial_search(driver, client, question, enable_file_expansion)
    
    # 虽然本实验无扩展动作，但保留框架以便对比
    # 实际上只执行一步决策，立即返回
    for step in range(2, MAX_STEPS + 1):
        decision = react_decide_no_cc(client, question, collected, step)
        
        if decision.get("sufficient") or decision.get("action") == "sufficient":
            collected["steps"].append({
                "step": step,
                "action": "sufficient",
                "info": "无callers/callees扩展可用，直接停止"
            })
            break
    
    return collected


def process_single(driver, client, row: dict, idx: int, enable_file_expansion: bool = False) -> dict:
    """处理单个问题"""
    print(f"[{idx}] {row.get('具体问题', 'N/A')[:50]}...")
    
    import time
    start_time = time.time()
    
    question = row.get('具体问题', '')
    
    try:
        collected = react_search_no_cc(driver, client, question, enable_file_expansion)
        
        answer = generate_answer(question, collected)
        
        latency = time.time() - start_time
        
        return {
            "index": idx,
            "具体问题": question,
            "参考答案": row.get('答案', ''),
            "生成答案": answer,
            "路由类型": "V8_NoCallersCallees_FileExp" if enable_file_expansion else "V8_NoCallersCallees",
            "检索结果": {
                "function_count": len(collected.get("functions", [])),
                "issue_count": len(collected.get("issues", [])),
                "step_count": len(collected.get("steps", [])),
                "file_expansion_count": collected.get("file_expansion_count", 0)
            },
            "延迟_s": latency
        }
        
    except Exception as e:
        return {
            "index": idx,
            "具体问题": question,
            "生成答案": f"处理失败: {str(e)}",
            "路由类型": "V8_NoCallersCallees_FileExp" if enable_file_expansion else "V8_NoCallersCallees",
            "错误": str(e),
            "延迟_s": time.time() - start_time
        }


def main():
    parser = argparse.ArgumentParser(description="V8 - 无Callers/Callees扩展实验")
    parser.add_argument("--csv", type=Path, required=True, help="输入CSV文件")
    parser.add_argument("--output", type=Path, required=True, help="输出JSON文件")
    parser.add_argument("--workers", type=int, default=20, help="并行数 (默认20)")
    parser.add_argument("--file-expansion", action="store_true", help="启用文件级扩展")
    args = parser.parse_args()
    
    rows = []
    with open(args.csv, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    
    print(f"共 {len(rows)} 题需要处理")
    print(f"模式: {'无CC+文件扩展' if args.file_expansion else '无CC纯Baseline'}")
    print(f"并行: {args.workers} workers")
    print(f"⚠️  本实验已从Agent动作中完全移除 expand_callers/expand_callees")
    
    driver = get_neo4j_driver()
    
    from tools.core.llm_client import get_llm_client
    client = get_llm_client()
    
    results = []
    completed = 0
    
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(process_single, driver, client, row, i, args.file_expansion): i
            for i, row in enumerate(rows)
        }
        
        for future in as_completed(futures):
            try:
                result = future.result()
                results.append(result)
                completed += 1
                
                if completed % 10 == 0 or completed == len(rows):
                    print(f"  已完成 {completed}/{len(rows)} 题...")
                    sorted_results = sorted(results, key=lambda x: x.get('index', 0))
                    with open(args.output, 'w', encoding='utf-8') as f:
                        json.dump(sorted_results, f, ensure_ascii=False, indent=2)
                        
            except Exception as e:
                print(f"  处理题目时出错: {e}")
                completed += 1
    
    sorted_results = sorted(results, key=lambda x: x.get('index', 0))
    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(sorted_results, f, ensure_ascii=False, indent=2)
    
    print(f"\n完成！共处理 {len(results)}/{len(rows)} 题")
    print(f"结果保存至: {args.output}")
    
    close_neo4j_driver()


if __name__ == "__main__":
    main()
