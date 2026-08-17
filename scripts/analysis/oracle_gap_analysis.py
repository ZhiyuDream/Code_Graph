#!/usr/bin/env python3
"""
Oracle Gap Analysis。

目标：解释 Region-Constrained Retrieval 与 Oracle 之间的 13 个百分点差距。

核心问题：
- Region 已经覆盖了 95% 的 gold functions
- 但 Region-Constrained R@50 只有 61.2%
- Oracle（gold 所在 modules 内检索）R@50 = 74.2%

差距来源：gold functions 在 Region 内排名不够高。

本脚本分析：
1. 每个 gold function 在 Region 内的排名分布
2. 与 baseline 全库排名的对比
3. 失败案例分类（按排名区间）
4. 典型失败样例展示
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
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


def analyze_oracle_gap(
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
):
    print("Analyzing oracle gap...")

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

        # Build region
        _, region_fids = build_region(
            top3_modules, "top3_modules+callers_k5", module_to_funcs, func_to_module,
            func_to_file, file_to_funcs, func_calls, q_emb, doc_matrix, chunk_index_by_id,
        )

        # Baseline ranking (all functions)
        baseline_retrieved = retrieve_in_scope(q_emb, all_func_ids, doc_matrix, chunk_index_by_id, top_k=len(all_func_ids))
        baseline_rank = {fid: rank + 1 for rank, (fid, _) in enumerate(baseline_retrieved)}

        # Region ranking
        region_retrieved = retrieve_in_scope(q_emb, list(region_fids), doc_matrix, chunk_index_by_id, top_k=len(region_fids))
        region_rank = {fid: rank + 1 for rank, (fid, _) in enumerate(region_retrieved)}

        # Oracle ranking (gold modules only)
        gold_modules = {func_to_module.get(fid) for fid in gold_fids}
        gold_modules.discard(None)
        oracle_fids = set()
        for mid in gold_modules:
            oracle_fids.update(module_to_funcs.get(mid, []))
        oracle_retrieved = retrieve_in_scope(q_emb, list(oracle_fids), doc_matrix, chunk_index_by_id, top_k=len(oracle_fids))
        oracle_rank = {fid: rank + 1 for rank, (fid, _) in enumerate(oracle_retrieved)}

        q_result = {
            "qa_id": item["qa_id"],
            "question": item["question"],
            "gold_count": len(gold_fids),
            "gold_modules": list(gold_modules),
            "region_size": len(region_fids),
            "oracle_size": len(oracle_fids),
            "gold_details": [],
        }

        for fid in gold_fids:
            chunk = chunks_by_id.get(fid, {})
            meta = chunk.get("meta", {})
            name = meta.get("name", "")
            file_path = meta.get("file_path", "")
            module = func_to_module.get(fid, "")

            br = baseline_rank.get(fid)
            rr = region_rank.get(fid)
            orr = oracle_rank.get(fid)

            q_result["gold_details"].append({
                "fid": fid,
                "name": name,
                "file_path": file_path,
                "module": module,
                "baseline_rank": br,
                "region_rank": rr,
                "oracle_rank": orr,
                "region_covered": fid in region_fids,
            })

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


def print_report(results):
    print("\n" + "=" * 90)
    print("ORACLE GAP ANALYSIS")
    print("=" * 90)

    all_golds = []
    for r in results:
        for g in r["gold_details"]:
            all_golds.append({**g, "qa_id": r["qa_id"], "question": r["question"]})

    total = len(all_golds)
    covered = sum(1 for g in all_golds if g["region_covered"])
    print(f"\nTotal gold functions: {total}")
    print(f"Covered by region: {covered} ({covered/total*100:.1f}%)")
    print(f"Not covered by region: {total-covered} ({(total-covered)/total*100:.1f}%)")

    # 对 covered 的 gold，分析 region 内排名分布
    covered_golds = [g for g in all_golds if g["region_covered"]]

    print("\n[Region Rank Distribution for Covered Golds]")
    bins = [(1, 10), (11, 20), (21, 50), (51, 100), (101, 300), (301, 999999)]
    labels = ["1-10", "11-20", "21-50", "51-100", "101-300", ">300"]
    for (lo, hi), label in zip(bins, labels):
        count = sum(1 for g in covered_golds if lo <= g["region_rank"] <= hi)
        print(f"  Rank {label:>7}: {count} golds ({count/len(covered_golds)*100:.1f}%)")

    print("\n[Baseline vs Region Rank (for covered golds)]")
    better = sum(1 for g in covered_golds if g["region_rank"] < g["baseline_rank"])
    same = sum(1 for g in covered_golds if g["region_rank"] == g["baseline_rank"])
    worse = sum(1 for g in covered_golds if g["region_rank"] > g["baseline_rank"])
    print(f"  Better in region: {better} ({better/len(covered_golds)*100:.1f}%)")
    print(f"  Same rank: {same} ({same/len(covered_golds)*100:.1f}%)")
    print(f"  Worse in region: {worse} ({worse/len(covered_golds)*100:.1f}%)")

    print("\n[Oracle vs Region Rank (for covered golds)]")
    for g in covered_golds:
        g["rank_gap"] = g["region_rank"] - g["oracle_rank"]
    median_gap = np.median([g["rank_gap"] for g in covered_golds])
    mean_gap = np.mean([g["rank_gap"] for g in covered_golds])
    print(f"  Median rank gap (region - oracle): {median_gap:.0f}")
    print(f"  Mean rank gap (region - oracle): {mean_gap:.1f}")

    # 失败案例分析：region 覆盖了但 ranking 没进前 50
    failure_cases = [g for g in covered_golds if g["region_rank"] > 50]
    print(f"\n[Failure Cases: Covered but Rank > 50] {len(failure_cases)} golds")

    # 按排名区间分类
    print("\n  Failure rank distribution:")
    for (lo, hi), label in zip(bins, labels):
        count = sum(1 for g in failure_cases if lo <= g["region_rank"] <= hi)
        print(f"    {label:>7}: {count}")

    # 典型失败样例
    print("\n[Typical Failure Examples (covered, region rank 51-100)]")
    for g in sorted(failure_cases, key=lambda x: x["region_rank"])[:10]:
        print(f"\n  Q: {g['question'][:70]}...")
        print(f"     Gold: {g['name']} ({g['file_path']})")
        print(f"     Region rank: {g['region_rank']}, Baseline rank: {g['baseline_rank']}, Oracle rank: {g['oracle_rank']}")
        print(f"     Module: {g['module']}")

    print("\n[Typical Failure Examples (covered, region rank 101-300)]")
    for g in sorted([g for g in failure_cases if 101 <= g["region_rank"] <= 300], key=lambda x: x["region_rank"])[:10]:
        print(f"\n  Q: {g['question'][:70]}...")
        print(f"     Gold: {g['name']} ({g['file_path']})")
        print(f"     Region rank: {g['region_rank']}, Baseline rank: {g['baseline_rank']}, Oracle rank: {g['oracle_rank']}")
        print(f"     Module: {g['module']}")

    # 问题级别统计
    print("\n[Per-Question Failure Count]")
    q_failures = []
    for r in results:
        covered_total = sum(1 for g in r["gold_details"] if g["region_covered"])
        ranked_fail = sum(1 for g in r["gold_details"] if g["region_covered"] and g["region_rank"] > 50)
        q_failures.append({
            "qa_id": r["qa_id"],
            "gold_count": r["gold_count"],
            "covered": covered_total,
            "rank_fail": ranked_fail,
        })

    for qf in sorted(q_failures, key=lambda x: -x["rank_fail"])[:10]:
        print(f"  {qf['qa_id']}: {qf['rank_fail']}/{qf['covered']} covered golds ranked >50")


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

    results = analyze_oracle_gap(
        items, q_embs, func_to_module, module_to_funcs, func_to_file,
        file_to_funcs, func_calls, chunk_index_by_id, doc_matrix,
        line_lookup, chunks_by_id,
    )

    print_report(results)

    output_path = _ROOT / "results" / "oracle_gap_analysis.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\nSaved detailed results to: {output_path}")


if __name__ == "__main__":
    main()
