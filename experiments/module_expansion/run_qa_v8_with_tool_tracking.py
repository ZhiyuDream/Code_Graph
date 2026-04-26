#!/usr/bin/env python3
"""
V8 + 文件级扩展 + 详细工具调用记录
基于 run_qa_v8_with_file_expansion.py 添加工具调用追踪
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
    convert_grep_to_function_results, search_module_functions
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


def expand_by_file_level(initial_funcs: list, max_total: int = FILE_EXPANSION_MAX) -> tuple:
    """
    文件级扩展：从初始召回的函数中，扩展到同文件的其他函数
    返回: (扩展后的函数列表, 新增函数数量)
    """
    rag_index = get_rag_index()
    if not rag_index:
        return initial_funcs, 0
    
    files_hit = set()
    initial_ids = set()
    
    for fn in initial_funcs:
        file_path = fn.get('file', '')
        if file_path:
            files_hit.add(file_path)
        initial_ids.add(f"{fn.get('name', '')}:{file_path}")
    
    if not files_hit:
        return initial_funcs, 0
    
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
    
    return expanded_funcs, added_count


def initial_search(driver, client, question: str, enable_file_expansion: bool = False) -> tuple:
    """
    初始检索：语义搜索 + Grep Fallback + Issue搜索 + 可选文件扩展
    返回: (collected_dict, tool_calls_list)
    """
    tool_calls = []
    
    # 1. 语义搜索
    funcs = search_functions_by_text(question, top_k=5)
    tool_calls.append({
        "step": 1,
        "tool": "semantic_search",
        "params": {"top_k": 5},
        "results": len(funcs),
        "new_functions": [f['name'] for f in funcs[:3]]
    })
    
    # 检查是否需要Grep Fallback
    max_score = max([f.get('score', 0) for f in funcs], default=0)
    fallback_triggered = False
    
    if max_score < FALLBACK_THRESHOLD:
        fallback_triggered = True
        entities = extract_entities_from_question(question)
        
        for entity in entities[:2]:
            # 模块搜索
            if '-' in entity or entity.islower():
                module_funcs = search_module_functions(entity, limit=5)
                if module_funcs:
                    tool_calls.append({
                        "step": 1,
                        "tool": "module_search",
                        "params": {"entity": entity},
                        "results": len(module_funcs),
                        "new_functions": [f['name'] for f in module_funcs[:2]]
                    })
                    for fn in module_funcs:
                        if not any(f['name'] == fn['name'] for f in funcs):
                            funcs.append(fn)
            
            # Grep搜索
            grep_results = grep_codebase(entity, limit=3)
            if grep_results:
                new_funcs = convert_grep_to_function_results(grep_results)
                tool_calls.append({
                    "step": 1,
                    "tool": "grep_fallback",
                    "params": {"entity": entity},
                    "results": len(new_funcs),
                    "new_functions": [f['name'] for f in new_funcs[:2]]
                })
                for fn in new_funcs:
                    if not any(f['name'] == fn['name'] for f in funcs):
                        funcs.append(fn)
    
    # 2. Issue搜索
    issues = search_issues(question, top_k=3)
    tool_calls.append({
        "step": 1,
        "tool": "issue_search",
        "params": {"top_k": 3},
        "results": len(issues)
    })
    
    # 3. 文件级扩展
    file_expansion_count = 0
    if enable_file_expansion and funcs:
        original_count = len(funcs)
        funcs, added = expand_by_file_level(funcs, max_total=FILE_EXPANSION_MAX)
        file_expansion_count = added
        tool_calls.append({
            "step": 1,
            "tool": "file_expansion",
            "params": {"max_total": FILE_EXPANSION_MAX},
            "results": added,
            "new_functions": [f['name'] for f in funcs[original_count:original_count+3]]
        })
    
    steps = [{
        "step": 1,
        "action": "initial_search",
        "found": len(funcs),
        "fallback_triggered": fallback_triggered,
        "file_expansion": file_expansion_count
    }]
    
    collected = {
        "functions": funcs,
        "issues": issues,
        "steps": steps,
        "call_chains": [],
        "fallback_triggered": fallback_triggered,
        "file_expansion_count": file_expansion_count
    }
    
    return collected, tool_calls


def react_search(driver, client, question: str, collected: dict, enable_file_expansion: bool = False) -> tuple:
    """
    ReAct迭代检索主流程
    返回: (collected_dict, tool_calls_list)
    """
    tool_calls = []
    info_gain_history = []
    
    for step in range(2, MAX_STEPS + 1):
        # ReAct决策（这里call_llm_json是工具调用）
        funcs = collected.get("functions", [])
        chains = collected.get("call_chains", [])
        expanded = [c['from'] for c in chains]
        
        # 构建上下文
        context_lines = [f"问题: {question}"]
        context_lines.append(f"\n【已收集函数】(共{len(funcs)}个):")
        for i, f in enumerate(funcs[:5]):
            source = f.get('source', 'embedding')
            score = f.get('score', 0)
            marker = " [已扩展]" if f['name'] in expanded else ""
            context_lines.append(f"{i+1}. {f['name']} ({source}, {score:.3f}){marker}")
        
        context = '\n'.join(context_lines)
        
        prompt = f"""{context}

你是代码检索专家。请根据当前已收集的信息和问题类型，选择最合适的下一步行动。

【可用工具】
1. expand_callers - 扩展某个函数的调用者
2. expand_callees - 扩展某个函数的被调用者  
3. sufficient - 信息充足，可以生成答案

返回JSON: {{"thought": "...", "sufficient": false, "action": "...", "target": "..."}}"""
        
        # 记录LLM决策调用
        tool_calls.append({
            "step": step,
            "tool": "llm_react_decide",
            "params": {"context_functions": len(funcs)},
            "results": 1
        })
        
        result = call_llm_json(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=150
        )
        
        if result is None or result.get("sufficient") or step >= 4:
            break
        
        action = result.get("action", "")
        target = result.get("target", "")
        
        if action in ["expand_callers", "expand_callees"] and target:
            direction = "callers" if action == "expand_callers" else "callees"
            
            # 记录Neo4j扩展调用
            chain = expand_call_chain(target, direction)
            
            new_count = 0
            new_func_names = []
            for fn in chain["functions"]:
                if not any(f['name'] == fn['name'] for f in collected["functions"]):
                    fn['score'] = 0.5
                    fn['source'] = f'{direction}_of_{target}'
                    collected["functions"].append(fn)
                    new_count += 1
                    new_func_names.append(fn.get('name', ''))
            
            tool_calls.append({
                "step": step,
                "tool": f"neo4j_{direction}",
                "params": {"function": target},
                "results": len(chain["functions"]),
                "new_functions": new_func_names[:3],
                "new_count": new_count
            })
            
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
            
            # 递减回报检测
            if step >= 3 and len(info_gain_history) >= 2:
                if all(g <= 1 for g in info_gain_history[-2:]):
                    break
    
    return collected, tool_calls


def process_single(driver, client, row: dict, idx: int, enable_file_expansion: bool = False) -> dict:
    """处理单个问题"""
    print(f"[{idx}] {row.get('具体问题', 'N/A')[:50]}...")
    
    import time
    start_time = time.time()
    
    question = row.get('具体问题', '')
    
    try:
        # 初始检索
        collected, initial_tools = initial_search(driver, client, question, enable_file_expansion)
        
        # ReAct迭代
        collected, react_tools = react_search(driver, client, question, collected, enable_file_expansion)
        
        # 合并工具调用记录
        all_tool_calls = initial_tools + react_tools
        
        # 生成答案
        answer = generate_answer(question, collected)
        
        latency = time.time() - start_time
        
        # 统计工具调用
        tool_stats = {}
        for call in all_tool_calls:
            tool_name = call.get('tool', 'unknown')
            tool_stats[tool_name] = tool_stats.get(tool_name, 0) + 1
        
        return {
            "index": idx,
            "具体问题": question,
            "参考答案": row.get('答案', ''),
            "生成答案": answer,
            "路由类型": "V8_FileExpansion" if enable_file_expansion else "V8_Baseline",
            "检索结果": {
                "function_count": len(collected.get("functions", [])),
                "issue_count": len(collected.get("issues", [])),
                "step_count": len(collected.get("steps", [])),
                "file_expansion_count": collected.get("file_expansion_count", 0),
                "tool_calls": all_tool_calls,
                "tool_stats": tool_stats
            },
            "延迟_s": latency
        }
        
    except Exception as e:
        return {
            "index": idx,
            "具体问题": question,
            "生成答案": f"处理失败: {str(e)}",
            "路由类型": "V8_FileExpansion" if enable_file_expansion else "V8_Baseline",
            "错误": str(e),
            "延迟_s": time.time() - start_time
        }


def main():
    parser = argparse.ArgumentParser(description="V8 + 工具调用追踪")
    parser.add_argument("--csv", type=Path, required=True, help="输入CSV文件")
    parser.add_argument("--output", type=Path, required=True, help="输出JSON文件")
    parser.add_argument("--workers", type=int, default=20, help="并行数")
    parser.add_argument("--file-expansion", action="store_true", help="启用文件级扩展")
    args = parser.parse_args()
    
    # 读取CSV
    rows = []
    with open(args.csv, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    
    print(f"共 {len(rows)} 题需要处理")
    print(f"模式: {'文件扩展' if args.file_expansion else 'Baseline'}")
    print(f"并行: {args.workers} workers")
    
    # 连接Neo4j
    driver = get_neo4j_driver()
    
    # 从core模块导入client
    from tools.core.llm_client import get_llm_client
    client = get_llm_client()
    
    # 并行处理
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
    
    # 最终保存
    sorted_results = sorted(results, key=lambda x: x.get('index', 0))
    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(sorted_results, f, ensure_ascii=False, indent=2)
    
    print(f"\n完成！共处理 {len(results)}/{len(rows)} 题")
    print(f"结果保存至: {args.output}")
    
    # 关闭连接
    close_neo4j_driver()


if __name__ == "__main__":
    main()
