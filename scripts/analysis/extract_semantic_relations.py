#!/usr/bin/env python3
"""Extract Temporal Relations and Implicit Logical Relations from Tree-sitter artifacts.

输出两类关系：
1. Temporal Relation：程序执行/生命周期/调用链中的先后依赖
   - CALLS：高置信度词法调用
   - INIT_BEFORE：同文件内定义顺序在前的初始化类函数

2. Implicit Logical Relation：不存在显式调用但共享语义职责的关联
   - SAME_MODULE：同一 Module 内函数
   - SAME_CONCEPT：同一 Concept 内函数
   - ALTERNATIVE_IMPLEMENTATION：命名模式相似的可替换实现（如不同 backend）

输出格式：
{
  "temporal_relations": [
    {"source": "fid", "target": "fid", "type": "CALLS", "confidence": 0.95, "evidence": "..."},
    ...
  ],
  "implicit_logical_relations": [
    {"source": "fid", "target": "fid", "type": "SAME_CONCEPT", "confidence": 0.9, "evidence": "concept_id"},
    ...
  ],
  "metadata": {...}
}
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path


THIRD_PARTY_PREFIXES = ("vendor/", "third_party/", "deps/")


def add_relation(relations: list, source: str, target: str, rtype: str, confidence: float, evidence: str = "") -> None:
    if source == target or confidence <= 0:
        return
    relations.append({
        "source": source,
        "target": target,
        "type": rtype,
        "confidence": round(confidence, 4),
        "evidence": evidence,
    })


def star_connect(relations: list, members: list[str], rtype: str, confidence: float, evidence: str) -> None:
    """星形连接：选一个 anchor，其他成员连 anchor，避免 O(N^2)。"""
    if len(members) < 2:
        return
    anchor = min(members)
    for fid in members:
        if fid != anchor:
            add_relation(relations, anchor, fid, rtype, confidence, evidence)


def load_extraction(path: Path) -> tuple[list[dict], dict[str, dict], list[dict]]:
    extraction = json.loads(path.read_text(encoding="utf-8"))
    files = [
        item for item in extraction["files"]
        if not item["file_path"].startswith(THIRD_PARTY_PREFIXES)
    ]
    functions = {}
    calls = []
    for item in files:
        for fn in item["functions"]:
            fn_with_file = {**fn, "file_path": item["file_path"]}
            functions[fn["id"]] = fn_with_file
        calls.extend(item.get("calls", []))
    return files, functions, calls


def resolve_calls(files: list[dict]) -> list[tuple[str, str, float]]:
    """Resolve lexical calls conservatively (same as experiment_hierarchical_abstraction)."""
    by_tail: dict[str, list[dict]] = defaultdict(list)
    by_file_tail: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for item in files:
        path = item["file_path"]
        for fn in item["functions"]:
            tail = fn["name"].split("::")[-1]
            by_tail[tail].append(fn)
            by_file_tail[(path, tail)].append(fn)

    def unique(candidates: list[dict]) -> str | None:
        definitions = [fn for fn in candidates if fn.get("is_definition", True)]
        ids = {fn["id"] for fn in (definitions or candidates)}
        return next(iter(ids)) if len(ids) == 1 else None

    edge_confidence: dict[tuple[str, str], float] = {}
    for item in files:
        path = item["file_path"]
        functions = item["functions"]
        for call in item["calls"]:
            caller_index = int(call["caller_index"])
            if not 0 <= caller_index < len(functions):
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
            elif callee is None:
                continue
            if callee and callee != caller:
                key = (caller, callee)
                edge_confidence[key] = max(edge_confidence.get(key, 0.0), confidence)

    return [(a, b, c) for (a, b), c in sorted(edge_confidence.items())]


def extract_temporal_calls(calls: list[tuple[str, str, float]]) -> list[dict]:
    """CALLS 关系。"""
    relations = []
    for caller, callee, conf in calls:
        add_relation(relations, caller, callee, "CALLS", conf, evidence="lexical_call")
    return relations


def extract_init_before(functions: dict[str, dict], max_links_per_init: int = 5) -> list[dict]:
    """同文件内定义顺序在前的 init/setup/create 类函数 → 后续函数（限制数量）。"""
    by_file: dict[str, list[dict]] = defaultdict(list)
    for fn in functions.values():
        by_file[fn["file_path"]].append(fn)

    init_keywords = ("init", "setup", "create", "new", "alloc", "construct")
    relations = []
    for file_path, fns in by_file.items():
        fns_sorted = sorted(fns, key=lambda f: f.get("start_line", 0))
        init_funcs = [
            f for f in fns_sorted
            if any(k in f["name"].lower() for k in init_keywords)
        ]
        for i, init_fn in enumerate(init_funcs):
            linked = 0
            for later_fn in fns_sorted[i + 1:]:
                if later_fn["id"] != init_fn["id"] and linked < max_links_per_init:
                    add_relation(
                        relations, init_fn["id"], later_fn["id"],
                        "INIT_BEFORE", 0.85, evidence=f"same_file:{file_path}"
                    )
                    linked += 1
    return relations


def extract_same_module(module_path: Path) -> list[dict]:
    """SAME_MODULE：同一 Module 内函数星形连接。"""
    data = json.loads(module_path.read_text(encoding="utf-8"))
    relations = []
    for module in data["modules"]:
        fids = module["function_ids"]
        star_connect(relations, fids, "SAME_MODULE", 0.80, evidence=module["id"])
    return relations


def extract_same_concept(concept_path: Path) -> list[dict]:
    """SAME_CONCEPT：同一 Concept 内函数星形连接。"""
    data = json.loads(concept_path.read_text(encoding="utf-8"))
    relations = []
    for concept in data["concepts"]:
        fids = concept["function_ids"]
        star_connect(relations, fids, "SAME_CONCEPT", 0.90, evidence=concept["id"])
    return relations


def extract_alternative_implementations(functions: dict[str, dict]) -> list[dict]:
    """ALTERNATIVE_IMPLEMENTATION：命名模式相似的可替换实现。"""
    groups: dict[str, list[str]] = defaultdict(list)

    for fid, fn in functions.items():
        name = fn["name"]
        lower = name.lower()

        # ggml_backend_{backend}_{op} 模式
        m = re.match(r"ggml_backend_(\w+?)_(init|free|graph_compute|supports_op|alloc_buffer|get_name|set_tensor|get_tensor)", lower)
        if m:
            groups[f"ggml_backend_{m.group(2)}"].append(fid)
            continue

        # sample_{strategy} 模式
        if lower.startswith("sample_"):
            groups["sampling_strategy"].append(fid)
            continue

        # chat_template_{op} 模式
        if "chat_template" in lower:
            groups["chat_template"].append(fid)
            continue

        # common_params_{op} 模式
        if lower.startswith("common_params_"):
            groups["common_params"].append(fid)
            continue

        # llama_{op} 模式（粗略）
        if lower.startswith("llama_"):
            groups["llama_core"].append(fid)
            continue

    relations = []
    for group_name, fids in groups.items():
        star_connect(relations, fids, "ALTERNATIVE_IMPLEMENTATION", 0.85, evidence=group_name)
    return relations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extraction", type=Path, default=Path("results/tree_sitter_cpp_extraction_20260715.json"))
    parser.add_argument("--module", type=Path, default=Path("data/module_abstraction_hierarchical_20260718.json"))
    parser.add_argument("--concept", type=Path, default=Path("data/concept_abstraction_hierarchical_20260718.json"))
    parser.add_argument("--output", type=Path, default=Path("data/semantic_relations.json"))
    args = parser.parse_args()

    files, functions, raw_calls = load_extraction(args.extraction)
    print(f"Loaded {len(files)} files, {len(functions)} functions, {len(raw_calls)} raw calls")

    # Temporal Relations
    resolved_calls = resolve_calls(files)
    temporal = []
    temporal.extend(extract_temporal_calls(resolved_calls))
    temporal.extend(extract_init_before(functions))
    print(f"Temporal relations: {len(temporal)}")

    # Implicit Logical Relations
    implicit = []
    implicit.extend(extract_same_module(args.module))
    implicit.extend(extract_same_concept(args.concept))
    implicit.extend(extract_alternative_implementations(functions))
    print(f"Implicit logical relations: {len(implicit)}")

    payload = {
        "temporal_relations": temporal,
        "implicit_logical_relations": implicit,
        "metadata": {
            "files": len(files),
            "functions": len(functions),
            "temporal_count": len(temporal),
            "implicit_count": len(implicit),
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
