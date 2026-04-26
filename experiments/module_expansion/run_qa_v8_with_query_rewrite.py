#!/usr/bin/env python3
"""
V8 + Query改写 + 文件级扩展
测试Query改写对Grep搜索的增强效果

用法:
    # 启用Query改写
    python run_qa_v8_with_query_rewrite.py --csv results/qav2_test.csv --output results/v8_rewrite.json --workers 20 --rewrite
    
    # 对比Baseline
    python run_qa_v8_with_query_rewrite.py --csv results/qav2_test.csv --output results/v8_baseline.json --workers 20
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
    get_neo4j_driver, close_neo4j_driver,
    call_llm, call_llm_json, generate_answer
)
from tools.search import (
    search_functions_by_text, expand_call_chain,
    search_issues, grep_codebase,
    convert_grep_to_function_results, search_module_functions,
)
from tools.search.semantic_search import _load_rag_index
from tools.search.query_rewriter import get_grep_keywords, rewrite_query

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


def initial_search_with_rewrite(
    driver, 
    client, 
    question: str, 
    enable_file_expansion: bool = False,
    enable_rewrite: bool = False
) -> dict:
    """
    初始检索：语义搜索 + Query改写Grep + Issue搜索 + 可选文件扩展
    
    Args:
        driver: Neo4j驱动
        client: LLM客户端
        question: 问题
        enable_file_expansion: 是否启用文件扩展
        enable_rewrite: 是否启用Query改写
    """
    print(f"    [Search] 问题: {question[:60]}...")
    
    # 1. 语义搜索（始终执行）
    funcs = search_functions_by_text(question, top_k=5)
    print(f"    [Search] 语义搜索召回: {len(funcs)}个")
    
    # 2. Query改写 + Grep搜索（如果启用）
    rewrite_info = {"enabled": enable_rewrite, "keywords": [], "grep_results": 0}
    
    if enable_rewrite:
        # Query改写
        rewritten = rewrite_query(question, use_llm=False)
        keywords = rewritten.keywords
        identifiers = rewritten.identifiers
        
        rewrite_info["keywords"] = keywords[:10]
        rewrite_info["identifiers"] = identifiers[:5]
        
        print(f"    [Rewrite] 提取标识符: {identifiers[:5]}")
        print(f"    [Rewrite] 生成关键词: {keywords[:8]}")
        
        # 使用改写后的关键词进行Grep搜索
        grep_funcs = []
        
        # 优先搜索标识符（精确匹配）
        for ident in identifiers[:3]:
            results = grep_codebase(ident, limit=3)
            if results:
                new_funcs = convert_grep_to_function_results(results)
                for fn in new_funcs:
                    if not any(f['name'] == fn['name'] for f in funcs):
                        fn['source'] = 'grep_rewrite_ident'
                        fn['rewrite_keyword'] = ident
                        grep_funcs.append(fn)
        
        # 搜索翻译的关键词
        for keyword in keywords[:5]:
            if len(grep_funcs) >= 10:  # 限制Grep结果数量
                break
            results = grep_codebase(keyword, limit=3)
            if results:
                new_funcs = convert_grep_to_function_results(results)
                for fn in new_funcs:
                    if not any(f['name'] == fn['name'] for f in funcs + grep_funcs):
                        fn['source'] = 'grep_rewrite_keyword'
                        fn['rewrite_keyword'] = keyword
                        grep_funcs.append(fn)
        
        funcs.extend(grep_funcs)
        rewrite_info["grep_results"] = len(grep_funcs)
        print(f"    [Rewrite] Grep搜索召回: {len(grep_funcs)}个")
    
    # 3. 检查是否需要Fallback（仅在未启用改写或改写效果不佳时）
    max_score = max([f.get('score', 0) for f in funcs], default=0)
    fallback_triggered = False
    
    if max_score < FALLBACK_THRESHOLD and not enable_rewrite:
        # 只有未启用改写时才使用传统Fallback
        fallback_triggered = True
        print(f"    [Fallback] 触发传统Fallback (max_score={max_score:.2f})")
        
        # 传统实体提取
        from tools.search import extract_entities_from_question
        entities = extract_entities_from_question(question)
        
        for entity in entities[:2]:
            # 模块搜索
            if '-' in entity or entity.islower():
                module_funcs = search_module_functions(entity, limit=5)
                for fn in module_funcs:
                    if not any(f['name'] == fn['name'] for f in funcs):
                        funcs.append(fn)
            
            # Grep搜索
            grep_results = grep_codebase(entity, limit=3)
            if grep_results:
                new_funcs = convert_grep_to_function_results(grep_results)
                for fn in new_funcs:
                    if not any(f['name'] == fn['name'] for f in funcs):
                        funcs.append(fn)
    
    # 4. Issue搜索
    issues = search_issues(question, top_k=3)
    
    # 5. 文件级扩展
    file_expansion_count = 0
    if enable_file_expansion and funcs:
        original_count = len(funcs)
        funcs = expand_by_file_level(funcs, max_total=FILE_EXPANSION_MAX)
        file_expansion_count = len(funcs) - original_count
        print(f"    [Expansion] 文件级扩展: {original_count} → {len(funcs)} (+{file_expansion_count})")
    
    return {
        "functions": funcs,
        "issues": issues,
        "steps": [{
            "step": 1,
            "action": "initial_search",
            "found": len(funcs),
            "fallback_triggered": fallback_triggered,
            "file_expansion": file_expansion_count,
            "rewrite_info": rewrite_info
        }],
        "call_chains": [],
        "tool_calls": [],
        "fallback_triggered": fallback_triggered,
        "file_expansion_count": file_expansion_count
    }


def react_decide(client, question: str, collected: dict, step: int) -> dict:
    """ReAct决策"""
    funcs = collected.get("functions", [])
    chains = collected.get("call_chains", [])
    
    # 构建上下文
    context_lines = [f"问题: {question}"]
    context_lines.append(f"\n【已收集函数】(共{len(funcs)}个):")
    
    expanded = [c['from'] for c in chains]
    for i, f in enumerate(funcs[:5]):
        source = f.get('source', 'embedding')
        score = f.get('score', 0)
        marker = " [已扩展]" if f['name'] in expanded else ""
        context_lines.append(f"{i+1}. {f['name']} ({source}, {score:.3f}){marker}")
    
    prompt = f"""{chr(10).join(context_lines)}

---

你是代码检索专家。根据当前信息选择下一步行动。

【可用工具】
1. expand_callers - 扩展调用者（谁调用了它）
2. expand_callees - 扩展被调用者（它调用了谁）
3. sufficient - 信息充足，可以生成答案

返回JSON: {{"thought": "分析", "sufficient": false, "action": "expand_callers|expand_callees|sufficient", "target": "目标函数名"}}

只输出JSON:"""
    
    try:
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
        
        # 验证target
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
    except Exception as e:
        return {"sufficient": True, "action": "sufficient", "target": ""}


def react_search(
    driver, 
    client, 
    question: str, 
    enable_file_expansion: bool = False,
    enable_rewrite: bool = False
) -> dict:
    """ReAct迭代检索主流程"""
    collected = initial_search_with_rewrite(
        driver, client, question, 
        enable_file_expansion, 
        enable_rewrite
    )
    
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


def process_single(
    driver, 
    client, 
    row: dict, 
    idx: int, 
    enable_file_expansion: bool = False,
    enable_rewrite: bool = False
) -> dict:
    """处理单个问题"""
    import time
    
    print(f"[{idx}] {row.get('具体问题', 'N/A')[:50]}...")
    
    start_time = time.time()
    question = row.get('具体问题', '')
    
    try:
        collected = react_search(
            driver, client, question, 
            enable_file_expansion, 
            enable_rewrite
        )
        
        answer = generate_answer(question, collected)
        latency = time.time() - start_time
        
        return {
            "index": idx,
            "具体问题": question,
            "参考答案": row.get('答案', ''),
            "生成答案": answer,
            "路由类型": "V8_QueryRewrite" if enable_rewrite else "V8_Baseline",
            "检索结果": {
                "function_count": len(collected.get("functions", [])),
                "issue_count": len(collected.get("issues", [])),
                "step_count": len(collected.get("steps", [])),
                "file_expansion_count": collected.get("file_expansion_count", 0),
                "rewrite_info": collected.get("steps", [{}])[0].get("rewrite_info", {})
            },
            "延迟_s": latency
        }
    
    except Exception as e:
        import traceback
        return {
            "index": idx,
            "具体问题": question,
            "生成答案": f"处理失败: {str(e)}\n{traceback.format_exc()}",
            "路由类型": "V8_QueryRewrite" if enable_rewrite else "V8_Baseline",
            "错误": str(e),
            "延迟_s": time.time() - start_time
        }


def main():
    parser = argparse.ArgumentParser(description="V8 + Query改写 + 文件级扩展")
    parser.add_argument("--csv", type=Path, required=True, help="输入CSV文件")
    parser.add_argument("--output", type=Path, required=True, help="输出JSON文件")
    parser.add_argument("--workers", type=int, default=20, help="并行数")
    parser.add_argument("--file-expansion", action="store_true", help="启用文件级扩展")
    parser.add_argument("--rewrite", action="store_true", help="启用Query改写")
    args = parser.parse_args()
    
    # 读取CSV
    rows = []
    with open(args.csv, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    
    print(f"共 {len(rows)} 题需要处理")
    print(f"Query改写: {'启用' if args.rewrite else '禁用'}")
    print(f"文件扩展: {'启用' if args.file_expansion else '禁用'}")
    print(f"并行: {args.workers} workers")
    
    # 连接Neo4j
    driver = get_neo4j_driver()
    
    from tools.core.llm_client import get_llm_client
    client = get_llm_client()
    
    # 并行处理
    results = []
    completed = 0
    
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                process_single, 
                driver, client, row, i, 
                args.file_expansion, 
                args.rewrite
            ): i
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
