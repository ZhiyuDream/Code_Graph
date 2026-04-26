#!/usr/bin/env python3
"""
快速AB测试：对比基础检索 vs 模块扩展检索的召回质量
"""

import sys
sys.path.insert(0, '/data/yulin/RUC/Code_Graph')

import json
import networkx as nx
from collections import defaultdict
from tools.core import get_neo4j_driver, run_cypher
from tools.search import search_functions_by_text

print("="*70)
print("AB测试：基础检索 vs 模块扩展检索")
print("="*70)

# 加载之前错误的问题
with open('wrong_questions.json') as f:
    test_questions = json.load(f)[:5]  # 只测5道

print(f"\n测试问题数: {len(test_questions)}")

# 构建调用图
print("\n[1/2] 构建调用图...")
driver = get_neo4j_driver()

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

G = nx.DiGraph()
for f in functions:
    G.add_node(f['id'], name=f['name'], file=f['file'])

for c in calls:
    caller_id, callee_id = c['caller'], c['callee']
    if caller_id in G.nodes and callee_id in G.nodes:
        caller_file = G.nodes[caller_id].get('file', '')
        callee_file = G.nodes[callee_id].get('file', '')
        weight = 1.0 if caller_file != callee_file else 0.3
        G.add_edge(caller_id, callee_id, weight=weight)

# 社区发现
G_undirected = G.to_undirected()
communities = nx.community.louvain_communities(G_undirected, weight='weight', seed=42)

node_to_module = {}
for module_id, community in enumerate(communities):
    for node in community:
        node_to_module[node] = module_id

print(f"  图构建完成: {len(functions)} 函数, {len(communities)} 模块")

# 对比测试
print("\n[2/2] 对比召回效果...")
print("\n" + "="*70)

results = []

for i, item in enumerate(test_questions):
    idx = item['index']
    question = item['question']
    reference = item['reference']
    
    print(f"\n[{i+1}] 问题 {idx}: {question[:50]}...")
    
    # 从参考答案中提取关键函数名（用于评估召回）
    import re
    ref_funcs = set(re.findall(r'\b(ggml_[a-z_]+|llama_[a-z_]+)\b', reference.lower()))
    
    # 基础检索
    baseline_funcs = search_functions_by_text(question, top_k=15)
    baseline_names = {f['name'].lower() for f in baseline_funcs}
    
    # 模块扩展
    modules_hit = set()
    for f in baseline_funcs:
        func_name = f.get('name', '')
        for node_id, node_data in G.nodes(data=True):
            if node_data.get('name') == func_name:
                if node_id in node_to_module:
                    modules_hit.add(node_to_module[node_id])
                break
    
    expanded_names = set(baseline_names)
    for node_id, module_id in node_to_module.items():
        if module_id in modules_hit:
            node_name = G.nodes[node_id].get('name', '').lower()
            if node_name:
                expanded_names.add(node_name)
        if len(expanded_names) >= 50:  # 限制
            break
    
    # 计算召回率（相对于参考答案中的函数）
    if ref_funcs:
        baseline_hit = len(ref_funcs & baseline_names)
        expanded_hit = len(ref_funcs & expanded_names)
        baseline_recall = baseline_hit / len(ref_funcs) * 100
        expanded_recall = expanded_hit / len(ref_funcs) * 100
        
        print(f"    参考答案函数: {ref_funcs}")
        print(f"    基础召回: {baseline_hit}/{len(ref_funcs)} ({baseline_recall:.0f}%) - {baseline_names & ref_funcs}")
        print(f"    模块扩展: {expanded_hit}/{len(ref_funcs)} ({expanded_recall:.0f}%) - {expanded_names & ref_funcs}")
        
        if expanded_recall > baseline_recall:
            print(f"    ✅ 提升: +{expanded_recall-baseline_recall:.0f}%")
        elif expanded_recall < baseline_recall:
            print(f"    ⚠️ 下降: {expanded_recall-baseline_recall:.0f}%")
        else:
            print(f"    = 持平")
        
        results.append({
            'index': idx,
            'baseline_recall': baseline_recall,
            'expanded_recall': expanded_recall,
            'improvement': expanded_recall - baseline_recall
        })

# 总结
print("\n" + "="*70)
print("总结")
print("="*70)

if results:
    avg_improvement = sum(r['improvement'] for r in results) / len(results)
    improved = sum(1 for r in results if r['improvement'] > 0)
    unchanged = sum(1 for r in results if r['improvement'] == 0)
    degraded = sum(1 for r in results if r['improvement'] < 0)
    
    print(f"\n平均召回提升: {avg_improvement:.1f}%")
    print(f"提升: {improved} 题 | 持平: {unchanged} 题 | 下降: {degraded} 题")
    
    # 详细数据
    print(f"\n详细:")
    for r in results:
        print(f"  Q{r['index']}: {r['baseline_recall']:.0f}% → {r['expanded_recall']:.0f}% ({r['improvement']:+.0f}%)")

print("\n" + "="*70)

