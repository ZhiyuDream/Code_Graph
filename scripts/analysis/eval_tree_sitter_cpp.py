#!/usr/bin/env python3
"""Offline Tree-sitter C/C++ extraction and coverage comparison.

The script never writes Neo4j or the existing embedding/concept caches.  It
compares Tree-sitter structure with the current clangd-derived QA embedding
index and with hard-benchmark evidence locations.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import get_repo_root
from src.ingestion.models import FileResult
from src.ingestion.tree_sitter_cpp_extractor import (
    collect_cpp_files,
    create_parser,
    parse_cpp_file,
)


def _serialize_file_result(result: FileResult, diagnostics: Any) -> dict[str, Any]:
    return {
        "file_path": result.file_path,
        "functions": [asdict(item) for item in result.functions],
        "classes": [asdict(item) for item in result.classes],
        "variables": [asdict(item) for item in result.variables],
        "calls": [asdict(item) for item in result.calls],
        "raw": result.raw,
        "diagnostics": asdict(diagnostics),
    }


def _load_embedding_functions(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    chunks = payload if isinstance(payload, list) else payload.get("chunks", payload.get("data", []))
    return [chunk["meta"] for chunk in chunks if chunk.get("type") == "function" and chunk.get("meta")]


def _load_benchmark_items(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload if isinstance(payload, list) else payload.get("items", payload.get("data", []))


def _contains_line(function: dict[str, Any], line: int) -> bool:
    return int(function.get("start_line", 0)) <= line <= int(function.get("end_line", 0))


def _compare(
    extracted: list[dict[str, Any]],
    embedding_functions: list[dict[str, Any]],
    benchmark_items: list[dict[str, Any]],
) -> dict[str, Any]:
    ts_functions = [fn for file in extracted for fn in file["functions"]]
    ts_calls = [call for file in extracted for call in file["calls"]]

    ts_by_file: dict[str, list[dict[str, Any]]] = {}
    calls_by_file: dict[str, list[dict[str, Any]]] = {}
    for fn in ts_functions:
        ts_by_file.setdefault(fn["file_path"], []).append(fn)
    for call in ts_calls:
        calls_by_file.setdefault(call["file_path"], []).append(call)

    emb_by_file: dict[str, list[dict[str, Any]]] = {}
    for fn in embedding_functions:
        emb_by_file.setdefault(fn["file_path"], []).append(fn)

    ts_name_file = {(fn["file_path"], fn["name"]) for fn in ts_functions}
    emb_name_file = {(fn["file_path"], fn["name"]) for fn in embedding_functions}
    ts_exact = {(fn["file_path"], fn["name"], int(fn["start_line"])) for fn in ts_functions}
    emb_exact = {(fn["file_path"], fn["name"], int(fn["start_line"])) for fn in embedding_functions}

    evidence_total = 0
    ts_file_hits = 0
    ts_structure_hits = 0
    emb_file_hits = 0
    emb_structure_hits = 0
    per_question = []

    for item in benchmark_items:
        q_total = q_ts = q_emb = 0
        for evidence in item.get("gold_evidence", []):
            evidence_total += 1
            q_total += 1
            file_path = evidence.get("file", "")
            line_start = int(evidence.get("line_start") or 0)
            line_end = int(evidence.get("line_end") or line_start)
            symbol = evidence.get("symbol", "")

            ts_funcs = ts_by_file.get(file_path, [])
            emb_funcs = emb_by_file.get(file_path, [])
            if file_path in ts_by_file or file_path in calls_by_file:
                ts_file_hits += 1
            if file_path in emb_by_file:
                emb_file_hits += 1

            def same_symbol(name: str) -> bool:
                return bool(symbol) and (name == symbol or name.split("::")[-1] == symbol)

            if symbol:
                containing_functions = [
                    fn for fn in ts_funcs if _contains_line(fn, line_start)
                ]
                ts_hit = any(same_symbol(fn["name"]) for fn in ts_funcs) or any(
                    same_symbol(call["callee_name"])
                    and any(
                        int(fn["start_line"]) <= int(call["line"]) <= int(fn["end_line"])
                        for fn in containing_functions
                    )
                    for call in calls_by_file.get(file_path, [])
                )
                emb_hit = any(
                    same_symbol(str(fn.get("name", ""))) or _contains_line(fn, line_start)
                    for fn in emb_funcs
                )
            else:
                # State/error/resource evidence intentionally has no symbol;
                # structural coverage means its source line belongs to a
                # function chunk.
                ts_hit = any(_contains_line(fn, line_start) for fn in ts_funcs)
                emb_hit = any(_contains_line(fn, line_start) for fn in emb_funcs)
            if ts_hit:
                ts_structure_hits += 1
                q_ts += 1
            if emb_hit:
                emb_structure_hits += 1
                q_emb += 1

        per_question.append({
            "qa_id": item.get("qa_id"),
            "evidence_total": q_total,
            "tree_sitter_structure_hits": q_ts,
            "embedding_structure_hits": q_emb,
        })

    return {
        "tree_sitter": {
            "files_with_functions": len(ts_by_file),
            "functions": len(ts_functions),
            "lexical_calls": len(ts_calls),
            "name_file_pairs": len(ts_name_file),
        },
        "embedding_baseline": {
            "files_with_functions": len(emb_by_file),
            "functions": len(embedding_functions),
            "name_file_pairs": len(emb_name_file),
        },
        "overlap": {
            "name_file_pairs": len(ts_name_file & emb_name_file),
            "tree_sitter_name_file_recall_of_embedding": (
                len(ts_name_file & emb_name_file) / len(emb_name_file) if emb_name_file else 0.0
            ),
            "embedding_name_file_recall_of_tree_sitter": (
                len(ts_name_file & emb_name_file) / len(ts_name_file) if ts_name_file else 0.0
            ),
            "exact_file_name_start": len(ts_exact & emb_exact),
        },
        "hard_benchmark_evidence": {
            "total": evidence_total,
            "tree_sitter_file_hits": ts_file_hits,
            "tree_sitter_structure_hits": ts_structure_hits,
            "embedding_file_hits": emb_file_hits,
            "embedding_structure_hits": emb_structure_hits,
        },
        "per_question": per_question,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=None)
    parser.add_argument("--benchmark", type=Path, default=ROOT / "datasets/benchmark_hard.json")
    parser.add_argument("--embedding-index", type=Path, default=ROOT / "data/qa_embedding_index.json")
    parser.add_argument("--output", type=Path, default=ROOT / "results/tree_sitter_cpp_extraction_20260715.json")
    parser.add_argument("--report", type=Path, default=ROOT / "results/tree_sitter_cpp_comparison_20260715.json")
    parser.add_argument("--limit", type=int, default=0, help="Parse only the first N files (smoke test)")
    args = parser.parse_args()

    repo_root = (args.repo or get_repo_root()).resolve()
    files = collect_cpp_files(repo_root)
    if args.limit > 0:
        files = files[:args.limit]
    print(f"Tree-sitter parsing {len(files)} files from {repo_root}", flush=True)

    ts_parser = create_parser()
    extracted = []
    started = perf_counter()
    for index, path in enumerate(files, 1):
        result, diagnostics = parse_cpp_file(path, repo_root, ts_parser)
        extracted.append(_serialize_file_result(result, diagnostics))
        if index % 100 == 0 or index == len(files):
            print(f"  [{index}/{len(files)}]", flush=True)
    elapsed = perf_counter() - started

    args.output.parent.mkdir(parents=True, exist_ok=True)
    artifact = {
        "metadata": {
            "parser": "tree-sitter-cpp",
            "repo_root": str(repo_root),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": elapsed,
            "files": len(extracted),
        },
        "files": extracted,
    }
    with args.output.open("w", encoding="utf-8") as handle:
        json.dump(artifact, handle, ensure_ascii=False)

    embedding_functions = _load_embedding_functions(args.embedding_index)
    benchmark_items = _load_benchmark_items(args.benchmark)
    comparison = _compare(extracted, embedding_functions, benchmark_items)
    comparison["run"] = artifact["metadata"]
    comparison["parse_quality"] = {
        "files_with_errors": sum(bool(file["diagnostics"]["has_error"]) for file in extracted),
        "error_nodes": sum(int(file["diagnostics"]["error_nodes"]) for file in extracted),
        "missing_nodes": sum(int(file["diagnostics"]["missing_nodes"]) for file in extracted),
        "largest_parse_ms": max((file["diagnostics"]["elapsed_ms"] for file in extracted), default=0.0),
    }
    with args.report.open("w", encoding="utf-8") as handle:
        json.dump(comparison, handle, ensure_ascii=False, indent=2)

    print(json.dumps({key: value for key, value in comparison.items() if key != "per_question"}, indent=2))
    print(f"Extraction: {args.output}")
    print(f"Comparison: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
