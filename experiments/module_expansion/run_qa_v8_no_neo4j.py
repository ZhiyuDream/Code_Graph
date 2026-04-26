#!/usr/bin/env python3
"""
V8 无Neo4j扩展版本
只保留初始搜索（semantic + grep + file_expansion），禁用ReAct Neo4j扩展

用法:
    python run_qa_v8_no_neo4j.py --csv results/qav2_test.csv --output results/v8_no_neo4j.json --workers 20
"""
from __future__ import annotations

import sys
import json
import csv
import argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))

from tools.core import (
    get_neo4j_driver, close_neo4j_driver,
    call_llm, call_llm_json, generate_answer
)
from tools.search import (
    search_functions_by_text,
    search_issues, extract_entities_from_question, grep_codebase,
    convert_grep_to_function_results, search_module_functions,
)
from tools.search.semantic_search import _load_rag_index

MAX_STEPS = 1  # 只运行1步（初始搜索），不进入ReAct循环
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

def initial_search(driver, client, question: str, enable_file_expansion: bool = False) -> dict:
    """初始检索：语义搜索 + Grep Fallback + Issue搜索 + 可选文件扩展"""
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
    
    file_expansion_count = 0
    if enable_file_expansion and funcs:
        original_count = len(funcs)
        funcs = expand_by_file_level(funcs, max_total=FILE_EXPANSION_MAX)
        file_expansion_count = len(funcs) - original_count
    
    return {
        "functions": funcs,
        "issues": issues,
        "steps": [{
            "step": 1,
            "action": "initial_search",
            "found": len(funcs),
            "fallback_triggered": fallback_triggered,
            "file_expansion": file_expansion_count
        }],
        "call_chains": [],
        "tool_calls": [],
        "fallback_triggered": fallback_triggered,
        "file_expansion_count": file_expansion_count
    }

def process_single(driver, client, row: dict, idx: int, enable_file_expansion: bool = False) -> dict:
    import time
    
    question = row.get('具体问题', '')
    
    try:
        collected = initial_search(driver, client, question, enable_file_expansion)
        answer = generate_answer(question, collected)
        
        return {
            "index": idx,
            "具体问题": question,
            "参考答案": row.get('答案', ''),
            "生成答案": answer,
            "路由类型": "V8_NoNeo4j",
            "检索结果": {
                "function_count": len(collected.get("functions", [])),
                "issue_count": len(collected.get("issues", [])),
                "step_count": 1,
                "file_expansion_count": collected.get("file_expansion_count", 0)
            },
            "延迟_s": 0
        }
    except Exception as e:
        return {
            "index": idx,
            "具体问题": question,
            "生成答案": f"处理失败: {str(e)}",
            "路由类型": "V8_NoNeo4j",
            "错误": str(e),
            "延迟_s": 0
        }

def main():
    parser = argparse.ArgumentParser(description="V8 无Neo4j扩展版本")
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=20)
    parser.add_argument("--file-expansion", action="store_true")
    args = parser.parse_args()
    
    rows = []
    with open(args.csv, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    
    print(f"共 {len(rows)} 题")
    print(f"模式: 无Neo4j扩展 + {'文件扩展' if args.file_expansion else '无扩展'}")
    
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
    close_neo4j_driver()

if __name__ == "__main__":
    main()
