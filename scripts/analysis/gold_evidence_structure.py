#!/usr/bin/env python3
"""
Gold Evidence Structure Analysis。

目标：理解每道题的 gold evidence 在仓库中的组织结构。

分析维度：
1. Gold module 数量分布
2. Gold module 之间的调用关系（module-level call graph）
3. Gold module 的目录关系
4. Gold module 的共享调用方/被调用方
5. Evidence pattern 分类：
   - single: 单个 module
   - chain: 调用链（A→B→C）
   - star: 星型（一个 hub module 连接多个）
   - cluster: 密集连接
   - tree: 树形/分支
   - independent: 无调用关系
   - utility_mixed: 包含明显 utility module
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import networkx as nx

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from src.core.neo4j_client import run_cypher
from scripts.analysis.eval_module_constrained_retrieval import (
    detect_modules,
    fetch_functions_and_calls,
    load_benchmark,
    load_embedding_index,
    build_helpers,
    gold_evidence_to_function_ids,
)


UTILITY_KEYWORDS = ["log", "utils", "common", "debug", "error", "assert", "memory", "alloc", "string", "hash"]


def is_utility_module(module_name_or_files: str | list[str]) -> bool:
    """启发式判断 module 是否是 utility。"""
    text = ""
    if isinstance(module_name_or_files, str):
        text = module_name_or_files.lower()
    else:
        text = " ".join(module_name_or_files).lower()
    return any(kw in text for kw in UTILITY_KEYWORDS)


def fetch_call_graph():
    """获取 function-level 调用图。"""
    records = run_cypher("MATCH (a:Function)-[:CALLS]->(b:Function) RETURN a.id AS caller, b.id AS callee")
    return [(r["caller"], r["callee"]) for r in records if r["caller"] != r["callee"]]


def build_module_call_graph(func_calls: list[tuple], func_to_module: dict):
    """构建 module-level 调用图。"""
    G = nx.DiGraph()
    module_pairs = Counter()

    for caller, callee in func_calls:
        m1 = func_to_module.get(caller)
        m2 = func_to_module.get(callee)
        if m1 and m2 and m1 != m2:
            module_pairs[(m1, m2)] += 1

    for (m1, m2), weight in module_pairs.items():
        G.add_edge(m1, m2, weight=weight)

    return G


def classify_module_pattern(gold_modules: set, module_call_graph: nx.DiGraph):
    """对 gold modules 的拓扑结构分类。"""
    if len(gold_modules) <= 1:
        return "single"

    subgraph = module_call_graph.subgraph(gold_modules).copy()
    n = len(gold_modules)
    m = subgraph.number_of_edges()

    if m == 0:
        return "independent"

    # 检查是否 chain：n 个节点，n-1 条边，且是连通有向路径
    if m == n - 1:
        # 检查是否是单一路径
        in_degrees = dict(subgraph.in_degree())
        out_degrees = dict(subgraph.out_degree())
        start_nodes = [node for node, d in in_degrees.items() if d == 0]
        end_nodes = [node for node, d in out_degrees.items() if d == 0]
        if len(start_nodes) == 1 and len(end_nodes) == 1:
            # 进一步检查是否没有分支
            if all(d <= 1 for d in in_degrees.values()) and all(d <= 1 for d in out_degrees.values()):
                return "chain"

    # 检查 star：一个 hub 连接 ≥2 个其他节点
    undirected = subgraph.to_undirected()
    degrees = dict(undirected.degree())
    max_degree = max(degrees.values()) if degrees else 0
    if max_degree >= 2 and m <= n:
        hub = max(degrees, key=degrees.get)
        if degrees[hub] >= n - 1:
            return "star"

    # 检查 tree：n 个节点，n-1 条边，无环
    if m == n - 1 and nx.is_tree(undirected):
        return "tree"

    # 检查 cluster：边很多，几乎完全连接
    max_edges = n * (n - 1)
    if max_edges > 0 and m / max_edges >= 0.3:
        return "cluster"

    # 默认：connected（有连接但不符合以上模式）
    return "connected"


def analyze_directory_relationship(gold_modules: set, func_to_module: dict, module_to_funcs: dict, func_to_file: dict):
    """分析 gold modules 的目录关系。"""
    module_dirs = {}
    for mid in gold_modules:
        files = [func_to_file.get(fid, "") for fid in module_to_funcs.get(mid, []) if func_to_file.get(fid)]
        dirs = [str(Path(f).parent) for f in files if f]
        module_dirs[mid] = set(dirs)

    same_dir_count = 0
    pair_count = 0
    common_parent_count = 0

    modules = list(gold_modules)
    for i in range(len(modules)):
        for j in range(i + 1, len(modules)):
            m1, m2 = modules[i], modules[j]
            pair_count += 1
            if module_dirs[m1] & module_dirs[m2]:
                same_dir_count += 1
            # 检查是否有共同父目录
            parents1 = {str(Path(d).parent) for d in module_dirs[m1]}
            parents2 = {str(Path(d).parent) for d in module_dirs[m2]}
            if parents1 & parents2:
                common_parent_count += 1

    return {
        "pair_count": pair_count,
        "same_dir_pairs": same_dir_count,
        "common_parent_pairs": common_parent_count,
        "module_dirs": {mid: list(dirs) for mid, dirs in module_dirs.items()},
    }


def analyze_shared_callers(
    gold_modules: set,
    module_to_funcs: dict,
    func_to_module: dict,
    func_calls: list[tuple],
):
    """分析是否有函数同时调用多个 gold module。"""
    # caller -> set of modules it calls into
    caller_to_gold_modules = defaultdict(set)

    for caller, callee in func_calls:
        callee_module = func_to_module.get(callee)
        if callee_module in gold_modules:
            caller_to_gold_modules[caller].add(callee_module)

    shared_callers = {
        caller: modules
        for caller, modules in caller_to_gold_modules.items()
        if len(modules) >= 2
    }

    return shared_callers


def analyze_question(
    item: dict,
    line_lookup: dict,
    chunks_by_id: dict,
    func_to_module: dict,
    module_to_funcs: dict,
    func_to_file: dict,
    module_call_graph: nx.DiGraph,
    func_calls: list[tuple],
):
    gold_fids = gold_evidence_to_function_ids(item, line_lookup, chunks_by_id)
    if not gold_fids:
        return None

    gold_modules = {func_to_module.get(fid) for fid in gold_fids}
    gold_modules.discard(None)

    pattern = classify_module_pattern(gold_modules, module_call_graph)

    # 检查是否包含 utility module
    module_files = {}
    for mid in gold_modules:
        files = [func_to_file.get(fid, "") for fid in module_to_funcs.get(mid, []) if func_to_file.get(fid)]
        module_files[mid] = files
    has_utility = any(is_utility_module(files) for files in module_files.values())

    if has_utility and pattern != "single":
        pattern = f"{pattern}_utility"

    dir_analysis = analyze_directory_relationship(gold_modules, func_to_module, module_to_funcs, func_to_file)
    shared_callers = analyze_shared_callers(gold_modules, module_to_funcs, func_to_module, func_calls)

    # module-level call subgraph details
    subgraph = module_call_graph.subgraph(gold_modules).copy() if len(gold_modules) > 1 else nx.DiGraph()

    return {
        "qa_id": item["qa_id"],
        "question": item["question"],
        "gold_count": len(gold_fids),
        "gold_modules": list(gold_modules),
        "gold_module_count": len(gold_modules),
        "pattern": pattern,
        "module_edges": list(subgraph.edges(data=True)),
        "module_edge_count": subgraph.number_of_edges(),
        "dir_analysis": dir_analysis,
        "shared_caller_count": len(shared_callers),
        "shared_callers": list(shared_callers.keys())[:10],
    }


def print_report(analyses: list[dict], pattern_counter: Counter):
    print("\n" + "=" * 75)
    print("GOLD EVIDENCE STRUCTURE ANALYSIS")
    print("=" * 75)

    print("\n[Gold Module Count Distribution]")
    module_count_dist = Counter(a["gold_module_count"] for a in analyses)
    for k in sorted(module_count_dist.keys()):
        print(f"  {k} module(s): {module_count_dist[k]} questions ({module_count_dist[k]/len(analyses)*100:.1f}%)")
    print(f"  Mean: {sum(a['gold_module_count'] for a in analyses)/len(analyses):.2f}")

    print("\n[Evidence Pattern Distribution]")
    for pattern, count in pattern_counter.most_common():
        print(f"  {pattern:<25}: {count} questions ({count/len(analyses)*100:.1f}%)")

    print("\n[Directory Relationship]")
    total_pairs = sum(a["dir_analysis"]["pair_count"] for a in analyses)
    same_dir = sum(a["dir_analysis"]["same_dir_pairs"] for a in analyses)
    common_parent = sum(a["dir_analysis"]["common_parent_pairs"] for a in analyses)
    print(f"  Total module pairs: {total_pairs}")
    print(f"  Same directory: {same_dir} ({same_dir/total_pairs*100:.1f}%)")
    print(f"  Common parent dir: {common_parent} ({common_parent/total_pairs*100:.1f}%)")

    print("\n[Shared Callers Across Gold Modules]")
    questions_with_shared = sum(1 for a in analyses if a["shared_caller_count"] > 0)
    print(f"  Questions with shared callers: {questions_with_shared}/{len(analyses)} ({questions_with_shared/len(analyses)*100:.1f}%)")

    print("\n[Examples per Pattern]")
    seen_patterns = set()
    for a in analyses:
        if a["pattern"] not in seen_patterns:
            seen_patterns.add(a["pattern"])
            print(f"\n  [{a['pattern']}] {a['qa_id']}")
            print(f"    Question: {a['question'][:80]}...")
            print(f"    Gold modules: {a['gold_module_count']}")
            print(f"    Module edges: {a['module_edge_count']}")
            print(f"    Shared callers: {a['shared_caller_count']}")


def main():
    benchmark_path = _ROOT / "datasets" / "benchmark_hard.json"
    index_path = _ROOT / "data" / "qa_embedding_index.json"

    items = load_benchmark(benchmark_path)
    chunks, _ = load_embedding_index(index_path)
    line_lookup, chunks_by_id, _, func_to_file, _ = build_helpers(chunks)

    print("[1/4] Fetching functions and calls...")
    funcs, _ = fetch_functions_and_calls()

    print("[2/4] Detecting modules (random_state=42)...")
    func_to_module, module_to_funcs = detect_modules(funcs, [], resolution=0.5)

    print("[3/4] Building module-level call graph...")
    func_calls = fetch_call_graph()
    module_call_graph = build_module_call_graph(func_calls, func_to_module)
    print(f"      Module-level edges: {module_call_graph.number_of_edges()}")

    print("[4/4] Analyzing gold evidence structure...")
    analyses = []
    pattern_counter = Counter()

    for item in items:
        analysis = analyze_question(
            item, line_lookup, chunks_by_id, func_to_module, module_to_funcs,
            func_to_file, module_call_graph, func_calls,
        )
        if analysis:
            analyses.append(analysis)
            pattern_counter[analysis["pattern"]] += 1

    print_report(analyses, pattern_counter)

    output_path = _ROOT / "results" / "gold_evidence_structure.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({
            "module_count": len(module_to_funcs),
            "pattern_distribution": dict(pattern_counter),
            "per_question": analyses,
        }, f, ensure_ascii=False, indent=2)
    print(f"\nSaved detailed results to: {output_path}")


if __name__ == "__main__":
    main()
