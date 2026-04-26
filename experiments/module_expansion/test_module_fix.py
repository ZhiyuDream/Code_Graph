#!/usr/bin/env python3
"""
验证：模块扩展能否修复原本回答错误的问题
"""

import sys
sys.path.insert(0, '/data/yulin/RUC/Code_Graph')

import json
import networkx as nx
from collections import defaultdict
from tools.core import get_neo4j_driver, run_cypher, call_llm
from tools.search import search_functions_by_text
from tools.core.answer_generator import generate_answer

print("="*70)
print("模块扩展效果验证 - 修复错误问题")
print("="*70)

# 1. 加载之前错误的问题
with open('wrong_questions.json') as f:
    test_questions = json.load(f)

print(f"\n测试问题数: {len(test_questions)}")

# 2. 构建调用图和模块（复用之前的代码）
print("\n[1/3] 构建调用图和模块...")
driver = get_neo4j_driver()

# 获取函数和调用关系
functions = run_cypher("""
    MATCH (f:Function)
    RETURN f.name AS name, f.file_path AS file, f.id AS id
    LIMIT 5000
""")

calls = run_cypher("""
    MATCH (a:Function)-[:CALLS]->(b:Function)
    RETURN a.id AS caller, b.id AS callee
    LIMIT 10000
""")

# 构建图
G = nx.DiGraph()
for f in functions:
    G.add_node(f['id'], name=f['name'], file=f['file'])

for c in calls:
    caller_id = c['caller']
    callee_id = c['callee']
    caller_file = G.nodes[caller_id].get('file', '') if caller_id in G.nodes else ''
    callee_file = G.nodes[callee_id].get('file', '') if callee_id in G.nodes else ''
    weight = 1.0 if caller_file != callee_file else 0.3
    if caller_id in G.nodes and callee_id in G.nodes:
        G.add_edge(caller_id, callee_id, weight=weight)

# 社区发现
G_undirected = G.to_undirected()
communities = nx.community.louvain_communities(G_undirected, weight='weight', seed=42)

node_to_module = {}
for module_id, community in enumerate(communities):
    for node in community:
        node_to_module[node] = module_id

print(f"  函数数: {len(functions)}, 模块数: {len(communities)}")

# 3. 对比测试
print("\n[2/3] 运行对比测试（基础 vs 模块扩展）...")
print("\n" + "="*70)

results = []

for i, item in enumerate(test_questions[:10]):  # 先测10道
    idx = item['index']
    question = item['question']
    reference = item['reference']
    
    print(f"\n[{i+1}/10] 问题 {idx}: {question[:45]}...")
    
    # --- 方法 A: 基础检索 ---
    baseline_funcs = search_functions_by_text(question, top_k=10)
    baseline_answer = generate_answer(question, {
        "functions": baseline_funcs,
        "issues": [],
        "steps": []
    })
    
    # --- 方法 B: 模块扩展检索 ---
    # 找到基础召回函数所在的模块
    modules_hit = set()
    for f in baseline_funcs:
        func_name = f.get('name', '')
        for node_id, node_data in G.nodes(data=True):
            if node_data.get('name') == func_name:
                if node_id in node_to_module:
                    modules_hit.add(node_to_module[node_id])
                break
    
    # 扩展：加入这些模块的所有函数（限制数量）
    expanded_funcs = list(baseline_funcs)  # 复制基础召回
    added_names = {f['name'] for f in baseline_funcs}
    
    for node_id, module_id in node_to_module.items():
        if module_id in modules_hit:
            node_name = G.nodes[node_id].get('name', '')
            if node_name and node_name not in added_names:
                expanded_funcs.append({
                    'name': node_name,
                    'file': G.nodes[node_id].get('file', ''),
                    'text': '',  # 简化，不读取代码
                    'score': 0.5,
                    'source': 'module_expansion'
                })
                added_names.add(node_name)
        
        # 限制扩展数量，避免上下文过载
        if len(expanded_funcs) >= 30:
            break
    
    module_answer = generate_answer(question, {
        "functions": expanded_funcs,
        "issues": [],
        "steps": []
    })
    
    print(f"  基础召回: {len(baseline_funcs)} 函数 → 生成答案")
    print(f"  模块扩展: {len(expanded_funcs)} 函数 (+{len(expanded_funcs)-len(baseline_funcs)}) → 生成答案")
    
    # 保存结果用于人工/LLM评估
    results.append({
        'index': idx,
        'question': question,
        'reference': reference[:200],
        'baseline_answer': baseline_answer,
        'module_answer': module_answer,
        'baseline_count': len(baseline_funcs),
        'module_count': len(expanded_funcs)
    })

# 4. 保存结果
print("\n[3/3] 保存结果...")
with open('ab_test_results.json', 'w') as f:
    json.dump(results, f, indent=2, ensure_ascii=False)

print("\n" + "="*70)
print("对比结果已保存到: ab_test_results.json")
print("="*70)

print("\n接下来需要:")
print("1. 人工查看 ab_test_results.json 对比两种方法的答案质量")
print("2. 或使用 LLM 评估哪个答案更接近参考答案")
print("3. 统计模块扩展后正确率是否提升")

