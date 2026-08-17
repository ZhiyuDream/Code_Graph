#!/usr/bin/env python3
"""
Evidence Chain Reachability 实验。

核心问题：从 Region 内的 top retrieved function 出发，
通过 call graph 的 1/2/3 跳扩展，能否到达 gold functions？

这验证的是：Question 是否需要多跳证据链推理，而不是单个函数匹配。

实验设计：
1. 对每个 question，构建 region (top3_modules + callers_k5)
2. 在 region 内做 embedding retrieval，取 top-1/3/5 seed functions
3. 从每个 seed 出发，在 call graph 上做 BFS：
   - 1-hop: 直接 caller + callee
   - 2-hop: 再扩展一层
   - 3-hop: 再扩展一层
4. 统计 gold functions 在多少跳内可达

扩展策略：
- within_region: 只扩展 region 内的函数
- full_graph: 扩展到全图
- callers_only: 只向上游扩展
- callees_only: 只向下游扩展
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict, deque
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


def bfs_reachable(seed: str, graph: dict, max_hops: int, allowed_nodes: set | None = None):
    """从 seed 出发做 BFS，返回每个 hop 能到达的节点集合。"""
    visited = {seed: 0}
    queue = deque([seed])
    reach_by_hop = {0: {seed}}

    while queue:
        node = queue.popleft()
        dist = visited[node]
        if dist >= max_hops:
            continue

        for neighbor in graph.get(node, []):
            if allowed_nodes is not None and neighbor not in allowed_nodes:
                continue
            if neighbor not in visited:
                visited[neighbor] = dist + 1
                queue.append(neighbor)
                reach_by_hop.setdefault(dist + 1, set()).add(neighbor)

    return visited, reach_by_hop


def evaluate_reachability(
    items,
    q_embs,
    func_to_module,
    module_to_funcs,
    func_to_file,
    file_to_funcs,
    func_calls,
    chunk_index_by_id,
    doc_matrix,
    line_lookup,
    chunks_by_id,
    max_hops: int = 3,
    seed_counts: tuple = (1, 3, 5),
):
    print("Evaluating evidence chain reachability...")

    # 构建 call graph
    call_graph = defaultdict(set)  # function -> neighbors (both directions)
    caller_graph = defaultdict(set)  # function -> callers
    callee_graph = defaultdict(set)  # function -> callees

    for caller, callee in func_calls:
        call_graph[caller].add(callee)
        call_graph[callee].add(caller)
        caller_graph[callee].add(caller)
        callee_graph[caller].add(callee)

    results = []

    for item, q_emb in zip(items, q_embs):
        gold_fids = gold_evidence_to_function_ids(item, line_lookup, chunks_by_id)
        if not gold_fids:
            continue

        ranked_modules = score_modules(q_emb, module_to_funcs, doc_matrix, chunk_index_by_id, method="max")
        top3_modules = [mid for mid, _ in ranked_modules[:3]]

        if not top3_modules:
            continue

        # Build region
        _, region_fids = build_region(
            top3_modules, "top3_modules+callers_k5", module_to_funcs, func_to_module,
            func_to_file, file_to_funcs, func_calls, q_emb, doc_matrix, chunk_index_by_id,
        )
        region_fids = set(region_fids)

        # Get top-K seeds within region by embedding
        region_fids_list = list(region_fids)
        idxs = [chunk_index_by_id[fid] for fid in region_fids_list if fid in chunk_index_by_id]
        if not idxs:
            continue
        sub = doc_matrix[idxs]
        sims = sub @ q_emb
        order = np.argsort(-sims)

        q_result = {
            "qa_id": item["qa_id"],
            "question": item["question"],
            "gold_count": len(gold_fids),
            "gold_fids": list(gold_fids),
            "region_size": len(region_fids),
        }

        for seed_count in seed_counts:
            top_seeds = [region_fids_list[order[i]] for i in range(min(seed_count, len(order)))]

            for direction in ["both", "callers_only", "callees_only"]:
                for scope in ["within_region", "full_graph"]:
                    allowed = region_fids if scope == "within_region" else None

                    key = f"seeds{seed_count}_{direction}_{scope}"

                    # 合并多个 seed 的 reachable sets
                    reachable = set()
                    reachable_by_hop = defaultdict(set)

                    for seed in top_seeds:
                        if direction == "both":
                            graph = call_graph
                        elif direction == "callers_only":
                            graph = caller_graph
                        else:
                            graph = callee_graph

                        visited, reach_by_hop = bfs_reachable(seed, graph, max_hops, allowed)
                        reachable.update(visited.keys())
                        for hop, nodes in reach_by_hop.items():
                            reachable_by_hop[hop].update(nodes)

                    # 计算每个 hop 的 gold coverage
                    coverage_by_hop = {}
                    cumulative = set()
                    for hop in range(max_hops + 1):
                        cumulative.update(reachable_by_hop.get(hop, set()))
                        covered = gold_fids & cumulative
                        coverage_by_hop[hop] = len(covered) / len(gold_fids)

                    # 统计最终 reachable 的 gold 数
                    covered_final = gold_fids & reachable

                    q_result[key] = {
                        "reachable_count": len(reachable),
                        "covered_gold_count": len(covered_final),
                        "coverage": len(covered_final) / len(gold_fids),
                        "coverage_by_hop": coverage_by_hop,
                        "seeds": top_seeds,
                    }

        results.append(q_result)

    return results


def print_report(results, max_hops: int = 3):
    print("\n" + "=" * 95)
    print("EVIDENCE CHAIN REACHABILITY REPORT")
    print("=" * 95)

    seed_counts = (1, 3, 5)
    directions = ["both", "callers_only", "callees_only"]
    scopes = ["within_region", "full_graph"]

    total_gold = sum(r["gold_count"] for r in results)

    print(f"\nTotal gold functions: {total_gold}")

    # 总覆盖率
    print("\n[Final Coverage after max hops]")
    print(f"{'Config':<35} {'Reachable Funcs':<18} {'Gold Coverage':<15}")
    for sc in seed_counts:
        for direction in directions:
            for scope in scopes:
                key = f"seeds{sc}_{direction}_{scope}"
                reachable = sum(r[key]["reachable_count"] for r in results)
                covered = sum(r[key]["covered_gold_count"] for r in results)
                print(f"{key:<35} {reachable/len(results):>10.0f}        {covered/total_gold*100:>6.1f}%")

    # 按 hop 的累计覆盖率
    print("\n[Cumulative Coverage by Hop]")
    for sc in seed_counts:
        print(f"\n  Seeds: {sc}")
        header = f"    {'Config':<30}"
        for hop in range(max_hops + 1):
            header += f" Hop{hop:<3}"
        print(header)

        for direction in directions:
            for scope in scopes:
                key = f"seeds{sc}_{direction}_{scope}"
                line = f"    {direction+'_'+scope:<30}"
                for hop in range(max_hops + 1):
                    cov = np.mean([r[key]["coverage_by_hop"][hop] for r in results])
                    line += f" {cov*100:>5.1f}%"
                print(line)

    # 对比：Region Coverage vs Reachability
    print("\n[Region Coverage vs Chain Reachability]")
    print(f"  Region Coverage (embedding top-K): ~96.6%")

    for sc in seed_counts:
        for direction in directions:
            for scope in scopes:
                key = f"seeds{sc}_{direction}_{scope}"
                final_cov = np.mean([r[key]["coverage"] for r in results])
                print(f"  {key:<35} avg per-question coverage: {final_cov*100:>5.1f}%")


def main():
    benchmark_path = _ROOT / "datasets" / "benchmark_hard.json"
    index_path = _ROOT / "data" / "qa_embedding_index.json"

    items = load_benchmark(benchmark_path)
    chunks, doc_matrix = load_embedding_index(index_path)
    line_lookup, chunks_by_id, chunk_index_by_id, func_to_file, file_to_funcs = build_helpers(chunks)

    funcs, calls = fetch_functions_and_calls()
    func_to_module, module_to_funcs = detect_modules(funcs, calls, resolution=0.5)
    func_calls = fetch_call_graph()

    retriever = FastEmbeddingRetriever(index_path=index_path)
    print("Encoding questions...")
    q_embs = retriever.encode_queries([item["question"] for item in items])

    results = evaluate_reachability(
        items, q_embs, func_to_module, module_to_funcs, func_to_file,
        file_to_funcs, func_calls, chunk_index_by_id, doc_matrix,
        line_lookup, chunks_by_id, max_hops=3, seed_counts=(1, 3, 5),
    )

    print_report(results, max_hops=3)

    output_path = _ROOT / "results" / "evidence_chain_reachability.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\nSaved detailed results to: {output_path}")


if __name__ == "__main__":
    main()
