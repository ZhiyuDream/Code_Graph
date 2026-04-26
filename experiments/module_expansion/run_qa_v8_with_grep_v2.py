#!/usr/bin/env python3
"""
V8 + Grep V2 (Claude Code 风格) 实验脚本

与 baseline 的唯一区别：grep_codebase 替换为 V2 实现
- --json 解析替代正则解析
- --max-columns 截断
- mtime 排序
- -Fw 标识符搜索
- 更多排除规则

用法:
    python run_qa_v8_with_grep_v2.py --csv results/qav2_test.csv --output results/v8_grep_v2.json --workers 20 --file-expansion
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
    search_issues, extract_entities_from_question,
    convert_grep_to_function_results, search_module_functions,
    semantic_search as semantic_search_func
)
# 使用 Grep V2（Claude Code 风格）
from tools.search.grep_search_v2 import grep_codebase
from tools.search.semantic_search import _load_rag_index

# 常量
MAX_STEPS = 5
FALLBACK_THRESHOLD = 0.5
FILE_EXPANSION_MAX = 50  # 文件扩展最大函数数

# 全局 RAG index 缓存（多进程共享）
_rag_index_cache = None


def get_rag_index():
    """获取全局 RAG index"""
    global _rag_index_cache
    if _rag_index_cache is None:
        _rag_index_cache = _load_rag_index()
    return _rag_index_cache


def expand_by_file_level(initial_funcs: list, max_total: int = FILE_EXPANSION_MAX) -> list:
    """
    文件级扩展：从初始召回的函数中，扩展到同文件的其他函数
    
    Args:
        initial_funcs: 初始召回的函数列表
        max_total: 最大返回数量
    
    Returns:
        扩展后的函数列表
    """
    rag_index = get_rag_index()
    if not rag_index:
        return initial_funcs
    
    # 获取已召回函数的文件
    files_hit = set()
    initial_ids = set()
    
    for fn in initial_funcs:
        file_path = fn.get('file', '')
        if file_path:
            files_hit.add(file_path)
        initial_ids.add(f"{fn.get('name', '')}:{file_path}")
    
    if not files_hit:
        return initial_funcs
    
    # 从 RAG index 中找到同文件的其他函数
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
        
        # 检查是否已存在
        func_id = f"{func_name}:{file_path}"
        if func_id in initial_ids:
            continue
            
        # 如果该文件在已召回列表中，添加这个函数
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
    # 语义搜索
    funcs = search_functions_by_text(question, top_k=5)
    
    # 检查是否需要Grep Fallback
    max_score = max([f.get('score', 0) for f in funcs], default=0)
    fallback_triggered = False
    
    if max_score < FALLBACK_THRESHOLD:
        fallback_triggered = True
        print(f"      Embedding最高相似度{max_score:.2f} < {FALLBACK_THRESHOLD}，触发主动搜索...")
        
        entities = extract_entities_from_question(question)
        print(f"      提取实体: {entities}")
        
        for entity in entities[:2]:
            # 尝试模块搜索
            if '-' in entity or entity.islower():
                print(f"      模块搜索: {entity}...")
                module_funcs = search_module_functions(entity, limit=5)
                if module_funcs:
                    print(f"        找到 {len(module_funcs)} 个模块函数")
                    for fn in module_funcs:
                        if not any(f['name'] == fn['name'] for f in funcs):
                            funcs.append(fn)
            
            # Grep搜索
            print(f"      Grep搜索: {entity}...")
            grep_results = grep_codebase(entity, limit=3)
            if grep_results:
                print(f"        找到 {len(grep_results)} 个相关函数")
                new_funcs = convert_grep_to_function_results(grep_results)
                for fn in new_funcs:
                    if not any(f['name'] == fn['name'] for f in funcs):
                        funcs.append(fn)
    
    # Issue搜索
    issues = search_issues(question, top_k=3)
    
    # 文件级扩展（如果启用）
    file_expansion_count = 0
    if enable_file_expansion and funcs:
        original_count = len(funcs)
        funcs = expand_by_file_level(funcs, max_total=FILE_EXPANSION_MAX)
        file_expansion_count = len(funcs) - original_count
        print(f"      文件级扩展: {original_count} → {len(funcs)} (+{file_expansion_count})")
    
    # 记录初始步骤
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


def react_decide(client, question: str, collected: dict, step: int) -> dict:
    """ReAct决策：选择下一步行动"""
    funcs = collected.get("functions", [])
    chains = collected.get("call_chains", [])
    issues = collected.get("issues", [])
    
    # 统计变量
    if not hasattr(react_decide, 'stats'):
        react_decide.stats = {"total": 0, "default_used": 0, "actions": {}}
    
    # 构建当前上下文
    context_lines = [f"问题: {question}"]
    context_lines.append(f"\n【已收集函数】(共{len(funcs)}个，按相似度排序):")
    
    # 已扩展的函数
    expanded = [c['from'] for c in chains]
    for i, f in enumerate(funcs[:5]):
        source = f.get('source', 'embedding') if f.get('source') else 'embedding'
        score = f.get('score', 0)
        marker = " [已扩展]" if f['name'] in expanded else ""
        context_lines.append(f"{i+1}. {f['name']} ({source}, {score:.3f}){marker}")
    
    if issues:
        context_lines.append(f"\n【相关Issue】(共{len(issues)}个):")
        for i, issue in enumerate(issues[:2]):
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
    
    result = call_llm_json(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=150
    )
    
    if result is None:
        return {"sufficient": True, "action": "sufficient", "target": ""}
    
    # 强制停止条件
    if step >= 4:
        return {"sufficient": True, "action": "sufficient", "target": ""}
    
    if result.get("sufficient") or result.get("action") == "sufficient":
        return {"sufficient": True, "action": "sufficient", "target": ""}
    
    action = result.get("action", "")
    target = result.get("target", "")
    
    # 验证target
    valid = [f['name'] for f in funcs[:8]]
    if target not in valid:
        for f in funcs[:5]:
            if f['name'] not in expanded:
                target = f['name']
                break
        else:
            target = funcs[0]['name'] if funcs else ""
    
    # 统计
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


def react_search(driver, client, question: str, enable_file_expansion: bool = False) -> dict:
    """ReAct迭代检索主流程"""
    # 初始检索（包含Grep Fallback和文件扩展）
    collected = initial_search(driver, client, question, enable_file_expansion)
    
    info_gain_history = []
    
    # ReAct迭代
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
            
            # 递减回报检测
            if step >= 3 and len(info_gain_history) >= 2:
                if all(g <= 1 for g in info_gain_history[-2:]):
                    break
    
    return collected


def process_single(driver, client, row: dict, idx: int, enable_file_expansion: bool = False) -> dict:
    """处理单个问题"""
    print(f"[{idx}] {row.get('具体问题', 'N/A')[:50]}...")
    
    import time
    start_time = time.time()
    
    question = row.get('具体问题', '')
    
    try:
        # ReAct检索（带文件扩展选项）
        collected = react_search(driver, client, question, enable_file_expansion)
        
        # 生成答案
        answer = generate_answer(question, collected)
        
        latency = time.time() - start_time
        
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
                "file_expansion_count": collected.get("file_expansion_count", 0)
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
    parser = argparse.ArgumentParser(description="V8 + 文件级扩展对比测试")
    parser.add_argument("--csv", type=Path, required=True, help="输入CSV文件")
    parser.add_argument("--output", type=Path, required=True, help="输出JSON文件")
    parser.add_argument("--workers", type=int, default=20, help="并行数 (默认20)")
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
