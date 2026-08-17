#!/usr/bin/env python3
"""
Region-Constrained Function Retrieval 实验。

核心问题：Region Coverage 的提升能否转化为 Function Retrieval 的提升？

对比四个 scope：
1. baseline：全库 17,458 函数
2. top3_modules：Top-3 modules 内检索
3. top3_modules+callers_k5：Region 内检索
4. directory：Top directory（seed module 所在目录）内检索

指标：
- Recall@10/20/50/100
- Full Coverage@K
- MRR
- Median First Gold Rank
- CDF: P@K
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
    expand_by_directory,
    expand_by_callers,
    fetch_call_graph,
    fetch_functions_and_calls,
    get_module_directories,
    load_benchmark,
    load_embedding_index,
    build_helpers,
    gold_evidence_to_function_ids,
    score_modules,
)


def retrieve_in_scope(q_emb, candidate_fids, doc_matrix, chunk_index_by_id, top_k=50):
    idxs = [chunk_index_by_id[fid] for fid in candidate_fids if fid in chunk_index_by_id]
    if not idxs:
        return []
    sub = doc_matrix[idxs]
    sims = sub @ q_emb
    order = np.argsort(-sims)[:top_k]
    return [(candidate_fids[i], float(sims[i])) for i in order]


def evaluate_retrieval(
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
    top_k_values=(5, 10, 20, 50, 100),
):
    print("Evaluating region-constrained retrieval...")

    results = []
    all_func_ids = list(chunk_index_by_id.keys())

    for item, q_emb in zip(items, q_embs):
        gold_fids = gold_evidence_to_function_ids(item, line_lookup, chunks_by_id)
        if not gold_fids:
            continue

        ranked_modules = score_modules(q_emb, module_to_funcs, doc_matrix, chunk_index_by_id, method="max")
        seed_module = ranked_modules[0][0] if ranked_modules else None
        top3_modules = [mid for mid, _ in ranked_modules[:3]]

        if not seed_module:
            continue

        # 构建不同 scope 的候选函数集合
        scopes = {}

        # Baseline
        scopes["baseline"] = set(all_func_ids)

        # Top-3 modules
        fids = set()
        for mid in top3_modules:
            fids.update(module_to_funcs.get(mid, []))
        scopes["top3_modules"] = fids

        # Region: top3_modules + callers_k5
        region_modules, region_fids = build_region(
            top3_modules, "top3_modules+callers_k5", module_to_funcs, func_to_module,
            func_to_file, file_to_funcs, func_calls, q_emb, doc_matrix, chunk_index_by_id,
        )
        scopes["top3_modules+callers_k5"] = region_fids

        # Directory: seed module 所在目录的所有函数
        seed_dirs = get_module_directories(seed_module, module_to_funcs, func_to_file)
        dir_fids = set()
        for fp in file_to_funcs:
            if str(Path(fp).parent) in seed_dirs:
                dir_fids.update(file_to_funcs[fp])
        scopes["directory_seed"] = dir_fids

        # Oracle region: gold 所在 modules 的 union
        gold_modules = {func_to_module.get(fid) for fid in gold_fids}
        gold_modules.discard(None)
        oracle_fids = set()
        for mid in gold_modules:
            oracle_fids.update(module_to_funcs.get(mid, []))
        scopes["oracle_modules"] = oracle_fids

        q_result = {
            "qa_id": item["qa_id"],
            "question": item["question"],
            "gold_count": len(gold_fids),
            "gold_modules": list(gold_modules),
        }

        for scope_name, candidate_fids in scopes.items():
            retrieved = retrieve_in_scope(q_emb, list(candidate_fids), doc_matrix, chunk_index_by_id, top_k=max(top_k_values))
            retrieved_fids = [fid for fid, _ in retrieved]

            recall = {}
            full_cov = {}
            has_gold = {}
            for k in top_k_values:
                top_k_set = set(retrieved_fids[:k])
                covered = gold_fids & top_k_set
                recall[k] = len(covered) / len(gold_fids)
                full_cov[k] = len(covered) == len(gold_fids)
                has_gold[k] = len(covered) > 0

            first_gold_rank = None
            for rank, fid in enumerate(retrieved_fids, 1):
                if fid in gold_fids:
                    first_gold_rank = rank
                    break
            mrr = 1.0 / first_gold_rank if first_gold_rank else 0.0

            q_result[scope_name] = {
                "scope_size": len(candidate_fids),
                "file_count": len({func_to_file.get(fid, "") for fid in candidate_fids if func_to_file.get(fid)}),
                "recall": recall,
                "full_coverage": full_cov,
                "has_gold": has_gold,
                "first_gold_rank": first_gold_rank,
                "mrr": mrr,
            }

        results.append(q_result)

    return results


def print_report(results, top_k_values):
    print("\n" + "=" * 95)
    print("REGION-CONSTRAINED RETRIEVAL REPORT")
    print("=" * 95)

    scope_names = ["baseline", "top3_modules", "top3_modules+callers_k5", "directory_seed", "oracle_modules"]

    print("\n[Scope Size]")
    print(f"{'Scope':<30} {'Funcs(mean)':<15} {'Files(mean)':<15}")
    for s in scope_names:
        funcs = np.mean([r[s]["scope_size"] for r in results])
        files = np.mean([r[s]["file_count"] for r in results])
        print(f"{s:<30} {funcs:<15.0f} {files:<15.0f}")

    print("\n[Recall@K]")
    header = f"{'Scope':<30}"
    for k in top_k_values:
        header += f" R@{k:<4}"
    print(header)
    for s in scope_names:
        line = f"{s:<30}"
        for k in top_k_values:
            mean_recall = np.mean([r[s]["recall"][k] for r in results])
            line += f" {mean_recall*100:5.1f}"
        print(line)

    print("\n[CDF: % questions with at least one gold in top-k]")
    header = f"{'Scope':<30}"
    for k in top_k_values:
        header += f" P@{k:<4}"
    print(header)
    for s in scope_names:
        line = f"{s:<30}"
        for k in top_k_values:
            mean_p = np.mean([r[s]["has_gold"][k] for r in results])
            line += f" {mean_p*100:5.1f}"
        print(line)

    print("\n[Full Coverage@K]")
    header = f"{'Scope':<30}"
    for k in top_k_values:
        header += f" FC@{k:<3}"
    print(header)
    for s in scope_names:
        line = f"{s:<30}"
        for k in top_k_values:
            mean_fc = np.mean([r[s]["full_coverage"][k] for r in results])
            line += f" {mean_fc*100:5.1f}"
        print(line)

    print("\n[MRR & Median First Gold Rank]")
    print(f"{'Scope':<30} {'MRR':<10} {'Median Rank':<15}")
    for s in scope_names:
        mrr = np.mean([r[s]["mrr"] for r in results])
        ranks = [r[s]["first_gold_rank"] for r in results if r[s]["first_gold_rank"] is not None]
        median_rank = np.median(ranks) if ranks else 0
        print(f"{s:<30} {mrr:<10.3f} {median_rank:<15.0f}")


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

    top_k_values = (5, 10, 20, 50, 100)
    results = evaluate_retrieval(
        items, q_embs, func_to_module, module_to_funcs, func_to_file,
        file_to_funcs, func_calls, chunk_index_by_id, doc_matrix,
        line_lookup, chunks_by_id, top_k_values,
    )

    print_report(results, top_k_values)

    output_path = _ROOT / "results" / "region_constrained_retrieval.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\nSaved detailed results to: {output_path}")


if __name__ == "__main__":
    main()
