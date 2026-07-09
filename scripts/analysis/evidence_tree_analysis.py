#!/usr/bin/env python3
"""
Evidence Tree / Steiner Tree 分析。

核心问题：连接一道题所有 gold functions 所需的最小子图长什么样？

方法：
1. 把 gold functions 当作 terminal nodes
2. 在 call graph 上计算它们之间的最短路径
3. 在 metric closure 上求 MST
4. 把 MST 的边展开成原图上的最短路径，得到 Evidence Tree
5. 统计 Evidence Tree 的大小、直径、Steiner nodes 数量
6. 检查 Top-1 Seed 是否落在 Evidence Tree 上或附近

这个分析验证：Repository QA 的答案是否由一条小而局部的证据链组成。
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict, deque
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from src.core.neo4j_client import run_cypher
from src.qa.retrievers.fast_embedding import FastEmbeddingRetriever
from scripts.analysis.eval_region_compression import (
    build_region,
    detect_modules,
    fetch_call_graph,
    fetch_functions_and_calls,
    load_benchmark,
    load_embedding_index,
    build_helpers,
    gold_evidence_to_function_ids,
    score_modules,
)


def build_undirected_graph(func_calls: list[tuple]):
    graph = defaultdict(set)
    for a, b in func_calls:
        graph[a].add(b)
        graph[b].add(a)
    return graph


def shortest_path(start: str, end: str, graph: dict, max_depth: int = 10):
    """BFS 返回 start 到 end 的最短路径（节点列表）。"""
    if start == end:
        return [start]

    visited = {start: None}
    queue = deque([start])

    while queue:
        node = queue.popleft()
        dist = 0
        tmp = node
        while visited[tmp] is not None:
            tmp = visited[tmp]
            dist += 1
        if dist >= max_depth:
            continue

        for neighbor in graph.get(node, []):
            if neighbor not in visited:
                visited[neighbor] = node
                queue.append(neighbor)
                if neighbor == end:
                    # reconstruct path
                    path = [end]
                    while path[-1] != start:
                        path.append(visited[path[-1]])
                    return path[::-1]

    return None


def minimum_spanning_tree_metric(gold_nodes: list[str], distances: dict):
    """
    在 metric closure 上求 MST（Prim 算法）。
    distances: dict[(u,v)] = distance
    返回 MST 的边列表 [(u, v, dist)]。
    """
    if len(gold_nodes) <= 1:
        return []

    n = len(gold_nodes)
    in_tree = {gold_nodes[0]}
    edges = []

    while len(in_tree) < n:
        best_edge = None
        best_dist = float("inf")
        for u in in_tree:
            for v in gold_nodes:
                if v in in_tree:
                    continue
                d = distances.get(tuple(sorted([u, v])), float("inf"))
                if d < best_dist:
                    best_dist = d
                    best_edge = (u, v, d)

        if best_edge is None:
            break

        u, v, d = best_edge
        in_tree.add(v)
        edges.append(best_edge)

    return edges


def build_evidence_tree(gold_fids: set, graph: dict, max_depth: int = 10):
    """
    构建连接所有 gold functions 的 Evidence Tree。
    返回 tree 的节点集合和边集合，以及每对 gold 之间的距离。
    """
    gold_list = list(gold_fids)

    if len(gold_list) == 1:
        return {gold_list[0]}, set(), {gold_list[0]: 0}, {}

    # 计算所有 gold 对之间的最短路径和距离
    distances = {}
    paths = {}

    for i in range(len(gold_list)):
        for j in range(i + 1, len(gold_list)):
            u, v = gold_list[i], gold_list[j]
            path = shortest_path(u, v, graph, max_depth)
            if path:
                paths[(u, v)] = path
                paths[(v, u)] = path[::-1]
                distances[tuple(sorted([u, v]))] = len(path) - 1
            else:
                distances[tuple(sorted([u, v]))] = float("inf")

    # 在 metric closure 上求 MST
    mst_edges = minimum_spanning_tree_metric(gold_list, distances)

    # 展开 MST 边为原图路径
    tree_nodes = set(gold_fids)
    tree_edges = set()

    for u, v, _ in mst_edges:
        path = paths.get((u, v))
        if path:
            tree_nodes.update(path)
            for k in range(len(path) - 1):
                edge = tuple(sorted([path[k], path[k + 1]]))
                tree_edges.add(edge)

    return tree_nodes, tree_edges, {n: 0 for n in gold_fids}, distances


def tree_diameter(tree_nodes: set, tree_edges: set, graph: dict):
    """计算 tree 的直径（最长最短路径）。"""
    if len(tree_nodes) <= 1:
        return 0

    # BFS from arbitrary node
    start = next(iter(tree_nodes))
    far_node, _ = bfs_farthest(start, tree_nodes, tree_edges)
    far_node2, diameter = bfs_farthest(far_node, tree_nodes, tree_edges)
    return diameter


def bfs_farthest(start: str, tree_nodes: set, tree_edges: set):
    """BFS 找最远距离的节点和距离。"""
    visited = {start: 0}
    queue = deque([start])
    far_node = start
    far_dist = 0

    while queue:
        node = queue.popleft()
        dist = visited[node]

        for neighbor in graph_local.get(node, []):
            if neighbor in tree_nodes and neighbor not in visited:
                visited[neighbor] = dist + 1
                queue.append(neighbor)
                if dist + 1 > far_dist:
                    far_dist = dist + 1
                    far_node = neighbor

    return far_node, far_dist


# 全局 graph，用于 diameter 计算
graph_local = defaultdict(set)


def analyze_question(item, q_emb, func_to_module, module_to_funcs, func_to_file,
                     file_to_funcs, func_calls, graph, chunk_index_by_id, doc_matrix,
                     line_lookup, chunks_by_id):
    gold_fids = gold_evidence_to_function_ids(item, line_lookup, chunks_by_id)
    if len(gold_fids) < 1:
        return None

    # Get top-1 seed
    ranked_modules = score_modules(q_emb, module_to_funcs, doc_matrix, chunk_index_by_id, method="max")
    top3_modules = [mid for mid, _ in ranked_modules[:3]]

    if not top3_modules:
        return None

    _, region_fids = build_region(
        top3_modules, "top3_modules+callers_k5", module_to_funcs, func_to_module,
        func_to_file, file_to_funcs, func_calls, q_emb, doc_matrix, chunk_index_by_id,
    )
    region_fids = set(region_fids)

    region_fids_list = list(region_fids)
    idxs = [chunk_index_by_id[fid] for fid in region_fids_list if fid in chunk_index_by_id]
    if not idxs:
        return None
    sub = doc_matrix[idxs]
    sims = sub @ q_emb
    top_seed = region_fids_list[np.argmax(sims)]

    # Build evidence tree
    tree_nodes, tree_edges, _, distances = build_evidence_tree(gold_fids, graph, max_depth=10)

    if not tree_nodes:
        return None

    # Tree stats
    steiner_nodes = tree_nodes - gold_fids
    diameter = tree_diameter(tree_nodes, tree_edges, graph)

    # Seed relation to tree
    seed_on_tree = top_seed in tree_nodes
    seed_distance_to_tree = 0 if seed_on_tree else None

    if not seed_on_tree:
        # BFS distance from seed to any tree node
        visited = {top_seed: 0}
        queue = deque([top_seed])
        while queue:
            node = queue.popleft()
            if node in tree_nodes:
                seed_distance_to_tree = visited[node]
                break
            for neighbor in graph.get(node, []):
                if neighbor not in visited:
                    visited[neighbor] = visited[node] + 1
                    queue.append(neighbor)

    # Is seed close to tree (within 2 hops)?
    seed_close = seed_on_tree or (seed_distance_to_tree is not None and seed_distance_to_tree <= 2)

    return {
        "qa_id": item["qa_id"],
        "question": item["question"],
        "gold_count": len(gold_fids),
        "gold_fids": list(gold_fids),
        "top_seed": top_seed,
        "tree_node_count": len(tree_nodes),
        "tree_edge_count": len(tree_edges),
        "steiner_node_count": len(steiner_nodes),
        "tree_diameter": diameter,
        "seed_on_tree": seed_on_tree,
        "seed_distance_to_tree": seed_distance_to_tree,
        "seed_close_to_tree": seed_close,
        "gold_pair_distances": [distances.get(tuple(sorted([u, v])), None)
                                for u in gold_fids for v in gold_fids if u < v],
    }


def print_report(results):
    print("\n" + "=" * 95)
    print("EVIDENCE TREE ANALYSIS")
    print("=" * 95)

    total_gold = sum(r["gold_count"] for r in results)
    multi_gold = [r for r in results if r["gold_count"] > 1]

    print(f"\nTotal questions: {len(results)}")
    print(f"Questions with ≥2 golds: {len(multi_gold)}")
    print(f"Total gold functions: {total_gold}")

    # Tree size stats
    print("\n[Evidence Tree Size]")
    tree_nodes_all = [r["tree_node_count"] for r in multi_gold]
    steiner_nodes_all = [r["steiner_node_count"] for r in multi_gold]
    diameters = [r["tree_diameter"] for r in multi_gold]

    print(f"  Mean tree nodes: {np.mean(tree_nodes_all):.2f}")
    print(f"  Median tree nodes: {np.median(tree_nodes_all):.1f}")
    print(f"  Mean Steiner nodes: {np.mean(steiner_nodes_all):.2f}")
    print(f"  Median Steiner nodes: {np.median(steiner_nodes_all):.1f}")
    print(f"  Mean tree diameter: {np.mean(diameters):.2f}")
    print(f"  Median tree diameter: {np.median(diameters):.1f}")

    # Distribution
    print("\n[Tree Node Count Distribution]")
    counter = Counter(r["tree_node_count"] for r in multi_gold)
    for k in sorted(counter.keys()):
        print(f"  {k} nodes: {counter[k]} questions ({counter[k]/len(multi_gold)*100:.1f}%)")

    print("\n[Steiner Node Count Distribution]")
    counter = Counter(r["steiner_node_count"] for r in multi_gold)
    for k in sorted(counter.keys()):
        print(f"  {k} Steiner nodes: {counter[k]} questions ({counter[k]/len(multi_gold)*100:.1f}%)")

    # Seed relation
    print("\n[Top-1 Seed Relation to Evidence Tree]")
    seed_on_tree = sum(1 for r in results if r["seed_on_tree"])
    seed_close = sum(1 for r in results if r["seed_close_to_tree"])
    print(f"  Seed on tree: {seed_on_tree}/{len(results)} ({seed_on_tree/len(results)*100:.1f}%)")
    print(f"  Seed on tree or ≤2 hops away: {seed_close}/{len(results)} ({seed_close/len(results)*100:.1f}%)")

    # Distance distribution for seeds not on tree
    dists = [r["seed_distance_to_tree"] for r in results if r["seed_distance_to_tree"] is not None and r["seed_distance_to_tree"] > 0]
    if dists:
        print(f"\n  Seed distance to tree (when not on tree):")
        dist_counter = Counter(dists)
        for d in sorted(dist_counter.keys()):
            print(f"    {d} hop(s): {dist_counter[d]}")

    # Ratio: tree size / gold count
    ratios = [r["tree_node_count"] / r["gold_count"] for r in multi_gold]
    print(f"\n[Tree Size / Gold Count Ratio]")
    print(f"  Mean: {np.mean(ratios):.2f}")
    print(f"  Median: {np.median(ratios):.2f}")

    # Examples
    print("\n[Examples: Small Evidence Trees]")
    for r in sorted(multi_gold, key=lambda x: x["tree_node_count"])[:5]:
        print(f"\n  {r['qa_id']}: {r['gold_count']} golds → tree {r['tree_node_count']} nodes, diameter {r['tree_diameter']}")
        print(f"    Seed on tree: {r['seed_on_tree']}, seed close: {r['seed_close_to_tree']}")
        print(f"    Q: {r['question'][:70]}...")

    print("\n[Examples: Large Evidence Trees]")
    for r in sorted(multi_gold, key=lambda x: -x["tree_node_count"])[:5]:
        print(f"\n  {r['qa_id']}: {r['gold_count']} golds → tree {r['tree_node_count']} nodes, diameter {r['tree_diameter']}")
        print(f"    Seed on tree: {r['seed_on_tree']}, seed close: {r['seed_close_to_tree']}")
        print(f"    Q: {r['question'][:70]}...")


def main():
    global graph_local

    benchmark_path = _ROOT / "datasets" / "benchmark_hard.json"
    index_path = _ROOT / "data" / "qa_embedding_index.json"

    items = load_benchmark(benchmark_path)
    chunks, doc_matrix = load_embedding_index(index_path)
    line_lookup, chunks_by_id, chunk_index_by_id, func_to_file, file_to_funcs = build_helpers(chunks)

    funcs, calls = fetch_functions_and_calls()
    func_to_module, module_to_funcs = detect_modules(funcs, calls, resolution=0.5)
    func_calls = fetch_call_graph()
    graph = build_undirected_graph(func_calls)
    graph_local = graph

    retriever = FastEmbeddingRetriever(index_path=index_path)
    print("Encoding questions...")
    q_embs = retriever.encode_queries([item["question"] for item in items])

    results = []
    for item, q_emb in zip(items, q_embs):
        r = analyze_question(
            item, q_emb, func_to_module, module_to_funcs, func_to_file,
            file_to_funcs, func_calls, graph, chunk_index_by_id, doc_matrix,
            line_lookup, chunks_by_id,
        )
        if r:
            results.append(r)

    print_report(results)

    output_path = _ROOT / "results" / "evidence_tree_analysis.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\nSaved detailed results to: {output_path}")


if __name__ == "__main__":
    main()
