#!/usr/bin/env python3
"""
两个实验：

1. Multiple-Seed Non-Greedy Navigation
   - Top-5 seeds in region
   - 3-hop expansion (both/callers/callees)
   - Union all visited nodes
   - Retrieve within union

2. Steiner Node vs Gold Node Analysis
   - 对每个 evidence tree，比较 gold nodes 和 Steiner nodes 的：
     * embedding similarity to question
     * graph degree
     * embedding score 分布

目的：
- 验证 "Multiple entry points + local expansion" 是否能超过 flat retrieval
- 验证 bridge/Steiner nodes 是否 embedding 低但结构重要
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
from scripts.analysis.evidence_tree_analysis import (
    build_evidence_tree,
    build_undirected_graph,
)
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
    get_module_directories,
)


def bfs_reachable(starts: list[str], graph: dict, max_hops: int) -> set[str]:
    visited = {}
    queue = deque()
    for s in starts:
        visited[s] = 0
        queue.append(s)

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


def evaluate_multiseed_navigation(
    items, q_embs, func_to_module, module_to_funcs, func_to_file, file_to_funcs,
    func_calls, undirected, callers, callees, chunk_index_by_id, doc_matrix,
    line_lookup, chunks_by_id,
):
    print("Evaluating multiple-seed navigation...")
    results = []
    all_func_ids = list(chunk_index_by_id.keys())

    for item, q_emb in zip(items, q_embs):
        gold_fids = gold_evidence_to_function_ids(item, line_lookup, chunks_by_id)
        if not gold_fids:
            continue

        ranked_modules = score_modules(q_emb, module_to_funcs, doc_matrix, chunk_index_by_id, method="max")
        top3_modules = [mid for mid, _ in ranked_modules[:3]]
        if not top3_modules:
            continue

        _, region_fids = build_region(
            top3_modules, "top3_modules+callers_k5", module_to_funcs, func_to_module,
            func_to_file, file_to_funcs, func_calls, q_emb, doc_matrix, chunk_index_by_id,
        )
        region_fids = set(region_fids)

        # Top-5 seeds in region
        region_fids_list = list(region_fids)
        region_idxs = [chunk_index_by_id[fid] for fid in region_fids_list if fid in chunk_index_by_id]
        region_sims = doc_matrix[region_idxs] @ q_emb
        top5_seeds = [region_fids_list[i] for i in np.argsort(-region_sims)[:5]]

        q_result = {
            "qa_id": item["qa_id"],
            "question": item["question"],
            "gold_count": len(gold_fids),
        }

        # Baseline
        baseline_top = get_top_k(q_emb, all_func_ids, doc_matrix, chunk_index_by_id, top_k=100)
        q_result["baseline"] = evaluate_scope(gold_fids, baseline_top, len(all_func_ids), func_to_file)

        # Region
        region_top = get_top_k(q_emb, list(region_fids), doc_matrix, chunk_index_by_id, top_k=100)
        q_result["region"] = evaluate_scope(gold_fids, region_top, len(region_fids), func_to_file)

        # Multi-seed navigation
        configs = [
            ("multiseed_both_2hop", undirected, 2),
            ("multiseed_both_3hop", undirected, 3),
            ("multiseed_both_4hop", undirected, 4),
            ("multiseed_callers_3hop", callers, 3),
            ("multiseed_callees_3hop", callees, 3),
        ]

        for name, graph, hops in configs:
            visited = bfs_reachable(top5_seeds, graph, hops)
            visited_top = get_top_k(q_emb, list(visited), doc_matrix, chunk_index_by_id, top_k=100)
            q_result[name] = evaluate_scope(gold_fids, visited_top, len(visited), func_to_file)

        results.append(q_result)

    return results


def analyze_steiner_nodes(
    items, q_embs, func_to_module, module_to_funcs, func_to_file, file_to_funcs,
    func_calls, graph, chunk_index_by_id, doc_matrix, line_lookup, chunks_by_id,
):
    print("Analyzing Steiner node properties...")

    all_scores = {"gold": [], "steiner": []}
    all_degrees = {"gold": [], "steiner": []}
    per_question = []

    for item, q_emb in zip(items, q_embs):
        gold_fids = gold_evidence_to_function_ids(item, line_lookup, chunks_by_id)
        if len(gold_fids) < 2:
            continue

        tree_nodes, tree_edges, _, _ = build_evidence_tree(gold_fids, graph, max_depth=10)
        steiner_nodes = tree_nodes - gold_fids

        if not steiner_nodes:
            continue

        gold_sims = []
        steiner_sims = []
        gold_degrees = []
        steiner_degrees = []

        for fid in gold_fids:
            idx = chunk_index_by_id.get(fid)
            if idx is not None:
                gold_sims.append(float(doc_matrix[idx] @ q_emb))
            gold_degrees.append(len(graph.get(fid, set())))

        for fid in steiner_nodes:
            idx = chunk_index_by_id.get(fid)
            if idx is not None:
                steiner_sims.append(float(doc_matrix[idx] @ q_emb))
            steiner_degrees.append(len(graph.get(fid, set())))

        all_scores["gold"].extend(gold_sims)
        all_scores["steiner"].extend(steiner_sims)
        all_degrees["gold"].extend(gold_degrees)
        all_degrees["steiner"].extend(steiner_degrees)

        per_question.append({
            "qa_id": item["qa_id"],
            "gold_count": len(gold_fids),
            "steiner_count": len(steiner_nodes),
            "gold_sim_mean": np.mean(gold_sims) if gold_sims else 0,
            "steiner_sim_mean": np.mean(steiner_sims) if steiner_sims else 0,
            "gold_degree_mean": np.mean(gold_degrees) if gold_degrees else 0,
            "steiner_degree_mean": np.mean(steiner_degrees) if steiner_degrees else 0,
        })

    return all_scores, all_degrees, per_question


def print_multiseed_report(results):
    print("\n" + "=" * 100)
    print("MULTI-SEED NON-GREEDY NAVIGATION REPORT")
    print("=" * 100)

    config_names = [key for key in results[0].keys()
                    if key not in ("qa_id", "question", "gold_count")]

    print("\n[Scope Size]")
    print(f"{'Config':<35} {'Scope Size':<15}")
    for name in config_names:
        size = np.mean([r[name]["scope_size"] for r in results])
        print(f"{name:<35} {size:<15.0f}")

    print("\n[Recall@K]")
    header = f"{'Config':<35}"
    for k in [5, 10, 20, 50, 100]:
        header += f" R@{k:<4}"
    print(header)
    for name in config_names:
        line = f"{name:<35}"
        for k in [5, 10, 20, 50, 100]:
            mean_r = np.mean([r[name][f"recall@{k}"] for r in results])
            line += f" {mean_r*100:5.1f}"
        print(line)

    print("\n[Has Any Gold in Top-K]")
    header = f"{'Config':<35}"
    for k in [5, 10, 20, 50, 100]:
        header += f" P@{k:<4}"
    print(header)
    for name in config_names:
        line = f"{name:<35}"
        for k in [5, 10, 20, 50, 100]:
            mean_p = np.mean([r[name][f"has_gold@{k}"] for r in results])
            line += f" {mean_p*100:5.1f}"
        print(line)

    print("\n[MRR & Median First Gold Rank]")
    print(f"{'Config':<35} {'MRR':<10} {'Median Rank':<15}")
    for name in config_names:
        mrr = np.mean([r[name]["mrr"] for r in results])
        ranks = [r[name]["first_gold_rank"] for r in results if r[name]["first_gold_rank"] is not None]
        median_r = np.median(ranks) if ranks else 0
        print(f"{name:<35} {mrr:<10.3f} {median_r:<15.0f}")


def print_steiner_report(all_scores, all_degrees, per_question):
    print("\n" + "=" * 100)
    print("STEINER NODE VS GOLD NODE ANALYSIS")
    print("=" * 100)

    print(f"\nTotal gold nodes: {len(all_scores['gold'])}")
    print(f"Total Steiner nodes: {len(all_scores['steiner'])}")

    print("\n[Embedding Similarity to Question]")
    print(f"  Gold mean:    {np.mean(all_scores['gold']):.4f}")
    print(f"  Gold median:  {np.median(all_scores['gold']):.4f}")
    print(f"  Steiner mean: {np.mean(all_scores['steiner']):.4f}")
    print(f"  Steiner median:{np.median(all_scores['steiner']):.4f}")

    # Simple effect size
    diff = np.mean(all_scores['gold']) - np.mean(all_scores['steiner'])
    pooled_std = np.sqrt((np.std(all_scores['gold'])**2 + np.std(all_scores['steiner'])**2) / 2)
    cohen_d = diff / pooled_std if pooled_std > 0 else 0
    print(f"  Cohen's d (effect size): {cohen_d:.3f}")

    print("\n[Graph Degree]")
    print(f"  Gold mean:    {np.mean(all_degrees['gold']):.2f}")
    print(f"  Gold median:  {np.median(all_degrees['gold']):.1f}")
    print(f"  Steiner mean: {np.mean(all_degrees['steiner']):.2f}")
    print(f"  Steiner median:{np.median(all_degrees['steiner']):.1f}")

    diff_d = np.mean(all_degrees['steiner']) - np.mean(all_degrees['gold'])
    pooled_std_d = np.sqrt((np.std(all_degrees['gold'])**2 + np.std(all_degrees['steiner'])**2) / 2)
    cohen_d_d = diff_d / pooled_std_d if pooled_std_d > 0 else 0
    print(f"  Cohen's d (Steiner vs Gold degree): {cohen_d_d:.3f}")

    print("\n[Per-Question Mean Comparison]")
    gold_higher = sum(1 for q in per_question if q["gold_sim_mean"] > q["steiner_sim_mean"])
    steiner_higher_degree = sum(1 for q in per_question if q["steiner_degree_mean"] > q["gold_degree_mean"])
    print(f"  Questions where gold sim > Steiner sim: {gold_higher}/{len(per_question)} ({gold_higher/len(per_question)*100:.1f}%)")
    print(f"  Questions where Steiner degree > gold degree: {steiner_higher_degree}/{len(per_question)} ({steiner_higher_degree/len(per_question)*100:.1f}%)")

    print("\n[Similarity Score Distribution (bins)]")
    bins = np.percentile(all_scores['gold'] + all_scores['steiner'], [0, 25, 50, 75, 100])
    # Simpler bins
    bin_edges = [0.15, 0.20, 0.25, 0.30, 0.35, 1.0]
    labels = ["<0.20", "0.20-0.25", "0.25-0.30", "0.30-0.35", ">0.35"]
    for i in range(len(bin_edges)-1):
        lo, hi = bin_edges[i], bin_edges[i+1]
        gold_count = sum(1 for s in all_scores['gold'] if lo <= s < hi)
        steiner_count = sum(1 for s in all_scores['steiner'] if lo <= s < hi)
        print(f"  {labels[i]:<12}: gold={gold_count:>3} ({gold_count/len(all_scores['gold'])*100:>5.1f}%)  "
              f"steiner={steiner_count:>3} ({steiner_count/len(all_scores['steiner'])*100:>5.1f}%)")


def main():
    benchmark_path = _ROOT / "datasets" / "benchmark_hard.json"
    index_path = _ROOT / "data" / "qa_embedding_index.json"

    items = load_benchmark(benchmark_path)
    chunks, doc_matrix = load_embedding_index(index_path)
    line_lookup, chunks_by_id, chunk_index_by_id, func_to_file, file_to_funcs = build_helpers(chunks)

    funcs, calls = fetch_functions_and_calls()
    func_to_module, module_to_funcs = detect_modules(funcs, calls, resolution=0.5)
    func_calls = fetch_call_graph()
    graph = build_undirected_graph(func_calls)

    # Build directed graphs for callers/callees
    callers = defaultdict(set)
    callees = defaultdict(set)
    for a, b in func_calls:
        callers[b].add(a)
        callees[a].add(b)

    retriever = FastEmbeddingRetriever(index_path=index_path)
    print("Encoding questions...")
    q_embs = retriever.encode_queries([item["question"] for item in items])

    # Experiment 1: Multi-seed navigation
    nav_results = evaluate_multiseed_navigation(
        items, q_embs, func_to_module, module_to_funcs, func_to_file,
        file_to_funcs, func_calls, graph, callers, callees,
        chunk_index_by_id, doc_matrix, line_lookup, chunks_by_id,
    )
    print_multiseed_report(nav_results)

    with open(_ROOT / "results" / "multiseed_navigation.json", "w", encoding="utf-8") as f:
        json.dump(nav_results, f, ensure_ascii=False, indent=2)

    # Experiment 2: Steiner node analysis
    all_scores, all_degrees, per_question = analyze_steiner_nodes(
        items, q_embs, func_to_module, module_to_funcs, func_to_file,
        file_to_funcs, func_calls, graph, chunk_index_by_id, doc_matrix,
        line_lookup, chunks_by_id,
    )
    print_steiner_report(all_scores, all_degrees, per_question)

    with open(_ROOT / "results" / "steiner_node_analysis.json", "w", encoding="utf-8") as f:
        json.dump({
            "all_scores": all_scores,
            "all_degrees": all_degrees,
            "per_question": per_question,
        }, f, ensure_ascii=False, indent=2)

    print("\nSaved results to:")
    print("  results/multiseed_navigation.json")
    print("  results/steiner_node_analysis.json")


if __name__ == "__main__":
    main()
