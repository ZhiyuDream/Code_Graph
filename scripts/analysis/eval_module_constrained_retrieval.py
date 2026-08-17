#!/usr/bin/env python3
"""
Module-Constrained Function Retrieval 完整实验。

包含：
1. Baseline: 全库 function retrieval
2. Module Scope: Top-M modules → function retrieval
3. Oracle Module: gold 所在 modules → function retrieval（上限）
4. Random Scope: 与 Module Scope 同样大小的随机函数集合
5. Directory Scope: Top-D directories → function retrieval
6. Scoring Ablation: max / mean / top5_mean
7. Scope Reduction Analysis
8. Noise Source 变化分析
9. Module Prediction Failure Analysis

输出：对比表格、CDF rank distribution、per-question JSON。
"""
from __future__ import annotations

import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

import networkx as nx
import numpy as np

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from src.core.neo4j_client import run_cypher
from src.qa.retrievers.fast_embedding import FastEmbeddingRetriever

random.seed(42)


def load_benchmark(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)["items"]


def load_embedding_index(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    chunks = data["chunks"]
    embs = np.asarray(data["embeddings"], dtype=np.float32)
    return chunks, embs


def fetch_functions_and_calls():
    print("[1/5] Fetching functions from Neo4j...")
    funcs = {r["f.id"]: r for r in run_cypher(
        "MATCH (f:Function) RETURN f.id, f.name, f.file_path, f.start_line, f.end_line"
    )}
    print(f"      Functions: {len(funcs)}")

    print("[2/5] Fetching CALLS edges from Neo4j...")
    calls = [(r["caller"], r["callee"]) for r in run_cypher(
        "MATCH (a:Function)-[:CALLS]->(b:Function) RETURN a.id AS caller, b.id AS callee"
    ) if r["caller"] != r["callee"]]
    print(f"      Calls: {len(calls)}")
    return funcs, calls


def detect_modules(funcs: dict, calls: list, resolution: float = 0.5, same_file_weight: float = 0.5):
    print(f"[3/5] Detecting modules (resolution={resolution})...")
    G = nx.Graph()
    for fid, f in funcs.items():
        G.add_node(fid, name=f["f.name"], file_path=f["f.file_path"])

    seen = set()
    for a, b in calls:
        key = tuple(sorted([a, b]))
        if key in seen:
            continue
        seen.add(key)
        if G.has_edge(a, b):
            G[a][b]["weight"] += 1.0
        else:
            G.add_edge(a, b, weight=1.0)

    file_to_funcs = defaultdict(list)
    for fid, f in funcs.items():
        file_to_funcs[f["f.file_path"]].append(fid)
    for fp, fids in file_to_funcs.items():
        if len(fids) < 2:
            continue
        for i in range(len(fids)):
            for j in range(i + 1, len(fids)):
                a, b = fids[i], fids[j]
                if G.has_edge(a, b):
                    G[a][b]["weight"] += same_file_weight
                else:
                    G.add_edge(a, b, weight=same_file_weight)

    import community as community_louvain
    partition = community_louvain.best_partition(G, weight="weight", resolution=resolution, random_state=42)

    communities = defaultdict(list)
    for fid, cid in partition.items():
        communities[cid].append(fid)

    large = {cid: mem for cid, mem in communities.items() if len(mem) >= 10}
    small = {cid: mem for cid, mem in communities.items() if len(mem) < 10}
    if large:
        for sc, smem in small.items():
            best = None
            best_score = -1
            for lc, lmem in large.items():
                score = sum(1 for m in smem for lm in lmem if G.has_edge(m, lm))
                if score > best_score:
                    best_score = score
                    best = lc
            if best is None or best_score == 0:
                best = max(large, key=lambda c: len(large[c]))
            large[best].extend(smem)
        communities = large

    func_to_module = {}
    module_to_funcs = {}
    for cid, members in communities.items():
        mid = f"module:{cid}"
        module_to_funcs[mid] = members
        for fid in members:
            func_to_module[fid] = mid

    print(f"      Modules: {len(module_to_funcs)}")
    return func_to_module, module_to_funcs


def build_helpers(chunks: list):
    line_lookup = {}
    for ch in chunks:
        meta = ch.get("meta", {})
        fp = meta.get("file_path", "")
        fid = ch.get("id", "")
        for line in range(meta.get("start_line", 0), meta.get("end_line", 0) + 1):
            line_lookup[(fp, line)] = fid

    chunks_by_id = {ch.get("id", ""): ch for ch in chunks}
    chunk_index_by_id = {ch.get("id", ""): i for i, ch in enumerate(chunks)}
    func_to_file = {ch.get("id", ""): ch.get("meta", {}).get("file_path", "") for ch in chunks}
    file_to_funcs = defaultdict(list)
    for ch in chunks:
        fp = ch.get("meta", {}).get("file_path", "")
        file_to_funcs[fp].append(ch.get("id", ""))

    return line_lookup, chunks_by_id, chunk_index_by_id, func_to_file, file_to_funcs


def gold_evidence_to_function_ids(item: dict, line_lookup: dict, chunks_by_id: dict):
    gold_fids = set()
    for ev in item.get("gold_evidence", []):
        fp = ev.get("file", "")
        line = ev.get("line_start", 0)
        symbol = ev.get("symbol", "")
        fid = line_lookup.get((fp, line))
        if fid is None and symbol and fp:
            for ch in chunks_by_id.values():
                meta = ch.get("meta", {})
                if meta.get("file_path") == fp and meta.get("name") == symbol:
                    fid = ch.get("id", "")
                    break
        if fid:
            gold_fids.add(fid)
    return gold_fids


def score_modules(
    q_emb: np.ndarray,
    module_to_funcs: dict,
    doc_matrix: np.ndarray,
    chunk_index_by_id: dict,
    method: str = "max",
) -> list[tuple[str, float]]:
    """module scoring: max / mean / top5_mean"""
    scores = []
    for mid, fids in module_to_funcs.items():
        idxs = [chunk_index_by_id[fid] for fid in fids if fid in chunk_index_by_id]
        if not idxs:
            continue
        sims = doc_matrix[idxs] @ q_emb
        if method == "max":
            score = float(np.max(sims))
        elif method == "mean":
            score = float(np.mean(sims))
        elif method == "top5_mean":
            score = float(np.mean(np.partition(sims, -min(5, len(sims)))[-min(5, len(sims)):]))
        else:
            raise ValueError(f"Unknown method: {method}")
        scores.append((mid, score))
    scores.sort(key=lambda x: -x[1])
    return scores


def retrieve_in_scope(
    q_emb: np.ndarray,
    candidate_fids: list[str],
    doc_matrix: np.ndarray,
    chunk_index_by_id: dict,
    top_k: int = 50,
) -> list[tuple[str, float]]:
    idxs = [chunk_index_by_id[fid] for fid in candidate_fids if fid in chunk_index_by_id]
    if not idxs:
        return []
    sub = doc_matrix[idxs]
    sims = sub @ q_emb
    order = np.argsort(-sims)[:top_k]
    # `order` contains indices into `candidate_fids`/`sims`/`idxs` (they are aligned)
    return [(candidate_fids[i], float(sims[i])) for i in order]


def get_scope_candidates(
    scope_type: str,
    ranked_modules: list[tuple[str, float]],
    module_to_funcs: dict,
    gold_fids: set,
    func_to_file: dict,
    file_to_funcs: dict,
    all_func_ids: list[str],
    top_m: int = 3,
) -> tuple[list[str], dict]:
    """返回候选函数集合和 scope 元信息。"""
    info = {"type": scope_type, "top_m": top_m}

    if scope_type == "baseline":
        return all_func_ids, info

    if scope_type == "module":
        top_modules = [mid for mid, _ in ranked_modules[:top_m]]
        fids = []
        for mid in top_modules:
            fids.extend(module_to_funcs[mid])
        info["modules"] = top_modules
        return list(set(fids)), info

    if scope_type == "oracle_module":
        gold_modules = {func_to_module.get(fid) for fid in gold_fids}
        gold_modules.discard(None)
        fids = []
        for mid in gold_modules:
            fids.extend(module_to_funcs[mid])
        info["modules"] = list(gold_modules)
        return list(set(fids)), info

    if scope_type == "random_same_size":
        # 先算 module scope 大小
        module_fids, _ = get_scope_candidates(
            "module", ranked_modules, module_to_funcs, gold_fids, func_to_file, file_to_funcs, all_func_ids, top_m
        )
        size = len(module_fids)
        info["target_size"] = size
        return random.sample(all_func_ids, min(size, len(all_func_ids))), info

    if scope_type == "directory":
        # 用 module 内函数的最大相似度给 directory 打分
        # 简化：复用 module ranking 中 top-M modules 涉及的 directories，取同样数量函数
        module_fids, _ = get_scope_candidates(
            "module", ranked_modules, module_to_funcs, gold_fids, func_to_file, file_to_funcs, all_func_ids, top_m
        )
        target_size = len(module_fids)
        # 按 directory 内最大 function similarity 排序
        dir_scores = {}
        for fp, fids in file_to_funcs.items():
            idxs = [chunk_index_by_id_global.get(fid) for fid in fids if fid in chunk_index_by_id_global]
            if not idxs:
                continue
            dir_scores[fp] = float(np.max(doc_matrix_global[idxs] @ q_emb_global))
        sorted_dirs = sorted(dir_scores.items(), key=lambda x: -x[1])
        dir_fids = []
        for fp, _ in sorted_dirs:
            dir_fids.extend(file_to_funcs[fp])
            if len(dir_fids) >= target_size:
                break
        info["directories"] = [fp for fp, _ in sorted_dirs[:top_m * 2]]
        return list(set(dir_fids)), info

    raise ValueError(f"Unknown scope type: {scope_type}")


# 全局变量用于 directory scope 中的回调
chunk_index_by_id_global: dict = {}
doc_matrix_global: np.ndarray = np.array([])
q_emb_global: np.ndarray = np.array([])
func_to_module: dict = {}


def evaluate_one_question(
    item: dict,
    q_emb: np.ndarray,
    func_to_module_local: dict,
    module_to_funcs: dict,
    func_to_file: dict,
    file_to_funcs: dict,
    all_func_ids: list[str],
    doc_matrix: np.ndarray,
    chunk_index_by_id: dict,
    line_lookup: dict,
    chunks_by_id: dict,
    scoring_method: str = "max",
    top_m_values: tuple = (1, 3, 5),
    top_k_values: tuple = (5, 10, 20, 50, 100),
) -> dict:
    global chunk_index_by_id_global, doc_matrix_global, q_emb_global, func_to_module
    chunk_index_by_id_global = chunk_index_by_id
    doc_matrix_global = doc_matrix
    q_emb_global = q_emb
    func_to_module = func_to_module_local

    question = item["question"]
    gold_fids = gold_evidence_to_function_ids(item, line_lookup, chunks_by_id)
    if not gold_fids:
        return None

    gold_modules = {func_to_module.get(fid) for fid in gold_fids}
    gold_modules.discard(None)
    gold_files = {func_to_file.get(fid, "") for fid in gold_fids}
    gold_files.discard("")

    ranked_modules = score_modules(q_emb, module_to_funcs, doc_matrix, chunk_index_by_id, method=scoring_method)

    result = {
        "qa_id": item["qa_id"],
        "question": question,
        "gold_count": len(gold_fids),
        "gold_modules": list(gold_modules),
        "gold_files": list(gold_files),
        "scoring_method": scoring_method,
    }

    # Module prediction metrics
    for k in [1, 3, 5, 10]:
        top_k_modules = {mid for mid, _ in ranked_modules[:k]}
        covered = [fid for fid in gold_fids if func_to_module.get(fid) in top_k_modules]
        result[f"module_recall@{k}"] = len(covered) > 0
        result[f"module_full_coverage@{k}"] = len(covered) == len(gold_fids)

    # Retrieval experiments
    scope_types = ["baseline", "module", "oracle_module", "random_same_size", "directory"]
    scope_results = {}

    for scope_type in scope_types:
        for top_m in top_m_values if scope_type in ("module", "random_same_size", "directory") else [1]:
            key = f"{scope_type}_m{top_m}" if scope_type != "baseline" else "baseline"
            candidates, info = get_scope_candidates(
                scope_type, ranked_modules, module_to_funcs, gold_fids, func_to_file, file_to_funcs, all_func_ids, top_m
            )
            retrieved = retrieve_in_scope(q_emb, candidates, doc_matrix, chunk_index_by_id, top_k=max(top_k_values))
            retrieved_fids = [fid for fid, _ in retrieved]

            # Recall@K
            recall = {}
            full_cov = {}
            for k in top_k_values:
                top_k_fids = set(retrieved_fids[:k])
                covered = gold_fids & top_k_fids
                recall[k] = len(covered) / len(gold_fids)
                full_cov[k] = len(covered) == len(gold_fids)

            # rank of first gold
            first_gold_rank = None
            for rank, fid in enumerate(retrieved_fids, 1):
                if fid in gold_fids:
                    first_gold_rank = rank
                    break

            # MRR
            mrr = 1.0 / first_gold_rank if first_gold_rank else 0.0

            # noise analysis (for top-50)
            top50_fids = retrieved_fids[:50]
            noise = Counter({"same_module": 0, "other_module": 0, "same_file": 0, "other_file": 0, "is_gold": 0})
            for fid in top50_fids:
                if fid in gold_fids:
                    noise["is_gold"] += 1
                elif func_to_module.get(fid) in gold_modules:
                    noise["same_module"] += 1
                elif func_to_file.get(fid, "") in gold_files:
                    noise["same_file"] += 1
                else:
                    if func_to_module.get(fid) is not None:
                        noise["other_module"] += 1
                    else:
                        noise["other_file"] += 1

            scope_results[key] = {
                "scope_size": len(candidates),
                "scope_files": len({func_to_file.get(fid, "") for fid in candidates if func_to_file.get(fid)}),
                "scope_modules_or_dirs": len(info.get("modules", info.get("directories", []))),
                "recall": recall,
                "full_coverage": full_cov,
                "first_gold_rank": first_gold_rank,
                "mrr": mrr,
                "noise_top50": dict(noise),
                "scope_info": info,
            }

    result["scope_results"] = scope_results
    return result


def aggregate(results: list[dict], top_k_values: tuple):
    """Aggregate per-question results into summary tables."""
    summary = {}

    scope_keys = list(results[0]["scope_results"].keys()) if results else []

    for key in scope_keys:
        summary[key] = {
            "scope_size_mean": np.mean([r["scope_results"][key]["scope_size"] for r in results]),
            "scope_size_median": np.median([r["scope_results"][key]["scope_size"] for r in results]),
            "scope_files_mean": np.mean([r["scope_results"][key]["scope_files"] for r in results]),
        }
        for k in top_k_values:
            summary[key][f"recall@{k}"] = np.mean([r["scope_results"][key]["recall"][k] for r in results])
            summary[key][f"full_coverage@{k}"] = np.mean([r["scope_results"][key]["full_coverage"][k] for r in results])
        summary[key]["mrr"] = np.mean([r["scope_results"][key]["mrr"] for r in results])
        summary[key]["first_gold_rank_median"] = np.median([
            r["scope_results"][key]["first_gold_rank"] for r in results
            if r["scope_results"][key]["first_gold_rank"] is not None
        ] or [0])

    # CDF: % questions with at least one gold in top-k
    cdf = {}
    for key in scope_keys:
        cdf[key] = {}
        for k in top_k_values:
            cdf[key][k] = np.mean([r["scope_results"][key]["recall"][k] > 0 for r in results])

    # Noise aggregate
    noise_summary = {}
    for key in scope_keys:
        agg = Counter()
        for r in results:
            agg.update(r["scope_results"][key]["noise_top50"])
        total = sum(agg.values()) - agg.get("is_gold", 0)
        if total > 0:
            noise_summary[key] = {
                cat: f"{cnt / total * 100:.1f}%" for cat, cnt in agg.items()
            }

    # Failure analysis: module prediction misses
    failures = [r for r in results if not r["module_recall@3"]]
    failure_analysis = {
        "count": len(failures),
        "avg_gold_count": np.mean([r["gold_count"] for r in failures]) if failures else 0,
        "avg_gold_modules": np.mean([len(r["gold_modules"]) for r in failures]) if failures else 0,
        "examples": [
            {
                "qa_id": r["qa_id"],
                "question": r["question"],
                "gold_count": r["gold_count"],
                "gold_modules": r["gold_modules"],
            }
            for r in failures[:10]
        ],
    }

    return summary, cdf, noise_summary, failure_analysis


def print_report(summary: dict, cdf: dict, noise: dict, failure: dict, top_k_values: tuple, scoring_method: str):
    print("\n" + "=" * 80)
    print(f"MODULE-CONSTRAINED RETRIEVAL REPORT (scoring={scoring_method})")
    print("=" * 80)

    print("\n[Scope Size Reduction]")
    print(f"{'Scope':<25} {'Funcs(mean)':<15} {'Funcs(median)':<15} {'Files(mean)':<15}")
    for key, s in summary.items():
        print(f"{key:<25} {s['scope_size_mean']:<15.0f} {s['scope_size_median']:<15.0f} {s['scope_files_mean']:<15.0f}")

    print("\n[Recall@K (avg over all gold functions)]")
    header = f"{'Scope':<25}"
    for k in top_k_values:
        header += f" R@{k:<4}"
    print(header)
    for key, s in summary.items():
        line = f"{key:<25}"
        for k in top_k_values:
            line += f" {s[f'recall@{k}']*100:5.1f}"
        print(line)

    print("\n[CDF: % questions with at least one gold in top-k]")
    header = f"{'Scope':<25}"
    for k in top_k_values:
        header += f" P@{k:<4}"
    print(header)
    for key, c in cdf.items():
        line = f"{key:<25}"
        for k in top_k_values:
            line += f" {c[k]*100:5.1f}"
        print(line)

    print("\n[Full Coverage@K]")
    header = f"{'Scope':<25}"
    for k in top_k_values:
        header += f" FC@{k:<3}"
    print(header)
    for key, s in summary.items():
        line = f"{key:<25}"
        for k in top_k_values:
            line += f" {s[f'full_coverage@{k}']*100:5.1f}"
        print(line)

    print("\n[MRR & Median First Gold Rank]")
    print(f"{'Scope':<25} {'MRR':<10} {'Median Rank':<15}")
    for key, s in summary.items():
        print(f"{key:<25} {s['mrr']:<10.3f} {s['first_gold_rank_median']:<15.0f}")

    print("\n[Top-50 Noise Breakdown]")
    print(f"{'Scope':<25} {'same_mod':<12} {'other_mod':<12} {'same_file':<12} {'other_file':<12}")
    for key, n in noise.items():
        print(f"{key:<25} {n.get('same_module', '0'):<12} {n.get('other_module', '0'):<12} "
              f"{n.get('same_file', '0'):<12} {n.get('other_file', '0'):<12}")

    print("\n[Module Prediction Failure Analysis]")
    print(f"  Questions where Top-3 modules miss all gold: {failure['count']}/50")
    print(f"  Avg gold count in failures: {failure['avg_gold_count']:.1f}")
    print(f"  Avg gold modules in failures: {failure['avg_gold_modules']:.1f}")


def main():
    benchmark_path = _ROOT / "datasets" / "benchmark_hard.json"
    index_path = _ROOT / "data" / "qa_embedding_index.json"

    items = load_benchmark(benchmark_path)
    chunks, doc_matrix = load_embedding_index(index_path)
    line_lookup, chunks_by_id, chunk_index_by_id, func_to_file, file_to_funcs = build_helpers(chunks)

    funcs, calls = fetch_functions_and_calls()
    func_to_module_local, module_to_funcs = detect_modules(funcs, calls, resolution=0.5)

    retriever = FastEmbeddingRetriever(index_path=index_path)
    all_func_ids = list(chunk_index_by_id.keys())

    top_k_values = (5, 10, 20, 50, 100)
    top_m_values = (1, 3, 5)

    all_results = {}

    for scoring_method in ("max", "mean", "top5_mean"):
        print(f"\n\n### Running scoring method: {scoring_method} ###")
        results = []

        # encode all questions first
        questions = [item["question"] for item in items]
        print("Encoding questions...")
        q_embs = retriever.encode_queries(questions)

        for item, q_emb in zip(items, q_embs):
            res = evaluate_one_question(
                item, q_emb, func_to_module_local, module_to_funcs, func_to_file, file_to_funcs,
                all_func_ids, doc_matrix, chunk_index_by_id, line_lookup, chunks_by_id,
                scoring_method=scoring_method, top_m_values=top_m_values, top_k_values=top_k_values,
            )
            if res:
                results.append(res)

        summary, cdf, noise, failure = aggregate(results, top_k_values)
        print_report(summary, cdf, noise, failure, top_k_values, scoring_method)

        all_results[scoring_method] = {
            "summary": summary,
            "cdf": cdf,
            "noise": noise,
            "failure": failure,
            "per_question": results,
        }

    # Save all results
    output_path = _ROOT / "results" / "module_constrained_retrieval_full.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print(f"\n\nSaved full results to: {output_path}")


if __name__ == "__main__":
    main()
