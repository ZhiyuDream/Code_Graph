#!/usr/bin/env python3
"""
Region Compression 实验。

目标：在保持高 gold coverage 的前提下，把 Region 的搜索空间压缩到更小。

探索多种压缩策略：
1. Directory depth：控制目录扩展的层级
2. Caller top-K：只取最相关的 K 个 caller modules
3. Directory + Caller 的组合
4. 不同 seed 选择（Top-1 module vs Top-1 directory）

同时分析不同 Question Type 下的 Region 效果。
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
from scripts.analysis.eval_module_constrained_retrieval import (
    detect_modules,
    fetch_functions_and_calls,
    load_benchmark,
    load_embedding_index,
    build_helpers,
    gold_evidence_to_function_ids,
    score_modules,
)


def fetch_call_graph():
    return [(r["caller"], r["callee"]) for r in run_cypher(
        "MATCH (a:Function)-[:CALLS]->(b:Function) RETURN a.id AS caller, b.id AS callee"
    ) if r["caller"] != r["callee"]]


def get_module_directories(module_id, module_to_funcs, func_to_file):
    dirs = set()
    for fid in module_to_funcs.get(module_id, []):
        fp = func_to_file.get(fid, "")
        if fp:
            dirs.add(str(Path(fp).parent))
    return dirs


def get_directory_hierarchy(dirs: set, depth: int = 0):
    """
    获取目录的层级扩展。
    depth=0: 只包含原始目录
    depth=1: 包含父目录
    depth=2: 包含祖父目录
    """
    expanded = set(dirs)
    current = set(dirs)
    for _ in range(depth):
        parents = set()
        for d in current:
            p = str(Path(d).parent)
            if p and p != "." and p != "/":
                parents.add(p)
        expanded.update(parents)
        current = parents
    return expanded


def expand_by_directory(
    seed_module: str,
    module_to_funcs: dict,
    func_to_file: dict,
    file_to_funcs: dict,
    depth: int = 0,
):
    """按目录层级扩展。"""
    seed_dirs = get_module_directories(seed_module, module_to_funcs, func_to_file)
    expanded_dirs = get_directory_hierarchy(seed_dirs, depth)

    region_modules = set()
    region_fids = set()

    for mid, fids in module_to_funcs.items():
        module_dirs = get_module_directories(mid, module_to_funcs, func_to_file)
        if module_dirs & expanded_dirs:
            region_modules.add(mid)
            region_fids.update(fids)

    return region_modules, region_fids


def expand_by_callers(
    seed_module: str,
    module_to_funcs: dict,
    func_to_module: dict,
    func_calls: list[tuple],
    q_emb: np.ndarray,
    doc_matrix: np.ndarray,
    chunk_index_by_id: dict,
    top_k: int = 10,
    direction: str = "callers",
):
    """
    按调用关系扩展，只取 top-K 最相关的 caller/callee modules。
    """
    seed_fids = set(module_to_funcs.get(seed_module, []))

    neighbor_modules = defaultdict(list)  # module -> list of (function, sim)

    for a, b in func_calls:
        if direction == "callers" and b in seed_fids:
            caller_module = func_to_module.get(a)
            if caller_module and caller_module != seed_module:
                idx = chunk_index_by_id.get(a)
                if idx is not None:
                    sim = float(doc_matrix[idx] @ q_emb)
                    neighbor_modules[caller_module].append((a, sim))
        elif direction == "callees" and a in seed_fids:
            callee_module = func_to_module.get(b)
            if callee_module and callee_module != seed_module:
                idx = chunk_index_by_id.get(b)
                if idx is not None:
                    sim = float(doc_matrix[idx] @ q_emb)
                    neighbor_modules[callee_module].append((b, sim))

    # 给每个 neighbor module 打分：内部函数与 question 的最大相似度
    scored_modules = []
    for mid, func_sims in neighbor_modules.items():
        max_sim = max(sim for _, sim in func_sims)
        scored_modules.append((mid, max_sim))

    scored_modules.sort(key=lambda x: -x[1])
    selected_modules = {mid for mid, _ in scored_modules[:top_k]}

    region_fids = set()
    for mid in selected_modules:
        region_fids.update(module_to_funcs.get(mid, []))

    return selected_modules, region_fids


def build_region(
    seed_modules: list[str],
    strategy: str,
    module_to_funcs: dict,
    func_to_module: dict,
    func_to_file: dict,
    file_to_funcs: dict,
    func_calls: list[tuple],
    q_emb: np.ndarray,
    doc_matrix: np.ndarray,
    chunk_index_by_id: dict,
):
    """根据策略构建 region。"""
    region_modules = set(seed_modules)
    region_fids = set()
    for sm in seed_modules:
        region_fids.update(module_to_funcs.get(sm, []))

    if strategy == "seed_only" or strategy == "top3_modules":
        return region_modules, region_fids

    # 解析组合策略，例如 "dir_d1+callers_k5"
    parts = strategy.split("+")

    for part in parts:
        if part == "top3_modules":
            continue

        if part.startswith("dir_d"):
            depth = int(part.split("_d")[1])
            for sm in seed_modules:
                mods, fids = expand_by_directory(sm, module_to_funcs, func_to_file, file_to_funcs, depth)
                region_modules.update(mods)
                region_fids.update(fids)

        elif part.startswith("callers_k"):
            k = int(part.split("_k")[1])
            for sm in seed_modules:
                mods, fids = expand_by_callers(
                    sm, module_to_funcs, func_to_module, func_calls,
                    q_emb, doc_matrix, chunk_index_by_id, top_k=k, direction="callers",
                )
                region_modules.update(mods)
                region_fids.update(fids)

        elif part.startswith("callees_k"):
            k = int(part.split("_k")[1])
            for sm in seed_modules:
                mods, fids = expand_by_callers(
                    sm, module_to_funcs, func_to_module, func_calls,
                    q_emb, doc_matrix, chunk_index_by_id, top_k=k, direction="callees",
                )
                region_modules.update(mods)
                region_fids.update(fids)

    return region_modules, region_fids


def classify_question_type(gold_fids, gold_modules, func_to_file, module_to_funcs):
    """根据 gold evidence 的分布分类问题类型。"""
    if len(gold_fids) == 1:
        return "single_function"

    if len(gold_modules) == 1:
        return "single_module"

    # 检查是否跨目录
    dirs = set()
    for fid in gold_fids:
        fp = func_to_file.get(fid, "")
        if fp:
            dirs.add(str(Path(fp).parent))
    if len(dirs) == 1:
        return "cross_module_same_dir"

    # 检查是否共享父目录
    parents = set()
    for d in dirs:
        parents.add(str(Path(d).parent))
    if len(parents) == 1:
        return "cross_dir_same_parent"

    return "cross_parent"


def evaluate_strategies(
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
    strategies: list[str],
):
    results = []

    for item, q_emb in zip(items, q_embs):
        gold_fids = gold_evidence_to_function_ids(item, line_lookup, chunks_by_id)
        if not gold_fids:
            continue

        gold_modules = {func_to_module.get(fid) for fid in gold_fids}
        gold_modules.discard(None)

        if not gold_modules:
            continue

        ranked_modules = score_modules(q_emb, module_to_funcs, doc_matrix, chunk_index_by_id, method="max")
        seed_module = ranked_modules[0][0] if ranked_modules else None
        top3_modules = [mid for mid, _ in ranked_modules[:3]]

        if not seed_module:
            continue

        q_type = classify_question_type(gold_fids, gold_modules, func_to_file, module_to_funcs)

        q_result = {
            "qa_id": item["qa_id"],
            "question": item["question"],
            "question_type": q_type,
            "gold_count": len(gold_fids),
            "gold_modules": list(gold_modules),
            "gold_module_count": len(gold_modules),
            "seed_module": seed_module,
            "top3_modules": top3_modules,
        }

        for strategy in strategies:
            seeds = top3_modules if strategy.startswith("top3") else [seed_module]
            region_modules, region_fids = build_region(
                seeds, strategy, module_to_funcs, func_to_module,
                func_to_file, file_to_funcs, func_calls, q_emb, doc_matrix, chunk_index_by_id,
            )

            covered_fids = gold_fids & region_fids
            covered_modules = gold_modules & region_modules

            q_result[strategy] = {
                "function_coverage": len(covered_fids) / len(gold_fids),
                "module_coverage": len(covered_modules) / len(gold_modules),
                "full_coverage": len(covered_fids) == len(gold_fids),
                "has_any_gold": len(covered_fids) > 0,
                "function_count": len(region_fids),
                "module_count": len(region_modules),
                "file_count": len({func_to_file.get(fid, "") for fid in region_fids if func_to_file.get(fid)}),
            }

        results.append(q_result)

    return results


def print_report(results: list[dict], strategies: list[str]):
    print("\n" + "=" * 90)
    print("REGION COMPRESSION REPORT")
    print("=" * 90)

    print("\n[Compression Strategies Comparison]")
    print(f"{'Strategy':<30} {'Funcs':<10} {'Files':<10} {'Modules':<10} {'Mean Cov':<12} {'Full Cov':<12} {'Any Gold':<10}")
    for s in strategies:
        funcs = np.mean([r[s]["function_count"] for r in results])
        files = np.mean([r[s]["file_count"] for r in results])
        mods = np.mean([r[s]["module_count"] for r in results])
        mean_cov = np.mean([r[s]["function_coverage"] for r in results])
        full_cov = np.mean([r[s]["full_coverage"] for r in results])
        any_gold = np.mean([r[s]["has_any_gold"] for r in results])
        print(f"{s:<30} {funcs:<10.0f} {files:<10.0f} {mods:<10.0f} {mean_cov*100:<11.1f}% {full_cov*100:<11.1f}% {any_gold*100:<9.1f}%")

    print("\n[Strategies with Mean Coverage > 90%]")
    high_cov = [(s, np.mean([r[s]["function_count"] for r in results]),
                 np.mean([r[s]["function_coverage"] for r in results]),
                 np.mean([r[s]["full_coverage"] for r in results]))
                for s in strategies
                if np.mean([r[s]["function_coverage"] for r in results]) > 0.9]
    high_cov.sort(key=lambda x: x[1])
    for s, funcs, cov, full in high_cov:
        print(f"  {s:<30} funcs={funcs:>6.0f}  mean_cov={cov*100:>5.1f}%  full_cov={full*100:>5.1f}%")

    # Question type analysis
    print("\n[Question Type Distribution]")
    type_counts = defaultdict(int)
    for r in results:
        type_counts[r["question_type"]] += 1
    for t, c in sorted(type_counts.items(), key=lambda x: -x[1]):
        print(f"  {t:<30}: {c} questions ({c/len(results)*100:.1f}%)")

    print("\n[Region Coverage by Question Type (dir_d0)]")
    for q_type in sorted(type_counts.keys()):
        subset = [r for r in results if r["question_type"] == q_type]
        cov = np.mean([r["dir_d0"]["function_coverage"] for r in subset])
        size = np.mean([r["dir_d0"]["function_count"] for r in subset])
        print(f"  {q_type:<30}: coverage={cov*100:>5.1f}%  size={size:>6.0f}  n={len(subset)}")


if __name__ == "__main__":
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

    strategies = [
        "seed_only",
        "top3_modules",
        "top3_modules+callers_k5",
        "top3_modules+dir_d0",
        "dir_d0",  # same directory
        "dir_d1",  # parent directory
        "callers_k3",
        "callers_k5",
        "callers_k10",
        "callees_k3",
        "callees_k5",
        "dir_d0+callers_k3",
        "dir_d0+callers_k5",
        "dir_d0+callers_k10",
        "dir_d1+callers_k3",
        "dir_d1+callers_k5",
        "dir_d1+callers_k10",
    ]

    results = evaluate_strategies(
        items, q_embs, func_to_module, module_to_funcs, func_to_file,
        file_to_funcs, func_calls, chunk_index_by_id, doc_matrix,
        line_lookup, chunks_by_id, strategies,
    )

    print_report(results, strategies)

    output_path = _ROOT / "results" / "region_compression.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\nSaved detailed results to: {output_path}")
