#!/usr/bin/env python3
"""
Module Prediction Accuracy 实验。

核心问题：Question 能不能预测到包含 gold function 的 Module？

实验设计：
1. 用 clangd CALL Graph + 同文件关系做 Louvain 社区发现，得到 Module。
2. 对每个 Question，用 function embedding 计算每个 module 的分数：
   score(module, question) = max_{f in module} cos(question_emb, f_emb)
3. 取 Top-K modules，检查是否覆盖 gold functions。

输出指标：
- Module Recall@K：至少一个 gold function 落在 top-K modules 中的比例
- Full Coverage@K：所有 gold functions 都落在 top-K modules 中的比例
- Gold Module Hit@K：gold function 所属的 module 在 top-K 中的比例
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import networkx as nx
import numpy as np

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from src.core.neo4j_client import run_cypher
from src.qa.retrievers.fast_embedding import FastEmbeddingRetriever


def load_benchmark(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data["items"]


def load_embedding_index(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    chunks = data["chunks"]
    embs = np.asarray(data["embeddings"], dtype=np.float32)
    return chunks, embs


def fetch_functions_and_calls():
    print("[1/4] Fetching functions from Neo4j...")
    func_records = run_cypher("MATCH (f:Function) RETURN f.id, f.name, f.file_path, f.start_line, f.end_line")
    functions = {r["f.id"]: r for r in func_records}
    print(f"      Functions: {len(functions)}")

    print("[2/4] Fetching CALLS edges from Neo4j...")
    call_records = run_cypher("MATCH (a:Function)-[:CALLS]->(b:Function) RETURN a.id AS caller, b.id AS callee")
    calls = [(r["caller"], r["callee"]) for r in call_records if r["caller"] != r["callee"]]
    print(f"      Calls: {len(calls)}")

    return functions, calls


def detect_modules(functions: dict, calls: list[tuple], resolution: float = 0.3, same_file_weight: float = 0.5):
    print(f"[3/4] Detecting modules (resolution={resolution}, same_file_weight={same_file_weight})...")

    G = nx.Graph()
    for fid, f in functions.items():
        G.add_node(fid, name=f["f.name"], file_path=f["f.file_path"])

    seen_calls = set()
    for caller, callee in calls:
        key = tuple(sorted([caller, callee]))
        if key in seen_calls:
            continue
        seen_calls.add(key)
        if G.has_edge(caller, callee):
            G[caller][callee]["weight"] += 1.0
        else:
            G.add_edge(caller, callee, weight=1.0)

    file_to_funcs = defaultdict(list)
    for fid, f in functions.items():
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
    for fid, comm_id in partition.items():
        communities[comm_id].append(fid)

    large_communities = {cid: members for cid, members in communities.items() if len(members) >= 10}
    small_communities = {cid: members for cid, members in communities.items() if len(members) < 10}

    if large_communities:
        for small_cid, small_members in small_communities.items():
            best_large_cid = None
            best_score = -1
            for large_cid, large_members in large_communities.items():
                score = sum(1 for m in small_members for lm in large_members if G.has_edge(m, lm))
                if score > best_score:
                    best_score = score
                    best_large_cid = large_cid
            if best_large_cid is None or best_score == 0:
                best_large_cid = max(large_communities, key=lambda c: len(large_communities[c]))
            large_communities[best_large_cid].extend(small_members)
        communities = large_communities

    func_to_module = {}
    module_to_funcs = {}
    for cid, members in communities.items():
        module_id = f"module:{cid}"
        module_to_funcs[module_id] = members
        for fid in members:
            func_to_module[fid] = module_id

    print(f"      Modules: {len(communities)}, assigned functions: {len(func_to_module)}")
    return func_to_module, module_to_funcs


def build_line_to_function_lookup(chunks: list[dict]):
    lookup = {}
    for ch in chunks:
        meta = ch.get("meta", {})
        fp = meta.get("file_path", "")
        fid = ch.get("id", "")
        start = meta.get("start_line", 0)
        end = meta.get("end_line", 0)
        for line in range(start, end + 1):
            lookup[(fp, line)] = fid
    return lookup


def gold_evidence_to_function_ids(item: dict, line_lookup: dict, chunks_by_id: dict):
    gold_fids = set()
    for ev in item.get("gold_evidence", []):
        file_path = ev.get("file", "")
        line = ev.get("line_start", 0)
        symbol = ev.get("symbol", "")

        fid = line_lookup.get((file_path, line))
        if fid is None and symbol and file_path:
            for ch in chunks_by_id.values():
                meta = ch.get("meta", {})
                if meta.get("file_path") == file_path and meta.get("name") == symbol:
                    fid = ch.get("id", "")
                    break
        if fid:
            gold_fids.add(fid)
    return gold_fids


def score_modules_by_max_function_similarity(
    question_emb: np.ndarray,
    module_to_funcs: dict,
    doc_matrix: np.ndarray,
    chunk_index_by_id: dict,
) -> list[tuple[str, float]]:
    """
    score(module) = max_{f in module} cos(question_emb, f_emb)
    """
    scores = []
    for module_id, func_ids in module_to_funcs.items():
        # 收集 module 内所有函数的 embedding 索引
        idxs = []
        for fid in func_ids:
            idx = chunk_index_by_id.get(fid)
            if idx is not None:
                idxs.append(idx)
        if not idxs:
            continue
        module_embs = doc_matrix[idxs]
        sims = module_embs @ question_emb.T
        max_sim = float(np.max(sims))
        scores.append((module_id, max_sim))

    scores.sort(key=lambda x: -x[1])
    return scores


def evaluate_module_prediction(
    items: list[dict],
    retriever: FastEmbeddingRetriever,
    doc_matrix: np.ndarray,
    chunk_index_by_id: dict,
    func_to_module: dict,
    module_to_funcs: dict,
    line_lookup: dict,
    chunks_by_id: dict,
    k_values: list[int] = (1, 3, 5, 10),
):
    print("[4/4] Evaluating module prediction...")

    # 指标统计
    recall_at_k = {k: 0 for k in k_values}
    full_coverage_at_k = {k: 0 for k in k_values}
    gold_module_hit_at_k = {k: 0 for k in k_values}

    per_question = []
    total_gold_funcs = 0

    for item in items:
        qid = item["qa_id"]
        question = item["question"]

        gold_fids = gold_evidence_to_function_ids(item, line_lookup, chunks_by_id)
        if not gold_fids:
            continue

        total_gold_funcs += len(gold_fids)

        # gold 所属的 module
        gold_modules = {func_to_module.get(fid) for fid in gold_fids}
        gold_modules.discard(None)

        # question embedding
        q_emb = retriever.encode_queries([question])[0]

        # module ranking
        ranked_modules = score_modules_by_max_function_similarity(
            q_emb, module_to_funcs, doc_matrix, chunk_index_by_id
        )
        top_module_ids = {mid for mid, _ in ranked_modules[:max(k_values)]}

        q_result = {
            "qa_id": qid,
            "gold_count": len(gold_fids),
            "gold_modules": list(gold_modules),
            "recall": {},
            "full_coverage": {},
            "gold_module_hit": {},
            "top_modules": [{"id": mid, "score": float(score)} for mid, score in ranked_modules[:10]],
        }

        for k in k_values:
            top_k_modules = {mid for mid, _ in ranked_modules[:k]}

            # Recall@K: 至少一个 gold function 在 top-K modules 中
            covered_golds = [fid for fid in gold_fids if func_to_module.get(fid) in top_k_modules]
            recall = len(covered_golds) > 0
            q_result["recall"][k] = recall
            if recall:
                recall_at_k[k] += 1

            # Full Coverage@K: 所有 gold functions 都在 top-K modules 中
            full = len(covered_golds) == len(gold_fids)
            q_result["full_coverage"][k] = full
            if full:
                full_coverage_at_k[k] += 1

            # Gold Module Hit@K: gold 所属的 module 有多少在 top-K 中
            hit_modules = gold_modules & top_k_modules
            hit = len(hit_modules) > 0
            q_result["gold_module_hit"][k] = hit
            if hit:
                gold_module_hit_at_k[k] += 1

        per_question.append(q_result)

    n = len(items)
    return {
        "recall_at_k": {k: recall_at_k[k] / n for k in k_values},
        "full_coverage_at_k": {k: full_coverage_at_k[k] / n for k in k_values},
        "gold_module_hit_at_k": {k: gold_module_hit_at_k[k] / n for k in k_values},
        "per_question": per_question,
        "total_gold_functions": total_gold_funcs,
    }


def print_report(report: dict, resolution: float, module_count: int, k_values: list[int]):
    print("\n" + "=" * 70)
    print(f"MODULE PREDICTION ACCURACY (resolution={resolution}, modules={module_count})")
    print("=" * 70)

    print("\n[Module Recall@K]: 至少一个 gold function 落在 top-K modules 中")
    for k in k_values:
        print(f"  Recall@{k:2d}: {report['recall_at_k'][k]*100:5.1f}%")

    print("\n[Full Coverage@K]: 所有 gold functions 都落在 top-K modules 中")
    for k in k_values:
        print(f"  Coverage@{k:2d}: {report['full_coverage_at_k'][k]*100:5.1f}%")

    print("\n[Gold Module Hit@K]: 至少一个 gold 所属 module 在 top-K 中")
    for k in k_values:
        print(f"  Hit@{k:2d}: {report['gold_module_hit_at_k'][k]*100:5.1f}%")

    print(f"\nTotal questions: {len(report['per_question'])}")
    print(f"Total gold functions: {report['total_gold_functions']}")


def save_report(report: dict, resolution: float, output_path: Path):
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\nSaved detailed report to: {output_path}")


def main():
    benchmark_path = _ROOT / "datasets" / "benchmark_hard.json"
    index_path = _ROOT / "data" / "qa_embedding_index.json"

    items = load_benchmark(benchmark_path)
    chunks, doc_matrix = load_embedding_index(index_path)
    chunks_by_id = {ch.get("id", ""): ch for ch in chunks}
    chunk_index_by_id = {ch.get("id", ""): idx for idx, ch in enumerate(chunks)}

    functions, calls = fetch_functions_and_calls()
    retriever = FastEmbeddingRetriever(index_path=index_path)
    line_lookup = build_line_to_function_lookup(chunks)

    k_values = [1, 3, 5, 10]

    for resolution in [0.3, 0.5, 1.0]:
        print("\n" + "-" * 70)
        func_to_module, module_to_funcs = detect_modules(functions, calls, resolution=resolution)

        report = evaluate_module_prediction(
            items, retriever, doc_matrix, chunk_index_by_id,
            func_to_module, module_to_funcs, line_lookup, chunks_by_id,
            k_values=k_values,
        )

        print_report(report, resolution, len(module_to_funcs), k_values)

        output_path = _ROOT / "results" / f"module_prediction_accuracy_res{resolution}.json"
        save_report(report, resolution, output_path)


if __name__ == "__main__":
    main()
