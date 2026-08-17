#!/usr/bin/env python3
"""
评估 Concept-Level Navigation。

核心问题：Semantic Concept 是否比 Module 更适合作为 Agent 的高层抽象？

对比：
1. Module-level retrieval（top-K modules + function retrieval）
2. Concept-level retrieval（top-K concepts + function retrieval）
3. Module + symbol rerank
4. Concept + symbol rerank
5. Baseline / region

附加分析：
- Question symbols 跨 module 分布
- Question symbols 跨 concept 分布
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from src.core.concept_abstraction import ConceptAbstraction
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


def extract_question_symbols(question: str) -> list:
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", question)
    stopwords = {
        "How", "What", "Where", "When", "Why", "Is", "Are", "Does", "Do", "Did",
        "the", "a", "an", "and", "or", "in", "on", "at", "to", "for", "of",
        "with", "by", "from", "as", "it", "its", "this", "that", "these", "those",
        "AI", "帮我", "现有", "是否", "什么", "怎么", "如何", "为什么",
        "担心", "看", "确认", "顺一下", "理解",
    }
    symbols = []
    for t in tokens:
        if len(t) < 3 or t in stopwords:
            continue
        symbols.append(t.lower())
        parts = re.findall(r"[A-Z][a-z]+|[a-z]+|[A-Z]+", t)
        if len(parts) > 1:
            for p in parts:
                if len(p) >= 3 and p not in stopwords:
                    symbols.append(p.lower())
    return list(set(symbols))


def symbol_match_score(fid: str, symbols: list, chunks_by_id: dict) -> float:
    if not symbols:
        return 0.0
    meta = chunks_by_id.get(fid, {}).get("meta", {})
    name = meta.get("name", "").lower()
    file_path = meta.get("file_path", "").lower()
    signature = meta.get("signature", "").lower()
    text = chunks_by_id.get(fid, {}).get("text", "").lower()
    text_lower = f"{name} {file_path} {signature} {text}"

    matched = 0
    for sym in symbols:
        if sym in name:
            matched += 2.0
        elif sym in text_lower:
            matched += 1.0
    return matched / (len(symbols) * 2.0)


def get_top_k(q_emb, candidate_fids, doc_matrix, chunk_index_by_id, top_k=100):
    idxs = [chunk_index_by_id[fid] for fid in candidate_fids if fid in chunk_index_by_id]
    if not idxs:
        return []
    sub = doc_matrix[idxs]
    sims = sub @ q_emb
    order = np.argsort(-sims)[:top_k]
    return [(candidate_fids[i], float(sims[i])) for i in order]


def composite_rerank(candidate_fids, q_emb, doc_matrix, chunk_index_by_id, symbols, chunks_by_id, alpha=1.0, gamma=0.5):
    scored = []
    for fid in candidate_fids:
        idx = chunk_index_by_id.get(fid)
        if idx is None:
            continue
        sim = float(doc_matrix[idx] @ q_emb)
        sym = symbol_match_score(fid, symbols, chunks_by_id)
        score = alpha * sim + gamma * sym
        scored.append((fid, score))
    scored.sort(key=lambda x: -x[1])
    return scored


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
    ma: ModuleAbstraction,
    ca: ConceptAbstraction,
):
    results = []
    symbol_cross_module_stats = []
    symbol_cross_concept_stats = []

    all_func_ids = list(chunk_index_by_id.keys())

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
        }

        # Symbol cross-module / cross-concept analysis
        symbol_fids = [
            fid for fid in all_func_ids
            if any(sym in chunks_by_id.get(fid, {}).get("meta", {}).get("name", "").lower() for sym in symbols)
        ]
        if symbol_fids:
            symbol_modules = set()
            symbol_concepts = set()
            for fid in symbol_fids:
                # module
                for mid, fids in module_to_funcs.items():
                    if fid in fids:
                        symbol_modules.add(mid)
                        break
                # concept
                for cid, c in ca.concepts.items():
                    if fid in c.function_ids:
                        symbol_concepts.add(cid)
                        break

            symbol_cross_module_stats.append(len(symbol_modules))
            symbol_cross_concept_stats.append(len(symbol_concepts))

            q_result["symbol_fids_count"] = len(symbol_fids)
            q_result["symbol_modules_count"] = len(symbol_modules)
            q_result["symbol_concepts_count"] = len(symbol_concepts)
        else:
            q_result["symbol_fids_count"] = 0
            q_result["symbol_modules_count"] = 0
            q_result["symbol_concepts_count"] = 0

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

        # Module-level retrieval
        for k in [3, 5, 10]:
            module_candidates = ma.retrieve_modules(q_emb, doc_matrix, chunk_index_by_id, top_k=k, scoring="max")
            module_fids = set()
            for m, _ in module_candidates:
                module_fids.update(m.function_ids)

            # pure embedding
            emb_top = get_top_k(q_emb, list(module_fids), doc_matrix, chunk_index_by_id, top_k=100)
            q_result[f"module_top{k}_emb"] = evaluate_scope(gold_fids, emb_top, len(module_fids))

            # + symbol
            sym_top = composite_rerank(list(module_fids), q_emb, doc_matrix, chunk_index_by_id, symbols, chunks_by_id)
            q_result[f"module_top{k}_symbol"] = evaluate_scope(gold_fids, sym_top, len(module_fids))

        # Concept-level retrieval
        for k in [5, 10, 20, 50]:
            concept_candidates = ca.retrieve_concepts(q_emb, doc_matrix, chunk_index_by_id, top_k=k, scoring="max")
            concept_fids = set()
            for c, _ in concept_candidates:
                concept_fids.update(c.function_ids)

            # pure embedding
            emb_top = get_top_k(q_emb, list(concept_fids), doc_matrix, chunk_index_by_id, top_k=100)
            q_result[f"concept_top{k}_emb"] = evaluate_scope(gold_fids, emb_top, len(concept_fids))

            # + symbol
            sym_top = composite_rerank(list(concept_fids), q_emb, doc_matrix, chunk_index_by_id, symbols, chunks_by_id)
            q_result[f"concept_top{k}_symbol"] = evaluate_scope(gold_fids, sym_top, len(concept_fids))

        results.append(q_result)

    return results, symbol_cross_module_stats, symbol_cross_concept_stats


def print_report(results, symbol_cross_module_stats, symbol_cross_concept_stats):
    print("\n" + "=" * 100)
    print("CONCEPT-LEVEL NAVIGATION EVALUATION")
    print("=" * 100)

    print("\n[Question Symbol Distribution]")
    print(f"  Avg modules touched by question symbols: {np.mean(symbol_cross_module_stats):.1f}")
    print(f"  Avg concepts touched by question symbols: {np.mean(symbol_cross_concept_stats):.1f}")
    print(f"  Questions with symbols spanning ≥2 modules: {sum(1 for x in symbol_cross_module_stats if x >= 2)}/{len(symbol_cross_module_stats)} ({sum(1 for x in symbol_cross_module_stats if x >= 2)/len(symbol_cross_module_stats)*100:.1f}%)")
    print(f"  Questions with symbols spanning ≥3 modules: {sum(1 for x in symbol_cross_module_stats if x >= 3)}/{len(symbol_cross_module_stats)} ({sum(1 for x in symbol_cross_module_stats if x >= 3)/len(symbol_cross_module_stats)*100:.1f}%)")
    print(f"  Questions with symbols spanning ≥2 concepts: {sum(1 for x in symbol_cross_concept_stats if x >= 2)}/{len(symbol_cross_concept_stats)} ({sum(1 for x in symbol_cross_concept_stats if x >= 2)/len(symbol_cross_concept_stats)*100:.1f}%)")

    config_names = [key for key in results[0].keys()
                    if key not in ("qa_id", "question", "gold_count", "symbol_fids_count", "symbol_modules_count", "symbol_concepts_count")]

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

    print("\n[MRR & Median First Gold Rank]")
    print(f"{'Config':<35} {'MRR':<10} {'Median Rank':<15}")
    for name in config_names:
        mrr = np.mean([r[name]["mrr"] for r in results])
        ranks = [r[name]["first_gold_rank"] for r in results if r[name]["first_gold_rank"] is not None]
        median_r = np.median(ranks) if ranks else 0
        print(f"{name:<35} {mrr:<10.3f} {median_r:<15.0f}")


def main():
    benchmark_path = _ROOT / "datasets" / "benchmark_hard.json"
    index_path = _ROOT / "data" / "qa_embedding_index.json"
    module_cache = _ROOT / "data" / "module_abstraction.json"
    concept_cache = _ROOT / "data" / "concept_abstraction.json"

    items = load_benchmark(benchmark_path)
    chunks, doc_matrix = load_embedding_index(index_path)
    line_lookup, chunks_by_id, chunk_index_by_id, func_to_file, file_to_funcs = build_helpers(chunks)

    funcs, calls = fetch_functions_and_calls()
    func_to_module, module_to_funcs = detect_modules(funcs, calls, resolution=0.5)
    func_calls = fetch_call_graph()

    retriever = FastEmbeddingRetriever(index_path=index_path)
    print("Encoding questions...")
    q_embs = retriever.encode_queries([item["question"] for item in items])

    ma = ModuleAbstraction(cache_path=str(module_cache))
    if not ma.load():
        print("Building module abstraction...")
        ma.build_from_neo4j()
        ma.save()

    ca = ConceptAbstraction(cache_path=str(concept_cache))
    if not ca.load():
        print("Building concept abstraction...")
        ca.build_from_modules(ma, functions=funcs, calls=calls)
        ca.save()

    results, symbol_cross_module_stats, symbol_cross_concept_stats = evaluate(
        items, q_embs, func_to_module, module_to_funcs, func_to_file,
        file_to_funcs, func_calls, chunk_index_by_id, doc_matrix,
        line_lookup, chunks_by_id, ma, ca,
    )

    print_report(results, symbol_cross_module_stats, symbol_cross_concept_stats)

    output_path = _ROOT / "results" / "concept_navigation.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({
            "results": results,
            "symbol_cross_module_stats": symbol_cross_module_stats,
            "symbol_cross_concept_stats": symbol_cross_concept_stats,
        }, f, ensure_ascii=False, indent=2)
    print(f"\nSaved detailed results to: {output_path}")


if __name__ == "__main__":
    main()
