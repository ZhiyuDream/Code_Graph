#!/usr/bin/env python3
"""
同时评估三种 Navigation Signal：

1. Multiple-Seed + Degree-Aware Retrieval
   - Top-K seeds → 3-hop expansion → rerank by α*sim + β*degree

2. Question-Symbol Navigation
   - 从 question 提取 symbols
   - 用 symbol matching 找 seed / 给节点打分
   - 结合 embedding + degree + symbol_match

3. Two-Stage: Module coarse + Function fine
   - Stage 1: Top-5 modules by embedding
   - Stage 2: 在 module 内函数用 composite score 排序

对比 baseline / region / multiseed_baseline。
"""
from __future__ import annotations

import json
import math
import re
import sys
from collections import defaultdict, deque
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import numpy as np

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from src.core.module_abstraction import ModuleAbstraction
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


def build_call_graphs(func_calls):
    undirected = defaultdict(set)
    degree = defaultdict(int)
    for a, b in func_calls:
        if a == b:
            continue
        undirected[a].add(b)
        undirected[b].add(a)
        degree[a] += 1
        degree[b] += 1
    return undirected, degree


def bfs_reachable(starts, graph, max_hops):
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
        for neighbor in graph.get(node, set()):
            if neighbor not in visited:
                visited[neighbor] = dist + 1
                queue.append(neighbor)
    return set(visited.keys())


def extract_question_symbols(question: str) -> List[str]:
    """从问题中提取候选 symbols（函数名/类型/配置项）。"""
    # 代码风格的标识符
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", question)
    # 过滤常见停用词
    stopwords = {
        "How", "What", "Where", "When", "Why", "Is", "Are", "Does", "Do", "Did",
        "the", "a", "an", "and", "or", "in", "on", "at", "to", "for", "of",
        "with", "by", "from", "as", "it", "its", "this", "that", "these", "those",
        "AI", "帮我", "现有", "现有", "是否", "什么", "怎么", "如何", "为什么",
        "担心", "帮我", "看", "确认", "顺一下", "理解",
    }
    symbols = []
    for t in tokens:
        if len(t) < 3 or t in stopwords:
            continue
        # 保留 camelCase / snake_case 嫌疑词
        symbols.append(t)
        # 拆分 camelCase
        parts = re.findall(r"[A-Z][a-z]+|[a-z]+|[A-Z]+", t)
        if len(parts) > 1:
            for p in parts:
                if len(p) >= 3 and p not in stopwords:
                    symbols.append(p)
    return list(set(symbols))


def symbol_match_score(fid: str, symbols: List[str], chunks_by_id: Dict, func_to_file: Dict) -> float:
    """计算 function 与 question symbols 的匹配分数。"""
    if not symbols:
        return 0.0

    meta = chunks_by_id.get(fid, {}).get("meta", {})
    name = meta.get("name", "")
    file_path = meta.get("file_path", "")
    signature = meta.get("signature", "")
    text = chunks_by_id.get(fid, {}).get("text", "")

    text_lower = f"{name} {file_path} {signature} {text}".lower()
    name_lower = name.lower()

    matched = 0
    for sym in symbols:
        sym_lower = sym.lower()
        if sym_lower in name_lower:
            matched += 2.0  # 函数名匹配权重高
        elif sym_lower in text_lower:
            matched += 1.0
    return matched / (len(symbols) * 2.0)  # normalize to ~0-1


def normalize_degree(degree: int, max_degree: int) -> float:
    if max_degree <= 1:
        return 0.0
    return math.log(degree + 1) / math.log(max_degree + 1)


def composite_rerank(
    candidate_fids: Set[str],
    q_emb: np.ndarray,
    doc_matrix: np.ndarray,
    chunk_index_by_id: Dict,
    degree: Dict,
    max_degree: int,
    symbols: List[str],
    chunks_by_id: Dict,
    alpha: float = 1.0,
    beta: float = 0.3,
    gamma: float = 0.5,
) -> List[Tuple[str, float]]:
    """Composite score = α*embedding_sim + β*norm_degree + γ*symbol_match。"""
    scored = []
    for fid in candidate_fids:
        idx = chunk_index_by_id.get(fid)
        if idx is None:
            continue
        sim = float(doc_matrix[idx] @ q_emb)
        deg = normalize_degree(degree.get(fid, 0), max_degree)
        sym = symbol_match_score(fid, symbols, chunks_by_id, {})
        score = alpha * sim + beta * deg + gamma * sym
        scored.append((fid, score, sim, deg, sym))

    scored.sort(key=lambda x: -x[1])
    return [(fid, score) for fid, score, _, _, _ in scored]


def evaluate_navigation_signals(
    items,
    q_embs,
    func_to_module,
    module_to_funcs,
    func_to_file,
    file_to_funcs,
    func_calls,
    undirected,
    degree,
    chunk_index_by_id,
    doc_matrix,
    line_lookup,
    chunks_by_id,
    ma: Optional,
):
    results = []
    all_func_ids = list(chunk_index_by_id.keys())
    max_degree = max(degree.values()) if degree else 1

    for idx, (item, q_emb) in enumerate(zip(items, q_embs), 1):
        print(f"[{idx}/{len(items)}] {item['qa_id']}", flush=True)
        gold_fids = gold_evidence_to_function_ids(item, line_lookup, chunks_by_id)
        if not gold_fids:
            continue

        symbols = extract_question_symbols(item["question"])

        q_result = {
            "qa_id": item["qa_id"],
            "question": item["question"],
            "gold_count": len(gold_fids),
            "symbols": symbols,
        }

        # Baseline
        baseline_top = get_top_k(q_emb, all_func_ids, doc_matrix, chunk_index_by_id, top_k=100)
        q_result["baseline"] = evaluate_scope(gold_fids, baseline_top, len(all_func_ids))

        # Region
        ranked_modules = score_modules(q_emb, module_to_funcs, doc_matrix, chunk_index_by_id, method="max")
        top3_modules = [mid for mid, _ in ranked_modules[:3]]
        _, region_fids = build_region(
            top3_modules, "top3_modules+callers_k5", module_to_funcs, func_to_module,
            func_to_file, file_to_funcs, func_calls, q_emb, doc_matrix, chunk_index_by_id,
        )
        region_fids = set(region_fids)
        region_top = get_top_k(q_emb, list(region_fids), doc_matrix, chunk_index_by_id, top_k=100)
        q_result["region"] = evaluate_scope(gold_fids, region_top, len(region_fids))

        # Multi-seed baseline
        top5_seeds = get_top_k(q_emb, list(region_fids), doc_matrix, chunk_index_by_id, top_k=5)
        top5_seed_fids = [fid for fid, _ in top5_seeds]

        # 1. Multiple-Seed + Degree-Aware
        for hops in [2, 3]:
            visited = bfs_reachable(top5_seed_fids, undirected, hops)
            # pure embedding rerank
            emb_top = get_top_k(q_emb, list(visited), doc_matrix, chunk_index_by_id, top_k=100)
            q_result[f"multiseed_emb_{hops}hop"] = evaluate_scope(gold_fids, emb_top, len(visited))

            # degree-aware rerank
            comp_top = composite_rerank(
                visited, q_emb, doc_matrix, chunk_index_by_id, degree, max_degree,
                symbols, chunks_by_id, alpha=1.0, beta=0.5, gamma=0.0,
            )
            q_result[f"multiseed_degree_{hops}hop"] = evaluate_scope(gold_fids, comp_top, len(visited))

            # symbol-aware rerank
            comp_top = composite_rerank(
                visited, q_emb, doc_matrix, chunk_index_by_id, degree, max_degree,
                symbols, chunks_by_id, alpha=1.0, beta=0.0, gamma=1.0,
            )
            q_result[f"multiseed_symbol_{hops}hop"] = evaluate_scope(gold_fids, comp_top, len(visited))

            # composite rerank
            comp_top = composite_rerank(
                visited, q_emb, doc_matrix, chunk_index_by_id, degree, max_degree,
                symbols, chunks_by_id, alpha=1.0, beta=0.3, gamma=0.5,
            )
            q_result[f"multiseed_composite_{hops}hop"] = evaluate_scope(gold_fids, comp_top, len(visited))

        # 2. Question-Symbol Navigation
        if symbols:
            # symbol seeds: functions whose names contain any question symbol
            symbol_seeds = [
                fid for fid in all_func_ids
                if any(sym.lower() in chunks_by_id.get(fid, {}).get("meta", {}).get("name", "").lower()
                       for sym in symbols)
            ]
            if len(symbol_seeds) > 20:
                # keep top-20 by embedding similarity
                symbol_seeds = [fid for fid, _ in get_top_k(q_emb, symbol_seeds, doc_matrix, chunk_index_by_id, top_k=20)]

            if symbol_seeds:
                for hops in [2, 3]:
                    visited = bfs_reachable(symbol_seeds, undirected, hops)
                    comp_top = composite_rerank(
                        visited, q_emb, doc_matrix, chunk_index_by_id, degree, max_degree,
                        symbols, chunks_by_id, alpha=1.0, beta=0.3, gamma=0.5,
                    )
                    q_result[f"symbol_seed_composite_{hops}hop"] = evaluate_scope(gold_fids, comp_top, len(visited))
            else:
                for hops in [2, 3]:
                    q_result[f"symbol_seed_composite_{hops}hop"] = make_empty_metrics()
        else:
            for hops in [2, 3]:
                q_result[f"symbol_seed_composite_{hops}hop"] = make_empty_metrics()

        # 3. Two-Stage: Module coarse + Function fine
        if ma is not None:
            module_candidates = ma.retrieve_modules(q_emb, doc_matrix, chunk_index_by_id, top_k=5, scoring="max")
            module_fids = set()
            for m, _ in module_candidates:
                module_fids.update(m.function_ids)

            # Stage 2: composite rerank within modules
            comp_top = composite_rerank(
                module_fids, q_emb, doc_matrix, chunk_index_by_id, degree, max_degree,
                symbols, chunks_by_id, alpha=1.0, beta=0.3, gamma=0.5,
            )
            q_result["module_two_stage_composite"] = evaluate_scope(gold_fids, comp_top, len(module_fids))

            # also pure embedding within modules
            emb_top = get_top_k(q_emb, list(module_fids), doc_matrix, chunk_index_by_id, top_k=100)
            q_result["module_two_stage_emb"] = evaluate_scope(gold_fids, emb_top, len(module_fids))

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


def make_empty_metrics():
    metrics = {"scope_size": 0, "mrr": 0.0, "first_gold_rank": None}
    for k in [5, 10, 20, 50, 100]:
        metrics[f"recall@{k}"] = 0.0
        metrics[f"has_gold@{k}"] = False
        metrics[f"full_coverage@{k}"] = False
    return metrics


def evaluate_scope(gold_fids, retrieved, scope_size):
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
    print("NAVIGATION SIGNAL EVALUATION")
    print("=" * 100)

    config_names = [key for key in results[0].keys()
                    if key not in ("qa_id", "question", "gold_count", "symbols")]

    print("\n[Scope Size]")
    print(f"{'Config':<45} {'Scope Size':<15}")
    for name in config_names:
        size = np.mean([r[name]["scope_size"] for r in results])
        print(f"{name:<45} {size:<15.0f}")

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

    print("\n[MRR & Median First Gold Rank]")
    print(f"{'Config':<45} {'MRR':<10} {'Median Rank':<15}")
    for name in config_names:
        mrr = np.mean([r[name]["mrr"] for r in results])
        ranks = [r[name]["first_gold_rank"] for r in results if r[name]["first_gold_rank"] is not None]
        median_r = np.median(ranks) if ranks else 0
        print(f"{name:<45} {mrr:<10.3f} {median_r:<15.0f}")


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
    undirected, degree = build_call_graphs(func_calls)

    retriever = FastEmbeddingRetriever(index_path=index_path)
    print("Encoding questions...")
    q_embs = retriever.encode_queries([item["question"] for item in items])

    ma = ModuleAbstraction(cache_path=str(cache_path))
    if not ma.load():
        print("Module abstraction not found. Building...")
        ma.build_from_neo4j()
        ma.save()

    results = evaluate_navigation_signals(
        items, q_embs, func_to_module, module_to_funcs, func_to_file,
        file_to_funcs, func_calls, undirected, degree, chunk_index_by_id,
        doc_matrix, line_lookup, chunks_by_id, ma,
    )

    print_report(results)

    output_path = _ROOT / "results" / "navigation_signals.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\nSaved detailed results to: {output_path}")


if __name__ == "__main__":
    main()
