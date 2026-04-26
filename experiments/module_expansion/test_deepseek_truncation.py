"""
验证 DeepSeek 是否因 max_tokens 截断导致准确率下降
对照实验：10道被判"不完整/截断"的题目，用 1500 vs 4000 tokens 分别测试
"""
from __future__ import annotations

import json
import sys
import os
import csv
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))

os.environ['LLM_MODEL'] = 'deepseek-v4-pro'

from tools.core import get_neo4j_driver, close_neo4j_driver, generate_answer
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
    return {
        "functions": funcs,
        "issues": issues,
        "steps": [{"step": 1, "action": "initial_search", "found": len(funcs)}],
        "call_chains": [],
        "tool_calls": [],
        "fallback_triggered": fallback_triggered,
        "file_expansion_count": file_expansion_count
    }

def react_search(driver, client, question: str) -> dict:
    collected = initial_search(driver, client, question)
    info_gain_history = []
    for step in range(2, MAX_STEPS + 1):
        # 简化的决策逻辑：直接 sufficient（不做扩展，节省API调用）
        break
    return collected

def process_single(driver, client, row: dict, idx: int, max_tokens: int) -> dict:
    question = row.get('具体问题', '')
    try:
        collected = react_search(driver, client, question)
        answer = generate_answer(question, collected, max_tokens=max_tokens, model='deepseek-v4-pro', provider='deepseek')
        return {
            "index": idx,
            "question": question,
            "max_tokens": max_tokens,
            "answer": answer,
            "answer_len": len(answer)
        }
    except Exception as e:
        import traceback
        return {
            "index": idx,
            "question": question,
            "max_tokens": max_tokens,
            "answer": f"处理失败: {str(e)}",
            "answer_len": 0
        }

# 加载测试数据
rows = []
with open('results/qav2_test_cleaned.csv', 'r', encoding='utf-8') as f:
    reader = csv.DictReader(f)
    for row in reader:
        rows.append(row)

indices = json.load(open('/tmp/deepseek_trunc_test_indices.json'))

# 找到对应行
idx_to_row = {int(r['index']): r for r in rows}

driver = get_neo4j_driver()
from tools.core.llm_client import get_llm_client
client = get_llm_client(provider='deepseek')

print("=" * 60)
print("DeepSeek 截断对照实验")
print("=" * 60)

results_1500 = []
results_4000 = []

for idx in indices:
    row = idx_to_row[idx]
    print(f"\n--- 题目 {idx} ---")
    
    r1500 = process_single(driver, client, row, idx, 1500)
    r4000 = process_single(driver, client, row, idx, 4000)
    
    results_1500.append(r1500)
    results_4000.append(r4000)
    
    print(f"  max_tokens=1500: {r1500['answer_len']} 字")
    print(f"  max_tokens=4000: {r4000['answer_len']} 字")
    print(f"  长度差: {r4000['answer_len'] - r1500['answer_len']:+.0f} 字")
    
    t1500 = r1500['answer'][-40:] if len(r1500['answer']) > 40 else r1500['answer']
    t4000 = r4000['answer'][-40:] if len(r4000['answer']) > 40 else r4000['answer']
    print(f"  1500末尾: ...{t1500}")
    print(f"  4000末尾: ...{t4000}")

close_neo4j_driver()

# 转换为评判输入格式
def to_eval_format(results):
    out = []
    for r in results:
        row = idx_to_row[r['index']]
        out.append({
            'index': r['index'],
            '具体问题': r['question'],
            '参考答案': row.get('答案', ''),
            '生成答案': r['answer']
        })
    return out

for label, results in [("1500", results_1500), ("4000", results_4000)]:
    eval_input = to_eval_format(results)
    eval_path = f'/tmp/deepseek_trunc_{label}_eval_input.json'
    json.dump(eval_input, open(eval_path, 'w'), ensure_ascii=False, indent=2)

print("\n" + "=" * 60)
print("生成完成，准备评判...")
print("=" * 60)
