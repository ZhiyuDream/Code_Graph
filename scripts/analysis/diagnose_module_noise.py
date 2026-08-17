#!/usr/bin/env python3
"""
诊断：Top-K 候选函数中的噪音，有多少来自 gold 所在 Module 之外？

目的：验证 "Module 作为过滤器" 这个假设是否成立。

输出指标：
- 对所有 top-K 候选：same-module / other-module / no-module 比例
- 对 missed gold：其前面 top-K 候选的模块分布
- 不同 resolution 下的模块数量和噪音比例
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
    return data["chunks"]


def fetch_functions_and_calls():
    """从 Neo4j 拉取所有 Function 节点和 CALLS 边。"""
    print("[1/5] Fetching functions from Neo4j...")
    func_records = run_cypher("MATCH (f:Function) RETURN f.id, f.name, f.file_path, f.start_line, f.end_line")
    functions = {r["f.id"]: r for r in func_records}
    print(f"      Functions: {len(functions)}")

    print("[2/5] Fetching CALLS edges from Neo4j...")
    call_records = run_cypher("MATCH (a:Function)-[:CALLS]->(b:Function) RETURN a.id AS caller, b.id AS callee")
    calls = [(r["caller"], r["callee"]) for r in call_records if r["caller"] != r["callee"]]
    print(f"      Calls: {len(calls)}")

    return functions, calls


def detect_modules(functions: dict, calls: list[tuple], resolution: float = 0.3, same_file_weight: float = 0.5):
    """基于调用图 + 同文件关系做 Louvain 社区发现。"""
    print(f"[3/5] Detecting modules (resolution={resolution}, same_file_weight={same_file_weight})...")

    G = nx.Graph()
    for fid, f in functions.items():
        G.add_node(fid, name=f["f.name"], file_path=f["f.file_path"])

    # CALLS 边
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

    # 同文件边
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

    # 合并过小社区到最近的大社区
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
    for cid, members in communities.items():
        module_id = f"module:{cid}"
        for fid in members:
            func_to_module[fid] = module_id

    print(f"      Modules: {len(communities)}, assigned functions: {len(func_to_module)}")
    return func_to_module, communities


def build_line_to_function_lookup(chunks: list[dict]):
    """构建 (file_path, line) -> function_id 映射。"""
    lookup = {}
    for ch in chunks:
        meta = ch.get("meta", {})
        fp = meta.get("file_path", "")
        name = meta.get("name", "")
        start = meta.get("start_line", 0)
        end = meta.get("end_line", 0)
        fid = ch.get("id", "")
        if not fid:
            fid = f"{fp}:{name}:{start}"
        for line in range(start, end + 1):
            lookup[(fp, line)] = fid
    return lookup


def gold_evidence_to_function_ids(item: dict, line_lookup: dict, chunks_by_id: dict):
    """把 benchmark 中的 gold_evidence 映射到 function ids。"""
    gold_funcs = []
    for ev in item.get("gold_evidence", []):
        file_path = ev.get("file", "")
        line = ev.get("line_start", 0)
        symbol = ev.get("symbol", "")

        # 先用行号定位
        fid = line_lookup.get((file_path, line))

        # 如果行号定位失败，用 symbol + file 匹配
        if fid is None and symbol and file_path:
            for ch in chunks_by_id.values():
                meta = ch.get("meta", {})
                if meta.get("file_path") == file_path and meta.get("name") == symbol:
                    fid = ch.get("id", "")
                    break

        if fid:
            gold_funcs.append({
                "fid": fid,
                "file": file_path,
                "symbol": symbol,
                "evidence_id": ev.get("evidence_id", ""),
            })

    return gold_funcs


def get_top_k_function_ids(retriever: FastEmbeddingRetriever, question: str, k: int = 50):
    """返回 question 的 top-k function ids。"""
    q_emb = retriever.encode_queries([question])
    results = retriever.retrieve(q_emb, top_k=k)
    return [
        {
            "fid": f"{r['metadata']['file_path']}:{r['metadata']['name']}:{r['metadata']['start_line']}",
            "file_path": r["metadata"]["file_path"],
            "name": r["metadata"]["name"],
            "score": r["score"],
        }
        for r in results
    ]


def diagnose(items: list[dict], retriever: FastEmbeddingRetriever, func_to_module: dict,
             line_lookup: dict, chunks_by_id: dict, top_k: int = 50):
    """核心诊断逻辑。"""
    print(f"[4/5] Diagnosing {len(items)} questions (top_k={top_k})...")

    overall_counter = Counter({"same_module": 0, "other_module": 0, "no_module": 0, "is_gold": 0})
    missed_gold_counter = Counter({"same_module": 0, "other_module": 0, "no_module": 0})
    missed_gold_count = 0
    retrieved_gold_count = 0
    per_question_stats = []

    for item in items:
        qid = item["qa_id"]
        question = item["question"]

        gold_funcs = gold_evidence_to_function_ids(item, line_lookup, chunks_by_id)
        gold_fids = {g["fid"] for g in gold_funcs}

        if not gold_fids:
            continue

        top_funcs = get_top_k_function_ids(retriever, question, k=top_k)
        top_fids = [f["fid"] for f in top_funcs]

        # 哪些 gold 在 top-k 里
        retrieved_golds = gold_fids & set(top_fids)
        missed_golds = gold_fids - set(top_fids)
        retrieved_gold_count += len(retrieved_golds)
        missed_gold_count += len(missed_golds)

        # 统计 gold function 的 module 分布
        gold_modules = {func_to_module.get(fid, None) for fid in gold_fids}
        gold_modules.discard(None)

        q_stats = {
            "qa_id": qid,
            "gold_count": len(gold_fids),
            "retrieved_gold_count": len(retrieved_golds),
            "missed_gold_count": len(missed_golds),
            "gold_modules": list(gold_modules),
            "top50_module_distribution": Counter(),
        }

        for f in top_funcs:
            fid = f["fid"]
            mod = func_to_module.get(fid)

            if fid in gold_fids:
                overall_counter["is_gold"] += 1
                q_stats["top50_module_distribution"]["is_gold"] += 1
            elif mod is None:
                overall_counter["no_module"] += 1
                q_stats["top50_module_distribution"]["no_module"] += 1
            elif mod in gold_modules:
                overall_counter["same_module"] += 1
                q_stats["top50_module_distribution"]["same_module"] += 1
            else:
                overall_counter["other_module"] += 1
                q_stats["top50_module_distribution"]["other_module"] += 1

        # 对 missed gold，统计排在前面的候选来自哪些模块
        for missed_fid in missed_golds:
            missed_mod = func_to_module.get(missed_fid)
            if missed_mod is None:
                continue

            # 排在 missed gold 前面的函数（即所有 top-k 候选，因为 missed 不在里面）
            for f in top_funcs:
                fid = f["fid"]
                mod = func_to_module.get(fid)
                if fid in gold_fids:
                    continue
                if mod == missed_mod:
                    missed_gold_counter["same_module"] += 1
                elif mod is None:
                    missed_gold_counter["no_module"] += 1
                else:
                    missed_gold_counter["other_module"] += 1

        per_question_stats.append(q_stats)

    print(f"[5/5] Done. Retrieved gold: {retrieved_gold_count}, Missed gold: {missed_gold_count}")
    return overall_counter, missed_gold_counter, per_question_stats


def print_report(overall: Counter, missed: Counter, per_q: list[dict], top_k: int):
    print("\n" + "=" * 60)
    print("MODULE NOISE DIAGNOSTIC REPORT")
    print("=" * 60)

    def pct(part, total):
        return f"{part / total * 100:.1f}%" if total else "N/A"

    total_overall = sum(overall.values()) - overall["is_gold"]
    print(f"\n[Overall Top-{top_k} candidates (excluding gold hits)]")
    print(f"  Total non-gold candidates: {total_overall}")
    print(f"  Same module as gold:       {overall['same_module']:>6} ({pct(overall['same_module'], total_overall)})")
    print(f"  Other module:              {overall['other_module']:>6} ({pct(overall['other_module'], total_overall)})")
    print(f"  No module assigned:        {overall['no_module']:>6} ({pct(overall['no_module'], total_overall)})")

    total_missed = sum(missed.values())
    print(f"\n[Candidates ahead of missed gold functions]")
    print(f"  Total candidates ahead:    {total_missed}")
    print(f"  Same module as missed gold:{missed['same_module']:>6} ({pct(missed['same_module'], total_missed)})")
    print(f"  Other module:              {missed['other_module']:>6} ({pct(missed['other_module'], total_missed)})")
    print(f"  No module assigned:        {missed['no_module']:>6} ({pct(missed['no_module'], total_missed)})")

    # 问题级别的 breakdown
    print(f"\n[Per-question breakdown (selected)]")
    for q in per_q[:10]:
        dist = q["top50_module_distribution"]
        total = sum(dist.values()) - dist.get("is_gold", 0)
        if total == 0:
            continue
        other = dist.get("other_module", 0)
        same = dist.get("same_module", 0)
        print(f"  {q['qa_id']}: missed={q['missed_gold_count']}/{q['gold_count']}, "
              f"gold_modules={len(q['gold_modules'])}, "
              f"other={other}/{total} ({pct(other, total)}), "
              f"same={same}/{total} ({pct(same, total)})")


def main():
    benchmark_path = _ROOT / "datasets" / "benchmark_hard.json"
    index_path = _ROOT / "data" / "qa_embedding_index.json"

    items = load_benchmark(benchmark_path)
    chunks = load_embedding_index(index_path)
    chunks_by_id = {ch.get("id", ""): ch for ch in chunks}

    functions, calls = fetch_functions_and_calls()

    retriever = FastEmbeddingRetriever(index_path=index_path)

    # 尝试不同 resolution
    for resolution in [0.3, 0.5, 1.0]:
        print("\n" + "-" * 60)
        print(f"Resolution = {resolution}")
        print("-" * 60)
        func_to_module, communities = detect_modules(functions, calls, resolution=resolution)
        line_lookup = build_line_to_function_lookup(chunks)

        overall, missed, per_q = diagnose(
            items, retriever, func_to_module, line_lookup, chunks_by_id, top_k=50
        )
        print_report(overall, missed, per_q, top_k=50)


if __name__ == "__main__":
    main()
