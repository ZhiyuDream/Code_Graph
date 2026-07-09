#!/usr/bin/env python3
"""
极简 Navigation Prototype：Best-First Graph Search。

不使用 LLM，不使用 Agent，只使用：
1. Question embedding
2. Call graph
3. Embedding-based frontier selection

流程：
Question
  ↓
Top Seed（region 内 embedding 最高）
  ↓
Repeat N steps:
  - Expand frontier by 1 hop (callers + callees)
  - Score all newly reached nodes by embedding similarity
  - Keep top-K as new frontier
  ↓
Union of all visited nodes = candidate set
  ↓
Top-M retrieval within candidate set

对比：
- baseline: 全库 embedding retrieval
- region: region 内 embedding retrieval
- navigation: graph-expanded candidate set + embedding retrieval
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
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
    undirected = defaultdict(set)
    callers = defaultdict(set)  # callee -> callers
    callees = defaultdict(set)  # caller -> callees
    for a, b in func_calls:
        undirected[a].add(b)
        undirected[b].add(a)
        callers[b].add(a)
        callees[a].add(b)
    return undirected, callers, callees


def best_first_graph_search(
    seed: str,
    graph: dict,
    q_emb: np.ndarray,
    doc_matrix: np.ndarray,
    chunk_index_by_id: dict,
    max_steps: int = 3,
    frontier_size: int = 20,
    direction: str = "both",
) -> set[str]:
    """
    Best-first graph search。

    每一步：
    1. 扩展当前 frontier 的 1-hop 邻居
    2. 用 embedding similarity 给所有邻居打分
    3. 保留 top-K 作为下一步 frontier

    返回所有访问过的节点集合。
    """
    if direction == "both":
        g = graph
    elif direction == "callers":
        g = callers_global
    elif direction == "callees":
        g = callees_global
    else:
        raise ValueError(f"Unknown direction: {direction}")

    visited = {seed}
    frontier = {seed}

    for step in range(max_steps):
        # 扩展 frontier
        neighbors = set()
        for node in frontier:
            neighbors.update(g.get(node, set()))

        # 去掉已访问
        new_nodes = neighbors - visited
        if not new_nodes:
            break

        # 给新节点打分
        scored = []
        for fid in new_nodes:
            idx = chunk_index_by_id.get(fid)
            if idx is not None:
                sim = float(doc_matrix[idx] @ q_emb)
                scored.append((fid, sim))

        scored.sort(key=lambda x: -x[1])
        top_k = {fid for fid, _ in scored[:frontier_size]}

        frontier = top_k
        visited.update(top_k)

    return visited


def evaluate_navigation(
    items,
    q_embs,
    func_to_module,
    module_to_funcs,
    func_to_file,
    file_to_funcs,
    func_calls,
    undirected,
    callers,
    callees,
    chunk_index_by_id,
    doc_matrix,
    line_lookup,
    chunks_by_id,
):
    global callers_global, callees_global
    callers_global = callers
    callees_global = callees

    results = []
    all_func_ids = list(chunk_index_by_id.keys())

    for item, q_emb in zip(items, q_embs):
        gold_fids = gold_evidence_to_function_ids(item, line_lookup, chunks_by_id)
        if not gold_fids:
            continue

        # Build region
        ranked_modules = score_modules(q_emb, module_to_funcs, doc_matrix, chunk_index_by_id, method="max")
        top3_modules = [mid for mid, _ in ranked_modules[:3]]
        if not top3_modules:
            continue

        _, region_fids = build_region(
            top3_modules, "top3_modules+callers_k5", module_to_funcs, func_to_module,
            func_to_file, file_to_funcs, func_calls, q_emb, doc_matrix, chunk_index_by_id,
        )
        region_fids = set(region_fids)

        # Find top seed in region
        region_fids_list = list(region_fids)
        region_idxs = [chunk_index_by_id[fid] for fid in region_fids_list if fid in chunk_index_by_id]
        region_sims = doc_matrix[region_idxs] @ q_emb
        top_region_seed = region_fids_list[np.argmax(region_sims)]

        # Find top seed in all functions
        all_sims = doc_matrix @ q_emb
        top_global_seed = all_func_ids[np.argmax(all_sims)]

        q_result = {
            "qa_id": item["qa_id"],
            "question": item["question"],
            "gold_count": len(gold_fids),
            "gold_fids": list(gold_fids),
        }

        # Baseline: top-K from all functions
        baseline_top = get_top_k(q_emb, all_func_ids, doc_matrix, chunk_index_by_id, top_k=100)
        q_result["baseline"] = evaluate_scope(gold_fids, baseline_top, len(all_func_ids), func_to_file)

        # Region: top-K from region
        region_top = get_top_k(q_emb, list(region_fids), doc_matrix, chunk_index_by_id, top_k=100)
        q_result["region"] = evaluate_scope(gold_fids, region_top, len(region_fids), func_to_file)

        # Navigation configs
        configs = [
            ("nav_region_seed_s3_k20", top_region_seed, "both", 3, 20),
            ("nav_region_seed_s3_k50", top_region_seed, "both", 3, 50),
            ("nav_region_seed_s4_k20", top_region_seed, "both", 4, 20),
            ("nav_region_seed_s4_k50", top_region_seed, "both", 4, 50),
            ("nav_global_seed_s3_k20", top_global_seed, "both", 3, 20),
            ("nav_global_seed_s3_k50", top_global_seed, "both", 3, 50),
            ("nav_region_seed_callers_s3_k20", top_region_seed, "callers", 3, 20),
            ("nav_region_seed_callees_s3_k20", top_region_seed, "callees", 3, 20),
        ]

        for name, seed, direction, steps, k in configs:
            visited = best_first_graph_search(
                seed, undirected, q_emb, doc_matrix, chunk_index_by_id,
                max_steps=steps, frontier_size=k, direction=direction,
            )

            # 在 visited 集合里做 top-K retrieval
            visited_top = get_top_k(q_emb, list(visited), doc_matrix, chunk_index_by_id, top_k=100)
            q_result[name] = evaluate_scope(gold_fids, visited_top, len(visited), func_to_file)
            q_result[name]["visited_count"] = len(visited)

        results.append(q_result)

    return results


def get_top_k(q_emb, candidate_fids, doc_matrix, chunk_index_by_id, top_k=100):
    idxs = [chunk_index_by_id[fid] for fid in candidate_fids if fid in chunk_index_by_id]
    if not idxs:
        return []
    sub = doc_matrix[idxs]
    sims = sub @ q_emb
    order = np.argsort(-sims)[:top_k]
    return [(candidate_fids[i], float(sims[i])) for i in order]


def evaluate_scope(gold_fids, retrieved, scope_size, func_to_file):
    retrieved_fids = [fid for fid, _ in retrieved]

    metrics = {}
    for k in [5, 10, 20, 50, 100]:
        top_k_set = set(retrieved_fids[:k])
        covered = gold_fids & top_k_set
        metrics[f"recall@{k}"] = len(covered) / len(gold_fids)
        metrics[f"has_gold@{k}"] = len(covered) > 0
        metrics[f"full_coverage@{k}"] = len(covered) == len(gold_fids)

    first_gold_rank = None
    for rank, fid in enumerate(retrieved_fids, 1):
        if fid in gold_fids:
            first_gold_rank = rank
            break

    metrics["scope_size"] = scope_size
    metrics["mrr"] = 1.0 / first_gold_rank if first_gold_rank else 0.0
    metrics["first_gold_rank"] = first_gold_rank

    return metrics


def print_report(results):
    print("\n" + "=" * 100)
    print("MINIMAL NAVIGATION PROTOTYPE (Best-First Graph Search)")
    print("=" * 100)

    config_names = [key for key in results[0].keys()
                    if key not in ("qa_id", "question", "gold_count", "gold_fids")]

    print("\n[Scope Size]")
    print(f"{'Config':<45} {'Scope/Visited Size':<20}")
    for name in config_names:
        if name.startswith("nav_"):
            size = np.mean([r[name]["scope_size"] for r in results])
            print(f"{name:<45} {size:<20.0f}")
        else:
            size = np.mean([r[name]["scope_size"] for r in results])
            print(f"{name:<45} {size:<20.0f}")

    print("\n[Recall@K]")
    header = f"{'Config':<45}"
    for k in [5, 10, 20, 50, 100]:
        header += f" R@{k:<4}"
    print(header)
    for name in config_names:
        line = f"{name:<45}"
        for k in [5, 10, 20, 50, 100]:
            mean_r = np.mean([r[name][f"recall@{k}"] for r in results])
            line += f" {mean_r*100:5.1f}"
        print(line)

    print("\n[CDF: Has Any Gold in Top-K]")
    header = f"{'Config':<45}"
    for k in [5, 10, 20, 50, 100]:
        header += f" P@{k:<4}"
    print(header)
    for name in config_names:
        line = f"{name:<45}"
        for k in [5, 10, 20, 50, 100]:
            mean_p = np.mean([r[name][f"has_gold@{k}"] for r in results])
            line += f" {mean_p*100:5.1f}"
        print(line)

    print("\n[MRR & Median First Gold Rank]")
    print(f"{'Config':<45} {'MRR':<10} {'Median Rank':<15}")
    for name in config_names:
        mrr = np.mean([r[name]["mrr"] for r in results])
        ranks = [r[name]["first_gold_rank"] for r in results if r[name]["first_gold_rank"] is not None]
        median_r = np.median(ranks) if ranks else 0
        print(f"{name:<45} {mrr:<10.3f} {median_r:<15.0f}")


callers_global = {}
callees_global = {}


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

    results = evaluate_navigation(
        items, q_embs, func_to_module, module_to_funcs, func_to_file,
        file_to_funcs, func_calls, undirected, callers, callees,
        chunk_index_by_id, doc_matrix, line_lookup, chunks_by_id,
    )

    print_report(results)

    output_path = _ROOT / "results" / "minimal_navigation.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\nSaved detailed results to: {output_path}")


if __name__ == "__main__":
    main()
