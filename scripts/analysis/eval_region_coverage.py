#!/usr/bin/env python3
"""
Local Repository Region Coverage 实验。

核心问题：如果把检索范围从单个 Module 扩展为 "Region"，
能否更好地覆盖 gold evidence？

Region 定义（纯规则，无 LLM）：
  Region(seed_module) =
    seed_module
    + same_directory_modules    （与 seed module 文件同目录的 modules）
    + shared_caller_modules     （调用 seed module 的函数的 modules）

对比 baseline：
  - Top-1 module
  - Top-3 modules
  - Top-5 modules
  - Directory scope（与 seed module 同目录的所有 functions）

指标：
  - Gold Function Coverage: region 内包含的 gold functions 比例
  - Gold Module Coverage: region 内包含的 gold modules 比例
  - Region size: functions / files / modules 数量
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
    """获取完整 function-level 调用图。"""
    return [(r["caller"], r["callee"]) for r in run_cypher(
        "MATCH (a:Function)-[:CALLS]->(b:Function) RETURN a.id AS caller, b.id AS callee"
    ) if r["caller"] != r["callee"]]


def get_module_directories(module_id: str, module_to_funcs: dict, func_to_file: dict):
    """返回 module 中所有文件所在的目录集合。"""
    dirs = set()
    for fid in module_to_funcs.get(module_id, []):
        fp = func_to_file.get(fid, "")
        if fp:
            dirs.add(str(Path(fp).parent))
    return dirs


def expand_region(
    seed_module: str,
    func_to_module: dict,
    module_to_funcs: dict,
    func_to_file: dict,
    file_to_funcs: dict,
    func_calls: list[tuple],
    expand_dir: bool = True,
    expand_caller: bool = True,
) -> dict:
    """
    从 seed module 出发，扩展得到 Local Repository Region。

    扩展规则：
    1. same_directory: 加入与 seed module 文件在同一目录的 modules
    2. shared_caller: 加入调用 seed module 的函数的 modules
    """
    region_modules = {seed_module}
    expansion_log = {"seed": seed_module, "same_dir": [], "shared_callers": []}

    if expand_dir:
        seed_dirs = get_module_directories(seed_module, module_to_funcs, func_to_file)
        for mid, fids in module_to_funcs.items():
            if mid == seed_module:
                continue
            module_dirs = get_module_directories(mid, module_to_funcs, func_to_file)
            if seed_dirs & module_dirs:
                region_modules.add(mid)
                expansion_log["same_dir"].append(mid)

    if expand_caller:
        seed_fids = set(module_to_funcs.get(seed_module, []))
        caller_modules = set()
        for caller, callee in func_calls:
            if callee in seed_fids:
                caller_module = func_to_module.get(caller)
                if caller_module and caller_module != seed_module:
                    caller_modules.add(caller_module)
        region_modules.update(caller_modules)
        expansion_log["shared_callers"] = list(caller_modules)

    region_fids = set()
    for mid in region_modules:
        region_fids.update(module_to_funcs.get(mid, []))

    return {
        "modules": region_modules,
        "functions": region_fids,
        "expansion": expansion_log,
    }


def evaluate_region_coverage(
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
    scoring_method="max",
):
    print("Evaluating region coverage...")

    results = []

    for item, q_emb in zip(items, q_embs):
        gold_fids = gold_evidence_to_function_ids(item, line_lookup, chunks_by_id)
        if not gold_fids:
            continue

        gold_modules = {func_to_module.get(fid) for fid in gold_fids}
        gold_modules.discard(None)

        if not gold_modules:
            continue

        ranked_modules = score_modules(
            q_emb, module_to_funcs, doc_matrix, chunk_index_by_id, method=scoring_method
        )

        top1_module = ranked_modules[0][0] if ranked_modules else None
        top3_modules = {mid for mid, _ in ranked_modules[:3]}
        top5_modules = {mid for mid, _ in ranked_modules[:5]}

        # 各种 scope
        scopes = {}

        # Top-1 module
        if top1_module:
            scopes["top1_module"] = {
                "modules": {top1_module},
                "functions": set(module_to_funcs.get(top1_module, [])),
            }

        # Top-3 modules
        fids = set()
        for mid in top3_modules:
            fids.update(module_to_funcs.get(mid, []))
        scopes["top3_modules"] = {"modules": top3_modules, "functions": fids}

        # Top-5 modules
        fids = set()
        for mid in top5_modules:
            fids.update(module_to_funcs.get(mid, []))
        scopes["top5_modules"] = {"modules": top5_modules, "functions": fids}

        # Region: seed + same_dir + shared_callers
        if top1_module:
            region = expand_region(
                top1_module, func_to_module, module_to_funcs, func_to_file,
                file_to_funcs, func_calls, expand_dir=True, expand_caller=True,
            )
            scopes["region_full"] = {
                "modules": region["modules"],
                "functions": region["functions"],
                "expansion": region["expansion"],
            }

            # Region: seed + same_dir only
            region_dir = expand_region(
                top1_module, func_to_module, module_to_funcs, func_to_file,
                file_to_funcs, func_calls, expand_dir=True, expand_caller=False,
            )
            scopes["region_dir_only"] = {
                "modules": region_dir["modules"],
                "functions": region_dir["functions"],
            }

            # Region: seed + shared_callers only
            region_caller = expand_region(
                top1_module, func_to_module, module_to_funcs, func_to_file,
                file_to_funcs, func_calls, expand_dir=False, expand_caller=True,
            )
            scopes["region_caller_only"] = {
                "modules": region_caller["modules"],
                "functions": region_caller["functions"],
            }

        # Directory scope: seed module 所在目录的所有 functions
        if top1_module:
            seed_dirs = get_module_directories(top1_module, module_to_funcs, func_to_file)
            dir_fids = set()
            for fp in file_to_funcs:
                if str(Path(fp).parent) in seed_dirs:
                    dir_fids.update(file_to_funcs[fp])
            scopes["directory_seed"] = {
                "modules": set(),  # 不计算
                "functions": dir_fids,
            }

        # 计算指标
        q_result = {
            "qa_id": item["qa_id"],
            "question": item["question"],
            "gold_count": len(gold_fids),
            "gold_modules": list(gold_modules),
            "gold_module_count": len(gold_modules),
        }

        for scope_name, scope in scopes.items():
            covered_fids = gold_fids & scope["functions"]
            covered_modules = gold_modules & scope["modules"] if scope["modules"] else set()

            q_result[scope_name] = {
                "function_coverage": len(covered_fids) / len(gold_fids),
                "module_coverage": len(covered_modules) / len(gold_modules) if gold_modules else 0,
                "has_any_gold": len(covered_fids) > 0,
                "full_coverage": len(covered_fids) == len(gold_fids),
                "function_count": len(scope["functions"]),
                "module_count": len(scope["modules"]),
                "file_count": len({func_to_file.get(fid, "") for fid in scope["functions"] if func_to_file.get(fid)}),
            }

        results.append(q_result)

    return results


def print_report(results: list[dict]):
    print("\n" + "=" * 80)
    print("LOCAL REPOSITORY REGION COVERAGE REPORT")
    print("=" * 80)

    scope_names = ["top1_module", "top3_modules", "top5_modules",
                   "region_dir_only", "region_caller_only", "region_full",
                   "directory_seed"]

    available = [s for s in scope_names if s in results[0]]

    print("\n[Gold Function Coverage]")
    print(f"{'Scope':<25} {'Mean Cov':<12} {'Full Cov':<12} {'Any Gold':<12}")
    for s in available:
        mean_cov = np.mean([r[s]["function_coverage"] for r in results])
        full_cov = np.mean([r[s]["full_coverage"] for r in results])
        any_gold = np.mean([r[s]["has_any_gold"] for r in results])
        print(f"{s:<25} {mean_cov*100:>6.1f}%      {full_cov*100:>6.1f}%      {any_gold*100:>6.1f}%")

    print("\n[Gold Module Coverage]")
    print(f"{'Scope':<25} {'Mean Cov':<12}")
    for s in available:
        if s == "directory_seed":
            continue
        mean_cov = np.mean([r[s]["module_coverage"] for r in results])
        print(f"{s:<25} {mean_cov*100:>6.1f}%")

    print("\n[Region Size]")
    print(f"{'Scope':<25} {'Funcs(mean)':<15} {'Files(mean)':<15} {'Modules(mean)':<15}")
    for s in available:
        funcs = np.mean([r[s]["function_count"] for r in results])
        files = np.mean([r[s]["file_count"] for r in results])
        mods = np.mean([r[s]["module_count"] for r in results]) if s != "directory_seed" else 0
        print(f"{s:<25} {funcs:<15.0f} {files:<15.0f} {mods:<15.0f}")

    # Distribution of function coverage
    print("\n[Function Coverage Distribution (region_full)]")
    bins = [0, 0.25, 0.5, 0.75, 0.99, 1.0]
    labels = ["0%", "1-25%", "26-50%", "51-75%", "76-99%", "100%"]
    covs = [r["region_full"]["function_coverage"] for r in results if "region_full" in r]
    for i in range(len(bins) - 1):
        count = sum(1 for c in covs if bins[i] < c <= bins[i + 1])
        print(f"  {labels[i]:>7}: {count} questions ({count/len(covs)*100:.1f}%)")


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

    results = evaluate_region_coverage(
        items, q_embs, func_to_module, module_to_funcs, func_to_file,
        file_to_funcs, func_calls, chunk_index_by_id, doc_matrix,
        line_lookup, chunks_by_id,
    )

    print_report(results)

    output_path = _ROOT / "results" / "region_coverage.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\nSaved detailed results to: {output_path}")


if __name__ == "__main__":
    main()
