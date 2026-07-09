#!/usr/bin/env python3
"""
Module-Constrained Retrieval 误差分解。

把 gold function 丢失的原因拆成两类：
1. Module Selection Error：gold function 所在 module 没被 Top-K modules 覆盖
2. Intra-Module Ranking Error：module 已被覆盖，但 gold function 在 module 内排名靠后

同时对比 baseline，看模块过滤到底损失了什么。
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.core.neo4j_client import run_cypher
from src.qa.retrievers.fast_embedding import FastEmbeddingRetriever
from scripts.analysis.eval_module_constrained_retrieval import (
    detect_modules,
    fetch_functions_and_calls,
    load_benchmark,
    load_embedding_index,
    build_helpers,
    gold_evidence_to_function_ids,
    score_modules,
)


def analyze_error_decomposition(
    items,
    q_embs,
    func_to_module,
    module_to_funcs,
    func_to_file,
    file_to_funcs,
    all_func_ids,
    doc_matrix,
    chunk_index_by_id,
    line_lookup,
    chunks_by_id,
    top_m_values=(1, 3, 5),
    top_k=50,
    scoring_method="max",
):
    print(f"\nAnalyzing error decomposition (scoring={scoring_method}, top_k={top_k})...")

    results = []

    for item, q_emb in zip(items, q_embs):
        gold_fids = gold_evidence_to_function_ids(item, line_lookup, chunks_by_id)
        if not gold_fids:
            continue

        ranked_modules = score_modules(
            q_emb, module_to_funcs, doc_matrix, chunk_index_by_id, method=scoring_method
        )

        # Baseline rank for each gold function
        baseline_retrieved = retrieve_in_scope(q_emb, all_func_ids, doc_matrix, chunk_index_by_id, top_k=len(all_func_ids))
        baseline_rank = {fid: rank + 1 for rank, (fid, _) in enumerate(baseline_retrieved)}

        q_result = {
            "qa_id": item["qa_id"],
            "gold_count": len(gold_fids),
            "gold_fids": list(gold_fids),
        }

        for top_m in top_m_values:
            top_modules = [mid for mid, _ in ranked_modules[:top_m]]
            top_module_set = set(top_modules)

            module_scope_fids = set()
            for mid in top_modules:
                module_scope_fids.update(module_to_funcs[mid])

            module_retrieved = retrieve_in_scope(
                q_emb, list(module_scope_fids), doc_matrix, chunk_index_by_id, top_k=top_k
            )
            module_rank = {fid: rank + 1 for rank, (fid, _) in enumerate(module_retrieved)}

            selection_error = 0
            ranking_error = 0
            recovered = 0

            error_details = []

            for fid in gold_fids:
                gold_module = func_to_module.get(fid)
                baseline_r = baseline_rank.get(fid)

                if gold_module not in top_module_set:
                    selection_error += 1
                    error_details.append({
                        "fid": fid,
                        "type": "module_selection_error",
                        "gold_module": gold_module,
                        "baseline_rank": baseline_r,
                    })
                elif fid not in module_rank or module_rank[fid] > top_k:
                    ranking_error += 1
                    error_details.append({
                        "fid": fid,
                        "type": "intra_module_ranking_error",
                        "gold_module": gold_module,
                        "baseline_rank": baseline_r,
                        "module_scope_size": len(module_scope_fids),
                        "module_rank": module_rank.get(fid),
                    })
                else:
                    recovered += 1
                    error_details.append({
                        "fid": fid,
                        "type": "recovered",
                        "gold_module": gold_module,
                        "baseline_rank": baseline_r,
                        "module_rank": module_rank[fid],
                    })

            q_result[f"module_m{top_m}"] = {
                "selection_error": selection_error,
                "ranking_error": ranking_error,
                "recovered": recovered,
                "scope_size": len(module_scope_fids),
                "error_details": error_details,
            }

        results.append(q_result)

    return results


def retrieve_in_scope(q_emb, candidate_fids, doc_matrix, chunk_index_by_id, top_k=50):
    idxs = [chunk_index_by_id[fid] for fid in candidate_fids if fid in chunk_index_by_id]
    if not idxs:
        return []
    sub = doc_matrix[idxs]
    sims = sub @ q_emb
    order = np.argsort(-sims)[:top_k]
    return [(candidate_fids[i], float(sims[i])) for i in order]


def aggregate_and_print(results, top_m_values, top_k):
    print("\n" + "=" * 75)
    print("MODULE ERROR DECOMPOSITION")
    print("=" * 75)

    for top_m in top_m_values:
        key = f"module_m{top_m}"

        total_gold = sum(r["gold_count"] for r in results)
        selection_errors = sum(r[key]["selection_error"] for r in results)
        ranking_errors = sum(r[key]["ranking_error"] for r in results)
        recovered = sum(r[key]["recovered"] for r in results)

        print(f"\n[Top-{top_m} Modules, Top-{top_k} Functions]")
        print(f"  Total gold functions: {total_gold}")
        print(f"  Recovered:            {recovered} ({recovered/total_gold*100:.1f}%)")
        print(f"  Module Selection Error: {selection_errors} ({selection_errors/total_gold*100:.1f}%)")
        print(f"  Intra-Module Ranking Error: {ranking_errors} ({ranking_errors/total_gold*100:.1f}%)")

        # Per-question breakdown
        q_with_selection_error = sum(1 for r in results if r[key]["selection_error"] > 0)
        q_with_ranking_error = sum(1 for r in results if r[key]["ranking_error"] > 0)
        print(f"  Questions with selection error: {q_with_selection_error}/{len(results)}")
        print(f"  Questions with intra-module ranking error: {q_with_ranking_error}/{len(results)}")

    # Distribution of error types per question
    print("\n[Error Pattern Distribution per Question]")
    for top_m in top_m_values:
        key = f"module_m{top_m}"
        patterns = Counter()
        for r in results:
            se = r[key]["selection_error"]
            re = r[key]["ranking_error"]
            patterns[(se, re)] += 1

        print(f"\n  Top-{top_m} Modules:")
        for (se, re), count in sorted(patterns.items()):
            print(f"    selection_error={se}, ranking_error={re}: {count} questions")

    # Examples
    print("\n[Examples: Questions with only selection error]")
    for r in results:
        if r["module_m3"]["selection_error"] > 0 and r["module_m3"]["ranking_error"] == 0:
            print(f"  {r['qa_id']}: {r['gold_count']} gold, "
                  f"selection_error={r['module_m3']['selection_error']}, "
                  f"scope_size={r['module_m3']['scope_size']}")
            break

    print("\n[Examples: Questions with only intra-module ranking error]")
    for r in results:
        if r["module_m3"]["selection_error"] == 0 and r["module_m3"]["ranking_error"] > 0:
            print(f"  {r['qa_id']}: {r['gold_count']} gold, "
                  f"ranking_error={r['module_m3']['ranking_error']}, "
                  f"scope_size={r['module_m3']['scope_size']}")
            break


def main():
    benchmark_path = _ROOT / "datasets" / "benchmark_hard.json"
    index_path = _ROOT / "data" / "qa_embedding_index.json"

    items = load_benchmark(benchmark_path)
    chunks, doc_matrix = load_embedding_index(index_path)
    line_lookup, chunks_by_id, chunk_index_by_id, func_to_file, file_to_funcs = build_helpers(chunks)

    funcs, calls = fetch_functions_and_calls()
    func_to_module, module_to_funcs = detect_modules(funcs, calls, resolution=0.5)

    retriever = FastEmbeddingRetriever(index_path=index_path)
    all_func_ids = list(chunk_index_by_id.keys())

    print("Encoding questions...")
    q_embs = retriever.encode_queries([item["question"] for item in items])

    results = analyze_error_decomposition(
        items, q_embs, func_to_module, module_to_funcs, func_to_file, file_to_funcs,
        all_func_ids, doc_matrix, chunk_index_by_id, line_lookup, chunks_by_id,
        top_m_values=(1, 3, 5), top_k=50, scoring_method="max",
    )

    aggregate_and_print(results, top_m_values=(1, 3, 5), top_k=50)

    # Save
    output_path = _ROOT / "results" / "module_error_decomposition.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\nSaved detailed results to: {output_path}")


if __name__ == "__main__":
    main()
