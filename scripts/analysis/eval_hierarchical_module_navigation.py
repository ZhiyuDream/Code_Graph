#!/usr/bin/env python3
"""
评估 Hierarchical Module Navigation。

对比：
1. baseline: 全库 function retrieval
2. region: top3_modules + callers_k5 region 内 retrieval
3. module_topk: HierarchicalModuleNavigator with module_topk mode
4. llm_select: HierarchicalModuleNavigator with LLM module selection
5. hierarchical: LLM select + 1-hop call graph expansion

指标：
- Recall@K
- Full Coverage@K
- Candidate count / visited functions
- MRR
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from src.core.module_abstraction import ModuleAbstraction
from src.qa.investigation.hierarchical_module import HierarchicalModuleNavigator
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


def evaluate(
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
    navigator: HierarchicalModuleNavigator,
):
    results = []
    all_func_ids = list(chunk_index_by_id.keys())

    for idx, (item, q_emb) in enumerate(zip(items, q_embs), 1):
        print(f"[{idx}/{len(items)}] Evaluating {item['qa_id']}...", flush=True)
        gold_fids = gold_evidence_to_function_ids(item, line_lookup, chunks_by_id)
        if not gold_fids:
            print("  no gold fids, skipping")
            continue

        q_result = {
            "qa_id": item["qa_id"],
            "question": item["question"],
            "gold_count": len(gold_fids),
        }

        # Baseline
        baseline_top = get_top_k(q_emb, all_func_ids, doc_matrix, chunk_index_by_id, top_k=100)
        q_result["baseline"] = evaluate_scope(gold_fids, baseline_top, len(all_func_ids), func_to_file)

        # Region
        ranked_modules = score_modules(q_emb, module_to_funcs, doc_matrix, chunk_index_by_id, method="max")
        top3_modules = [mid for mid, _ in ranked_modules[:3]]
        _, region_fids = build_region(
            top3_modules, "top3_modules+callers_k5", module_to_funcs, func_to_module,
            func_to_file, file_to_funcs, func_calls, q_emb, doc_matrix, chunk_index_by_id,
        )
        region_fids = set(region_fids)
        region_top = get_top_k(q_emb, list(region_fids), doc_matrix, chunk_index_by_id, top_k=100)
        q_result["region"] = evaluate_scope(gold_fids, region_top, len(region_fids), func_to_file)

        # Hierarchical module navigation
        for mode, max_modules, expand_hops in [
            ("module_topk", 3, 0),
            ("module_topk", 5, 0),
            ("llm_select", 3, 0),
            ("hierarchical", 3, 1),
        ]:
            key = f"{mode}_m{max_modules}_h{expand_hops}"
            print(f"  running {key}...", flush=True)
            try:
                nav_result = navigator.investigate(
                    item["question"],
                    mode=mode,
                    max_modules=max_modules,
                    max_functions=100,
                    expand_hops=expand_hops,
                )
                candidate_files = set()
                for m in nav_result["selected_modules"]:
                    # 从 module id 反查 file paths
                    mod = navigator.ma.modules.get(m["id"])
                    if mod:
                        candidate_files.update(mod.file_paths)

                # 估算 candidate function count：module 内 functions + expanded
                # 这里简化为用 file_filter 后的实际 candidate 数量
                candidate_fids_from_files = [
                    ch.get("id", "") for ch in navigator.retriever.chunks
                    if ch.get("meta", {}).get("file_path") in candidate_files
                ]

                retrieved_fids = [f"{r['metadata']['file_path']}:{r['metadata']['name']}:{r['metadata']['start_line']}"
                                  for r in nav_result["function_results"]]
                # 为了和 gold_fids 比较，需要把 retrieved_fids 转成和 gold 一样的格式
                # gold_fids 已经是 file_path:name:start_line 格式
                q_result[key] = evaluate_scope(
                    gold_fids,
                    [(fid, 0.0) for fid in retrieved_fids],
                    len(candidate_fids_from_files),
                    func_to_file,
                )
                q_result[key]["selected_modules"] = [m["name"] for m in nav_result["selected_modules"]]
            except Exception as e:
                print(f"Error in {key} for {item['qa_id']}: {e}")
                q_result[key] = {
                    "scope_size": 0,
                    "recall@5": 0, "recall@10": 0, "recall@20": 0,
                    "recall@50": 0, "recall@100": 0,
                    "mrr": 0, "first_gold_rank": None,
                }

        results.append(q_result)

    return results


def print_report(results):
    print("\n" + "=" * 100)
    print("HIERARCHICAL MODULE NAVIGATION EVALUATION")
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

    print("\n[Full Coverage@K]")
    header = f"{'Config':<35}"
    for k in [5, 10, 20, 50, 100]:
        header += f" FC@{k:<3}"
    print(header)
    for name in config_names:
        line = f"{name:<35}"
        for k in [5, 10, 20, 50, 100]:
            mean_fc = np.mean([r[name][f"full_coverage@{k}"] for r in results])
            line += f" {mean_fc*100:5.1f}"
        print(line)

    print("\n[MRR & Median First Gold Rank]")
    print(f"{'Config':<35} {'MRR':<10} {'Median Rank':<15}")
    for name in config_names:
        mrr = np.mean([r[name]["mrr"] for r in results])
        ranks = [r[name]["first_gold_rank"] for r in results if r[name]["first_gold_rank"] is not None]
        median_r = np.median(ranks) if ranks else 0
        print(f"{name:<35} {mrr:<10.3f} {median_r:<15.0f}")

    print("\n[Examples: LLM-selected modules]")
    for r in results[:5]:
        if "llm_select_m3_h0" in r:
            print(f"\n  {r['qa_id']}: {r['question'][:60]}...")
            print(f"    Selected: {r['llm_select_m3_h0'].get('selected_modules', [])}")


def main():
    benchmark_path = _ROOT / "datasets" / "benchmark_hard.json"
    index_path = _ROOT / "data" / "qa_embedding_index.json"
    cache_path = _ROOT / "data" / "module_abstraction.json"

    items = load_benchmark(benchmark_path)
    chunks, doc_matrix = load_embedding_index(index_path)
    line_lookup, chunks_by_id, chunk_index_by_id, func_to_file, file_to_funcs = build_helpers(chunks)

    funcs, calls = fetch_functions_and_calls()
    func_to_module, module_to_funcs = detect_modules(funcs, calls, resolution=0.5)
    func_calls = fetch_call_graph()

    retriever = FastEmbeddingRetriever(index_path=index_path)
    print("Encoding questions...")
    q_embs = retriever.encode_queries([item["question"] for item in items])

    # Load or build module abstraction
    ma = ModuleAbstraction(cache_path=str(cache_path), model="gpt-4.1-mini")
    if not ma.load():
        print("Module abstraction not found. Building...")
        ma.build_from_neo4j()
        ma.save()

    navigator = HierarchicalModuleNavigator(module_abstraction=ma, model="gpt-4.1-mini")

    results = evaluate(
        items, q_embs, func_to_module, module_to_funcs, func_to_file,
        file_to_funcs, func_calls, chunk_index_by_id, doc_matrix,
        line_lookup, chunks_by_id, navigator,
    )

    print_report(results)

    output_path = _ROOT / "results" / "hierarchical_module_navigation.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\nSaved detailed results to: {output_path}")


if __name__ == "__main__":
    main()
