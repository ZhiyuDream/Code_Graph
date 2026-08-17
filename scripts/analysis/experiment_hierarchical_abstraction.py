#!/usr/bin/env python3
"""Build a scalable Module -> Concept hierarchy from Tree-sitter artifacts.

This is an offline experiment.  It deliberately does not read or write Neo4j:

* Modules are communities in a weighted *file* graph (includes, lexical calls,
  and a sparse directory prior).
* Concepts are communities in per-module function k-NN graphs (embedding,
  lexical calls, class/namespace and sparse file priors).

The output uses the existing ModuleAbstraction/ConceptAbstraction JSON schema,
so it can be consumed by the current navigation and QA scripts.
"""
from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath

import networkx as nx
import numpy as np
from community import community_louvain
from sklearn.neighbors import NearestNeighbors


THIRD_PARTY_PREFIXES = ("vendor/", "third_party/", "deps/")
SOURCE_SUFFIXES = (".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx")


def add_weight(graph: nx.Graph, a: str, b: str, weight: float, source: str) -> None:
    if a == b or weight <= 0:
        return
    if graph.has_edge(a, b):
        graph[a][b]["weight"] += weight
        graph[a][b][source] = graph[a][b].get(source, 0.0) + weight
    else:
        graph.add_edge(a, b, weight=weight, **{source: weight})


def retained_files(extraction: dict) -> list[dict]:
    return [
        item for item in extraction["files"]
        if not item["file_path"].startswith(THIRD_PARTY_PREFIXES)
    ]


def resolve_includes(files: list[dict]) -> list[tuple[str, str]]:
    paths = {item["file_path"] for item in files}
    by_name: dict[str, list[str]] = defaultdict(list)
    for path in paths:
        by_name[PurePosixPath(path).name].append(path)

    edges = set()
    include_re = re.compile(r'#include\s*[<"]([^>"]+)[>"]')
    for item in files:
        source = item["file_path"]
        parent = PurePosixPath(source).parent
        for raw in item.get("raw", {}).get("includes", []):
            match = include_re.search(raw)
            if not match:
                continue
            target = match.group(1)
            local = str(parent / target)
            resolved = None
            if local in paths:
                resolved = local
            elif target in paths:
                resolved = target
            else:
                candidates = by_name.get(PurePosixPath(target).name, [])
                if len(candidates) == 1:
                    resolved = candidates[0]
            if resolved and resolved != source:
                edges.add((source, resolved))
    return sorted(edges)


def resolve_calls(files: list[dict]) -> tuple[list[tuple[str, str, float]], dict]:
    """Resolve lexical calls conservatively and preserve confidence."""
    by_tail: dict[str, list[dict]] = defaultdict(list)
    by_file_tail: dict[tuple[str, str], list[dict]] = defaultdict(list)
    function_file: dict[str, str] = {}
    for item in files:
        path = item["file_path"]
        for fn in item["functions"]:
            tail = fn["name"].split("::")[-1]
            by_tail[tail].append(fn)
            by_file_tail[(path, tail)].append(fn)
            function_file[fn["id"]] = path

    def unique(candidates: list[dict]) -> str | None:
        definitions = [fn for fn in candidates if fn.get("is_definition", True)]
        ids = {fn["id"] for fn in (definitions or candidates)}
        return next(iter(ids)) if len(ids) == 1 else None

    edge_confidence: dict[tuple[str, str], float] = {}
    stats = Counter()
    for item in files:
        path = item["file_path"]
        functions = item["functions"]
        for call in item["calls"]:
            stats["lexical_calls"] += 1
            caller_index = int(call["caller_index"])
            if not 0 <= caller_index < len(functions):
                stats["invalid_caller"] += 1
                continue
            caller = functions[caller_index]["id"]
            tail = call["callee_name"].split("::")[-1]
            local = by_file_tail.get((path, tail), [])
            callee = unique(local)
            confidence = 0.95
            if callee is None and not local:
                global_candidates = by_tail.get(tail, [])
                callee = unique(global_candidates)
                confidence = 0.60
                if callee is None:
                    stats["ambiguous_or_external"] += 1
            elif callee is None:
                stats["ambiguous_local"] += 1
            if callee and callee != caller:
                key = (caller, callee)
                edge_confidence[key] = max(edge_confidence.get(key, 0.0), confidence)

    stats["resolved_edges"] = len(edge_confidence)
    return [(a, b, c) for (a, b), c in sorted(edge_confidence.items())], dict(stats)


def build_file_graph(
    files: list[dict],
    includes: list[tuple[str, str]],
    calls: list[tuple[str, str, float]],
    include_weight: float,
    call_weight: float,
    directory_weight: float,
) -> tuple[nx.Graph, dict[str, list[str]]]:
    graph = nx.Graph()
    file_functions = {item["file_path"]: [fn["id"] for fn in item["functions"]] for item in files}
    function_file = {fid: path for path, fids in file_functions.items() for fid in fids}
    graph.add_nodes_from(file_functions)

    for source, target in includes:
        add_weight(graph, source, target, include_weight, "include_weight")

    aggregated_calls: dict[tuple[str, str], float] = defaultdict(float)
    for caller, callee, confidence in calls:
        source, target = function_file.get(caller), function_file.get(callee)
        if source and target and source != target:
            key = tuple(sorted((source, target)))
            aggregated_calls[key] += confidence
    for (source, target), confidence_sum in aggregated_calls.items():
        normalizer = math.sqrt(max(1, len(file_functions[source])) * max(1, len(file_functions[target])))
        add_weight(
            graph, source, target,
            call_weight * math.log1p(confidence_sum) / math.sqrt(normalizer),
            "call_weight",
        )

    # Sparse star per immediate directory: O(files), not a same-directory clique.
    directory_files: dict[str, list[str]] = defaultdict(list)
    for path in file_functions:
        directory_files[str(PurePosixPath(path).parent)].append(path)
    for members in directory_files.values():
        if len(members) < 2:
            continue
        anchor = min(members, key=lambda p: (-len(file_functions[p]), p))
        for path in members:
            if path != anchor:
                add_weight(graph, anchor, path, directory_weight, "directory_weight")
    return graph, file_functions


def choose_partition(graph: nx.Graph, resolutions: list[float], target_modules: int) -> tuple[dict, dict]:
    trials = []
    best = None
    for resolution in resolutions:
        partition = community_louvain.best_partition(
            graph, weight="weight", resolution=resolution, random_state=42
        )
        count = len(set(partition.values()))
        modularity = community_louvain.modularity(partition, graph, weight="weight")
        trial = {"resolution": resolution, "modules": count, "modularity": modularity}
        trials.append(trial)
        score = (abs(count - target_modules), -modularity)
        if best is None or score < best[0]:
            best = (score, partition, trial)
    assert best is not None
    return best[1], {"selected": best[2], "trials": trials}


def load_embedding_index(path: Path) -> tuple[list[dict], np.ndarray, dict[str, int]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    chunks = payload["chunks"]
    matrix = np.asarray(payload["embeddings"], dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    matrix /= np.maximum(norms, 1e-12)
    return chunks, matrix, {chunk["id"]: i for i, chunk in enumerate(chunks)}


def merge_small_communities(
    graph: nx.Graph, communities: dict[int, list[str]], minimum: int
) -> list[list[str]]:
    large = {cid: list(nodes) for cid, nodes in communities.items() if len(nodes) >= minimum}
    small = {cid: nodes for cid, nodes in communities.items() if len(nodes) < minimum}
    if not large:
        return [list(graph.nodes)]
    node_community = {node: cid for cid, nodes in large.items() for node in nodes}
    for nodes in small.values():
        scores = Counter()
        for node in nodes:
            for neighbor, attrs in graph[node].items():
                cid = node_community.get(neighbor)
                if cid is not None:
                    scores[cid] += attrs.get("weight", 1.0)
        target = scores.most_common(1)[0][0] if scores else min(large, key=lambda cid: len(large[cid]))
        large[target].extend(nodes)
        for node in nodes:
            node_community[node] = target
    return list(large.values())


def cluster_concepts(
    module_id: str,
    function_ids: list[str],
    functions: dict[str, dict],
    calls: list[tuple[str, str, float]],
    matrix: np.ndarray,
    index_by_id: dict[str, int],
    knn: int,
    resolution: float,
    min_size: int,
) -> list[list[str]]:
    ids = [fid for fid in function_ids if fid in index_by_id]
    if len(ids) <= max(min_size, 2):
        return [ids] if ids else []
    graph = nx.Graph()
    graph.add_nodes_from(ids)
    positions = np.asarray([index_by_id[fid] for fid in ids])
    vectors = matrix[positions]
    neighbors = min(knn + 1, len(ids))
    distances, indices = NearestNeighbors(n_neighbors=neighbors, metric="cosine", n_jobs=-1).fit(vectors).kneighbors(vectors)
    for row, fid in enumerate(ids):
        for distance, local_index in zip(distances[row, 1:], indices[row, 1:]):
            similarity = 1.0 - float(distance)
            if similarity >= 0.18:
                add_weight(graph, fid, ids[int(local_index)], similarity, "embedding_weight")

    id_set = set(ids)
    for caller, callee, confidence in calls:
        if caller in id_set and callee in id_set:
            add_weight(graph, caller, callee, 1.5 * confidence, "call_weight")

    # Sparse structural priors for files and parent classes/namespaces.
    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for fid in ids:
        fn = functions[fid]
        groups[("file", fn["file_path"])].append(fid)
        owner = fn.get("parent_class") or "::".join(fn["name"].split("::")[:-1])
        if owner:
            groups[("owner", owner)].append(fid)
    for (kind, _), members in groups.items():
        if len(members) < 2:
            continue
        anchor = min(members)
        weight = 0.25 if kind == "file" else 0.65
        for fid in members:
            if fid != anchor:
                add_weight(graph, anchor, fid, weight, f"{kind}_weight")

    partition = community_louvain.best_partition(
        graph, weight="weight", resolution=resolution, random_state=42
    )
    communities: dict[int, list[str]] = defaultdict(list)
    for fid, cid in partition.items():
        communities[cid].append(fid)
    return merge_small_communities(graph, communities, min_size)


def describe_group(function_ids: list[str], functions: dict[str, dict], limit: int = 8) -> tuple[str, str]:
    names = [functions[fid]["name"] for fid in function_ids]
    tokens = Counter()
    for name in names:
        for token in re.findall(r"[A-Za-z][a-z]+|[A-Z]{2,}|[a-z]{3,}", name.replace("::", "_")):
            if token.lower() not in {"get", "set", "init", "free", "create", "make", "from", "with"}:
                tokens[token.lower()] += 1
    label = " / ".join(token for token, _ in tokens.most_common(3)) or "code group"
    samples = ", ".join(names[:limit])
    return label, f"Functions related to {label}; examples: {samples}"


def distribution(values: list[int]) -> dict:
    if not values:
        return {}
    array = np.asarray(values)
    return {
        "min": int(array.min()), "p25": float(np.percentile(array, 25)),
        "median": float(np.median(array)), "p75": float(np.percentile(array, 75)),
        "p90": float(np.percentile(array, 90)), "max": int(array.max()),
        "mean": float(array.mean()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extraction", type=Path, default=Path("results/tree_sitter_cpp_extraction_20260715.json"))
    parser.add_argument("--embedding-index", type=Path, default=Path("data/qa_embedding_index_tree_sitter.json"))
    parser.add_argument("--module-output", type=Path, default=Path("data/module_abstraction_hierarchical_20260718.json"))
    parser.add_argument("--concept-output", type=Path, default=Path("data/concept_abstraction_hierarchical_20260718.json"))
    parser.add_argument("--report", type=Path, default=Path("results/hierarchical_abstraction_experiment_20260718.json"))
    parser.add_argument("--target-modules", type=int, default=60)
    parser.add_argument("--module-resolutions", default="0.4,0.6,0.8,1.0,1.3,1.7,2.2,3.0")
    parser.add_argument("--concept-resolution", type=float, default=1.15)
    parser.add_argument("--concept-knn", type=int, default=10)
    parser.add_argument("--min-concept-size", type=int, default=5)
    args = parser.parse_args()

    extraction = json.loads(args.extraction.read_text(encoding="utf-8"))
    files = retained_files(extraction)
    functions = {
        fn["id"]: {**fn, "file_path": item["file_path"]}
        for item in files for fn in item["functions"]
    }
    includes = resolve_includes(files)
    calls, call_stats = resolve_calls(files)
    file_graph, file_functions = build_file_graph(files, includes, calls, 1.0, 0.8, 0.30)
    resolutions = [float(value) for value in args.module_resolutions.split(",")]
    partition, partition_report = choose_partition(file_graph, resolutions, args.target_modules)

    module_files: dict[int, list[str]] = defaultdict(list)
    for path, cid in partition.items():
        module_files[cid].append(path)
    ordered_modules = sorted(module_files.values(), key=lambda paths: (-sum(len(file_functions[p]) for p in paths), min(paths)))

    modules = []
    function_module = {}
    for number, paths in enumerate(ordered_modules):
        fids = [fid for path in sorted(paths) for fid in file_functions[path]]
        module_id = f"module:{number:03d}"
        label, summary = describe_group(fids, functions)
        modules.append({"id": module_id, "name": label, "summary": summary, "function_ids": fids, "file_paths": sorted(paths)})
        function_module.update({fid: module_id for fid in fids})

    print(f"File graph: {file_graph.number_of_nodes()} nodes, {file_graph.number_of_edges()} edges", flush=True)
    print(f"Selected modules: {len(modules)}; loading embeddings...", flush=True)
    chunks, matrix, index_by_id = load_embedding_index(args.embedding_index)

    concepts = []
    concepts_per_module = Counter()
    for index, module in enumerate(modules, 1):
        groups = cluster_concepts(
            module["id"], module["function_ids"], functions, calls,
            matrix, index_by_id, args.concept_knn, args.concept_resolution,
            args.min_concept_size,
        )
        for local_number, fids in enumerate(sorted(groups, key=lambda group: (-len(group), min(group)))):
            concept_id = f"{module['id']}:concept:{local_number:03d}"
            label, summary = describe_group(fids, functions)
            concepts.append({
                "id": concept_id, "parent_module_id": module["id"],
                "name": label, "summary": summary, "function_ids": sorted(fids),
                "file_paths": sorted({functions[fid]["file_path"] for fid in fids}),
            })
            concepts_per_module[module["id"]] += 1
        if index % 10 == 0 or index == len(modules):
            print(f"Concept clustering: {index}/{len(modules)} modules, {len(concepts)} concepts", flush=True)

    module_payload = {
        "resolution": partition_report["selected"]["resolution"],
        "min_module_size": 1, "model": "offline-file-graph",
        "modules": modules,
    }
    concept_payload = {
        "sub_resolution": args.concept_resolution,
        "min_concept_size": args.min_concept_size,
        "model": "offline-embedding-knn",
        "concepts": concepts,
    }
    args.module_output.write_text(json.dumps(module_payload, ensure_ascii=False), encoding="utf-8")
    args.concept_output.write_text(json.dumps(concept_payload, ensure_ascii=False), encoding="utf-8")

    report = {
        "inputs": {"files": len(files), "functions": len(functions), "embedding_chunks": len(chunks)},
        "file_graph": {
            "nodes": file_graph.number_of_nodes(), "edges": file_graph.number_of_edges(),
            "include_edges": len(includes), "call_resolution": call_stats,
        },
        "module_partition": partition_report,
        "modules": len(modules), "concepts": len(concepts),
        "module_file_sizes": distribution([len(module["file_paths"]) for module in modules]),
        "module_function_sizes": distribution([len(module["function_ids"]) for module in modules]),
        "concept_function_sizes": distribution([len(concept["function_ids"]) for concept in concepts]),
        "concepts_per_module": distribution(list(concepts_per_module.values())),
        "coverage": {
            "functions_in_modules": len(function_module),
            "functions_in_concepts": len({fid for concept in concepts for fid in concept["function_ids"]}),
        },
        "outputs": {"modules": str(args.module_output), "concepts": str(args.concept_output)},
    }
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
