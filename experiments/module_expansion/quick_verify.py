#!/usr/bin/env python3
"""
快速验证：模块感知检索 vs 基础检索
不改动主流程，仅离线对比召回效果
"""

import sys
sys.path.insert(0, '/data/yulin/RUC/Code_Graph')

import json
import networkx as nx
from collections import defaultdict
from tools.core import get_neo4j_driver, run_cypher
from tools.search import search_functions_by_text

print("="*60)
print("模块感知检索 - 快速验证")
print("="*60)

# 1. 从 Neo4j 导出调用图
print("\n[1/4] 导出调用图...")
driver = get_neo4j_driver()

# 获取所有函数和调用关系
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

print(f"  函数数: {len(functions)}")
print(f"  调用关系数: {len(calls)}")

# 2. 构建 NetworkX 图
print("\n[2/4] 构建图并运行社区发现...")
G = nx.DiGraph()

# 添加节点
for f in functions:
    G.add_node(f['id'], name=f['name'], file=f['file'])

# 添加边（带权重：跨文件调用权重更高）
for c in calls:
    caller_id = c['caller']
    callee_id = c['callee']
    
    # 获取文件信息判断权重
    caller_file = G.nodes[caller_id].get('file', '') if caller_id in G.nodes else ''
    callee_file = G.nodes[callee_id].get('file', '') if callee_id in G.nodes else ''
    
    weight = 1.0 if caller_file != callee_file else 0.3
    
    if caller_id in G.nodes and callee_id in G.nodes:
        G.add_edge(caller_id, callee_id, weight=weight)

# 转换为无向图做社区发现（Leiden需要undirected）
G_undirected = G.to_undirected()

# 使用 Louvain 算法（NetworkX内置，无需额外安装）
communities = nx.community.louvain_communities(G_undirected, weight='weight', seed=42)

print(f"  发现社区数: {len(communities)}")

# 为每个节点分配 module_id
node_to_module = {}
for module_id, community in enumerate(communities):
    for node in community:
        node_to_module[node] = module_id

# 计算每个模块的统计信息
module_stats = defaultdict(lambda: {'size': 0, 'files': set()})
for node_id, module_id in node_to_module.items():
    module_stats[module_id]['size'] += 1
    if node_id in G.nodes:
        module_stats[module_id]['files'].add(G.nodes[node_id].get('file', ''))

print(f"  模块大小分布: min={min(s['size'] for s in module_stats.values())}, "
      f"max={max(s['size'] for s in module_stats.values())}, "
      f"avg={sum(s['size'] for s in module_stats.values())//len(module_stats)}")

# 3. 选择测试问题
print("\n[3/4] 选择测试问题...")

test_questions = [
    # 跨模块问题
    ("这个 ggml-blas 主要包含哪些核心功能或组件，它们之间是如何组织和协作的？", "跨模块架构"),
    ("apertus 包含哪些子模块或子组件，它们之间的层级关系和职责划分是怎样的？", "模块层级"),
    ("commonpp 依赖了哪些外部资源或模块，这些依赖之间的调用关系和数据流是怎样的？", "依赖关系"),
    ("llama-grammar 的内部结构是怎样的，各个子部分分别承担什么功能？", "内部结构"),
    ("为什么在系统设计中选择当前的方式划分和实现 concat，这种设计决策背后的核心考虑是什么？", "设计决策"),
    
    # 调用链问题
    ("函数 ggml_backend_blas_mul_mat 的数据流是怎样的？", "数据流"),
    ("ggml_vec_dot 的调用链路包含哪些关键函数？", "调用链路"),
    
    # 具体实现问题
    ("什么是 hunyuan-dense，它在整个代码体系中扮演什么角色？", "模块角色"),
    ("falcon 依赖了哪些其他模块或命名空间？", "模块依赖"),
    ("bert 直接依赖和间接依赖的其他模块有哪些？", "间接依赖"),
]

print(f"  选择 {len(test_questions)} 个典型问题")

# 4. 对比召回效果
print("\n[4/4] 对比召回效果...")
print("\n" + "="*60)

results = []

for question, q_type in test_questions:
    print(f"\n问题 [{q_type}]: {question[:40]}...")
    
    # 基础检索（语义搜索）
    baseline_funcs = search_functions_by_text(question, top_k=10)
    baseline_ids = {f.get('name', '') for f in baseline_funcs}
    
    # 模块扩展检索
    # 找到基础召回函数所在的模块
    modules_hit = set()
    for f in baseline_funcs:
        func_id = f.get('name', '')  # 简化为用 name 作为 id
        # 在图中查找对应的节点
        for node_id, node_data in G.nodes(data=True):
            if node_data.get('name') == func_id:
                if node_id in node_to_module:
                    modules_hit.add(node_to_module[node_id])
                break
    
    # 扩展：加入这些模块的所有函数
    expanded_ids = set(baseline_ids)
    for node_id, module_id in node_to_module.items():
        if module_id in modules_hit:
            node_name = G.nodes[node_id].get('name', '')
            if node_name:
                expanded_ids.add(node_name)
    
    # 统计
    baseline_count = len(baseline_ids)
    expanded_count = len(expanded_ids)
    gain = expanded_count - baseline_count
    gain_pct = (gain / baseline_count * 100) if baseline_count > 0 else 0
    
    print(f"  基础召回: {baseline_count} 个函数")
    print(f"  模块扩展: {expanded_count} 个函数 (+{gain}, +{gain_pct:.0f}%)")
    print(f"  涉及模块: {len(modules_hit)} 个")
    
    results.append({
        'question': question[:50],
        'type': q_type,
        'baseline': baseline_count,
        'expanded': expanded_count,
        'gain': gain,
        'gain_pct': gain_pct,
        'modules': len(modules_hit)
    })

# 5. 总结
print("\n" + "="*60)
print("验证结果总结")
print("="*60)

total_baseline = sum(r['baseline'] for r in results)
total_expanded = sum(r['expanded'] for r in results)
avg_gain = sum(r['gain_pct'] for r in results) / len(results)

print(f"\n平均召回提升: {avg_gain:.1f}%")
print(f"总函数数: {total_baseline} → {total_expanded} (+{total_expanded-total_baseline})")

# 按问题类型分组
by_type = defaultdict(list)
for r in results:
    by_type[r['type']].append(r['gain_pct'])

print(f"\n按问题类型分类:")
for q_type, gains in sorted(by_type.items(), key=lambda x: -sum(x[1])/len(x[1])):
    avg = sum(gains) / len(gains)
    print(f"  {q_type}: 平均提升 {avg:.1f}%")

# 最有潜力的问题（提升最大）
print(f"\n提升最大的3个问题:")
top3 = sorted(results, key=lambda x: -x['gain_pct'])[:3]
for r in top3:
    print(f"  [{r['type']}] {r['question'][:30]}... +{r['gain_pct']:.0f}%")

print("\n" + "="*60)
print("验证完成！")
print("="*60)
print("\n建议:")
if avg_gain > 50:
    print("✅ 模块扩展效果显著，建议投入完整实现")
elif avg_gain > 20:
    print("✓ 有一定效果，建议优化后实现（如 utility 降权、rerank）")
else:
    print("⚠ 效果不明显，建议重新设计策略")

