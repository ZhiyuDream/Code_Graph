#!/usr/bin/env python3
"""
Evidence Chain Structure Analysis。

两个核心分析：

1. Gold Distance Distribution
   对每个 gold function，计算它从 Top-1 Seed 出发在 call graph 中的最短距离。
   回答：gold 是否集中在 seed 的 2-3 跳范围内？

2. Oracle Seed Reachability
   用每个 gold function 本身作为 seed，做 1/2/3-hop graph walk，
   看能覆盖多少同一问题的其他 gold functions。
   回答：gold functions 是否形成局部证据链？

同时统计 call graph locality 相关的指标。
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


def build_call_graphs(func_calls: list[tuple]):
    """构建三个图：双向、caller-only、callee-only。"""
    undirected = defaultdict(set)
    callers = defaultdict(set)  # callee -> callers
    callees = defaultdict(set)  # caller -> callees

    for a, b in func_calls:
        undirected[a].add(b)
        undirected[b].add(a)
        callers[b].add(a)
        callees[a].add(b)

    return undirected, callers, callees


def shortest_distance(start: str, targets: set, graph: dict, max_depth: int = 10):
    """BFS 计算 start 到 targets 中每个节点的最短距离。"""
    distances = {}
    if start in targets:
        distances[start] = 0

    visited = {start: 0}
    queue = deque([start])

    while queue:
        node = queue.popleft()
        dist = visited[node]
        if dist >= max_depth:
            continue

        for neighbor in graph.get(node, []):
            if neighbor not in visited:
                visited[neighbor] = dist + 1
                queue.append(neighbor)
                if neighbor in targets:
                    distances[neighbor] = dist + 1
                    # 不 return，继续找所有 targets 的距离

    return distances


def bfs_reachable(start: str, graph: dict, max_hops: int):
    """返回从 start 出发 max_hops 内可达的所有节点。"""
    visited = {start: 0}
    queue = deque([start])

    while queue:
        node = queue.popleft()
        dist = visited[node]
        if dist >= max_hops:
            continue
        for neighbor in graph.get(node, []):
            if neighbor not in visited:
                visited[neighbor] = dist + 1
                queue.append(neighbor)

    return set(visited.keys())


def analyze_question(item, q_emb, func_to_module, module_to_funcs, func_to_file,
                     file_to_funcs, func_calls, undirected, callers, callees,
                     chunk_index_by_id, doc_matrix, line_lookup, chunks_by_id):
    gold_fids = gold_evidence_to_function_ids(item, line_lookup, chunks_by_id)
    if len(gold_fids) < 1:
        return None

    # Build region and get top-1 seed
    ranked_modules = score_modules(q_emb, module_to_funcs, doc_matrix, chunk_index_by_id, method="max")
    top3_modules = [mid for mid, _ in ranked_modules[:3]]

    if not top3_modules:
        return None

    _, region_fids = build_region(
        top3_modules, "top3_modules+callers_k5", module_to_funcs, func_to_module,
        func_to_file, file_to_funcs, func_calls, q_emb, doc_matrix, chunk_index_by_id,
    )
    region_fids = set(region_fids)

    # Top-1 seed in region
    region_fids_list = list(region_fids)
    idxs = [chunk_index_by_id[fid] for fid in region_fids_list if fid in chunk_index_by_id]
    if not idxs:
        return None
    sub = doc_matrix[idxs]
    sims = sub @ q_emb
    top_seed = region_fids_list[np.argmax(sims)]

    q_result = {
        "qa_id": item["qa_id"],
        "question": item["question"],
        "gold_count": len(gold_fids),
        "gold_fids": list(gold_fids),
        "top_seed": top_seed,
    }

    # 1. Gold distance from top seed
    distances = shortest_distance(top_seed, gold_fids, undirected, max_depth=10)
    q_result["gold_distances_from_seed"] = distances

    # 2. Oracle seed reachability: for each gold as seed, how many other golds are reached
    oracle_reach = {}
    for seed in gold_fids:
        seed_reach = {}
        for hops in [1, 2, 3]:
            reached = bfs_reachable(seed, undirected, hops)
            other_golds = gold_fids - {seed}
            covered = other_golds & reached
            seed_reach[hops] = {
                "reachable_count": len(reached),
                "other_gold_count": len(other_golds),
                "covered_count": len(covered),
                "coverage": len(covered) / len(other_golds) if other_golds else 0,
            }
        oracle_reach[seed] = seed_reach

    q_result["oracle_seed_reachability"] = oracle_reach

    # 3. Best oracle seed: which gold, when used as seed, covers most other golds
    best_coverage = 0
    best_seed = None
    for seed, reach_data in oracle_reach.items():
        cov = reach_data[3]["coverage"]  # 3-hop
        if cov > best_coverage:
            best_coverage = cov
            best_seed = seed
    q_result["best_oracle_seed_3hop_coverage"] = best_coverage
    q_result["best_oracle_seed"] = best_seed

    # 4. Min distance between any pair of gold functions (graph diameter of gold subgraph)
    gold_pair_distances = []
    gold_list = list(gold_fids)
    for i, g1 in enumerate(gold_list):
        for g2 in gold_list[i+1:]:
            dists = shortest_distance(g1, {g2}, undirected, max_depth=10)
            if g2 in dists:
                gold_pair_distances.append(dists[g2])

    q_result["gold_pair_distances"] = gold_pair_distances
    q_result["gold_pair_max_distance"] = max(gold_pair_distances) if gold_pair_distances else 0
    q_result["gold_pair_mean_distance"] = np.mean(gold_pair_distances) if gold_pair_distances else 0

    return q_result


def print_report(results):
    print("\n" + "=" * 95)
    print("EVIDENCE CHAIN STRUCTURE ANALYSIS")
    print("=" * 95)

    # 1. Gold Distance Distribution from Top Seed
    all_distances = []
    for r in results:
        for gold, dist in r["gold_distances_from_seed"].items():
            all_distances.append({
                "qa_id": r["qa_id"],
                "gold": gold,
                "distance": dist,
            })

    print(f"\n[Gold Distance from Top-1 Seed]")
    print(f"Total gold functions: {len(all_distances)}")

    dist_counter = Counter(d["distance"] for d in all_distances)
    for dist in sorted(dist_counter.keys()):
        count = dist_counter[dist]
        print(f"  {dist} hop(s): {count} ({count/len(all_distances)*100:.1f}%)")

    # Cumulative
    print(f"\n  Cumulative:")
    cumulative = 0
    for dist in sorted(dist_counter.keys()):
        cumulative += dist_counter[dist]
        print(f"  ≤{dist} hop(s): {cumulative} ({cumulative/len(all_distances)*100:.1f}%)")

    # 2. Oracle Seed Reachability
    print(f"\n[Oracle Seed Reachability]")
    print("For each question, use each gold as seed, measure coverage of other golds within N hops.")

    # Aggregate: for each question, best possible coverage using any gold as seed
    best_coverages = {1: [], 2: [], 3: []}
    avg_coverages = {1: [], 2: [], 3: []}

    for r in results:
        if r["gold_count"] <= 1:
            continue

        for hops in [1, 2, 3]:
            q_best = 0
            q_coverages = []
            for seed, reach_data in r["oracle_seed_reachability"].items():
                cov = reach_data[hops]["coverage"]
                q_best = max(q_best, cov)
                q_coverages.append(cov)

            best_coverages[hops].append(q_best)
            avg_coverages[hops].append(np.mean(q_coverages))

    print(f"\n  Questions with ≥2 golds: {len(best_coverages[1])}")
    for hops in [1, 2, 3]:
        print(f"\n  {hops}-hop reachability:")
        print(f"    Best-seed avg coverage: {np.mean(best_coverages[hops])*100:.1f}%")
        print(f"    Avg-seed avg coverage:  {np.mean(avg_coverages[hops])*100:.1f}%")

    # 3. Gold Pair Distances
    all_pair_distances = []
    for r in results:
        all_pair_distances.extend(r["gold_pair_distances"])

    print(f"\n[Gold Pair Distance Distribution]")
    print(f"Total gold pairs: {len(all_pair_distances)}")
    pair_counter = Counter(all_pair_distances)
    for dist in sorted(pair_counter.keys()):
        count = pair_counter[dist]
        print(f"  {dist} hop(s): {count} ({count/len(all_pair_distances)*100:.1f}%)")

    mean_pair_dist = np.mean(all_pair_distances) if all_pair_distances else 0
    median_pair_dist = np.median(all_pair_distances) if all_pair_distances else 0
    print(f"\n  Mean pair distance: {mean_pair_dist:.2f}")
    print(f"  Median pair distance: {median_pair_dist:.1f}")

    # 4. Per-question gold subgraph diameter
    diameters = [r["gold_pair_max_distance"] for r in results if r["gold_count"] > 1]
    print(f"\n[Gold Subgraph Diameter per Question]")
    print(f"  Mean: {np.mean(diameters):.2f}")
    print(f"  Median: {np.median(diameters):.1f}")

    # Examples
    print(f"\n[Examples: Questions where golds are far apart]")
    for r in sorted(results, key=lambda x: -x["gold_pair_max_distance"])[:5]:
        print(f"\n  {r['qa_id']}: max gold pair distance = {r['gold_pair_max_distance']}")
        print(f"    Q: {r['question'][:70]}...")
        print(f"    Gold count: {r['gold_count']}")

    print(f"\n[Examples: Questions where best oracle seed covers all golds in 3 hops]")
    fully_covered = [r for r in results if r["best_oracle_seed_3hop_coverage"] == 1.0 and r["gold_count"] > 1]
    print(f"    Count: {len(fully_covered)}/{len([r for r in results if r['gold_count'] > 1])}")
    for r in fully_covered[:5]:
        print(f"\n  {r['qa_id']}: {r['gold_count']} golds")
        print(f"    Q: {r['question'][:70]}...")


def main():
    benchmark_path = _ROOT / "datasets" / "benchmark_hard.json"
    index_path = _ROOT / "data" / "qa_embedding_index.json"

    items = load_benchmark(benchmark_path)
    chunks, doc_matrix = load_embedding_index(index_path)
    line_lookup, chunks_by_id, chunk_index_by_id, func_to_file, file_to_funcs = build_helpers(chunks)

    funcs, calls = fetch_functions_and_calls()
    func_to_module, module_to_funcs = detect_modules(funcs, calls, resolution=0.5)
    func_calls = fetch_call_graph()
    undirected, callers, callees = build_call_graphs(func_calls)

    retriever = FastEmbeddingRetriever(index_path=index_path)
    print("Encoding questions...")
    q_embs = retriever.encode_queries([item["question"] for item in items])

    results = []
    for item, q_emb in zip(items, q_embs):
        r = analyze_question(
            item, q_emb, func_to_module, module_to_funcs, func_to_file,
            file_to_funcs, func_calls, undirected, callers, callees,
            chunk_index_by_id, doc_matrix, line_lookup, chunks_by_id,
        )
        if r:
            results.append(r)

    print_report(results)

    output_path = _ROOT / "results" / "evidence_chain_structure.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\nSaved detailed results to: {output_path}")


if __name__ == "__main__":
    main()
