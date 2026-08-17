#!/usr/bin/env python3
"""Build isolated QA assets from the offline Tree-sitter extraction."""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import EMBEDDING_MODEL, get_repo_root
from src.core.concept_abstraction import Concept, ConceptAbstraction
from src.core.embedding_client import get_encoder
from src.core.module_abstraction import ModuleAbstraction


THIRD_PARTY_PREFIXES = ("vendor/", "third_party/", "deps/")


def read_code(repo_root: Path, file_path: str, start: int, end: int, cache: dict) -> str:
    lines = cache.get(file_path)
    if lines is None:
        path = repo_root / file_path
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        cache[file_path] = lines
    return "".join(lines[max(0, start - 1):min(len(lines), end)])


def make_chunks(extracted: list[dict], repo_root: Path) -> tuple[list[dict], list[dict]]:
    chunks = []
    retained_files = []
    line_cache: dict[str, list[str]] = {}
    for file_result in extracted:
        file_path = file_result["file_path"]
        if file_path.startswith(THIRD_PARTY_PREFIXES):
            continue
        retained_files.append(file_result)
        for fn in file_result["functions"]:
            code = read_code(repo_root, file_path, int(fn["start_line"]), int(fn["end_line"]), line_cache)
            text = (
                f"Function: {fn['name']}\n"
                f"File: {file_path}\n"
                f"Signature: {fn.get('signature', '')[:1000]}\n\n"
                f"{code[:6000]}"
            )
            chunks.append({
                "id": fn["id"],
                "type": "function",
                "text": text,
                "meta": {
                    "name": fn["name"],
                    "file_path": file_path,
                    "start_line": fn["start_line"],
                    "end_line": fn["end_line"],
                    "signature": fn.get("signature", ""),
                    "is_definition": bool(fn.get("is_definition", True)),
                    "parser": "tree-sitter-cpp",
                },
            })
    return chunks, retained_files


def embed_chunks(chunks: list[dict], baseline_path: Path, batch_size: int) -> tuple[list[list[float]], dict]:
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    old_index = {chunk["id"]: idx for idx, chunk in enumerate(baseline["chunks"])}
    vectors: list[list[float] | None] = [None] * len(chunks)
    pending = []
    reused = 0

    for idx, chunk in enumerate(chunks):
        old_idx = old_index.get(chunk["id"])
        if old_idx is not None:
            vectors[idx] = baseline["embeddings"][old_idx]
            reused += 1
        else:
            pending.append(idx)

    print(f"Embedding reuse: {reused}, new: {len(pending)}", flush=True)
    encoder = get_encoder(model=EMBEDDING_MODEL)
    for offset in range(0, len(pending), batch_size):
        indices = pending[offset:offset + batch_size]
        texts = [chunks[idx]["text"] for idx in indices]
        last_error = None
        for attempt in range(3):
            try:
                matrix = encoder.encode(texts)
                break
            except Exception as exc:
                last_error = exc
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
        else:
            raise RuntimeError(last_error)
        for idx, vector in zip(indices, matrix):
            vectors[idx] = np.asarray(vector, dtype=np.float16).astype(np.float32).tolist()
        done = min(offset + batch_size, len(pending))
        print(f"  embedded new {done}/{len(pending)}", flush=True)

    assert all(vector is not None for vector in vectors)
    return vectors, {"reused": reused, "new": len(pending), "model": EMBEDDING_MODEL}


def resolve_lexical_calls(files: list[dict]) -> tuple[list[tuple[str, str]], dict]:
    by_tail: dict[str, list[dict]] = defaultdict(list)
    by_file_tail: dict[tuple[str, str], list[dict]] = defaultdict(list)
    allowed_ids = set()
    for file_result in files:
        for fn in file_result["functions"]:
            allowed_ids.add(fn["id"])
            tail = fn["name"].split("::")[-1]
            by_tail[tail].append(fn)
            by_file_tail[(file_result["file_path"], tail)].append(fn)

    def choose(candidates: list[dict]) -> str | None:
        if not candidates:
            return None
        definitions = [fn for fn in candidates if fn.get("is_definition", True)]
        pool = definitions or candidates
        unique = {fn["id"] for fn in pool}
        return next(iter(unique)) if len(unique) == 1 else None

    edges = set()
    total = ambiguous = unresolved = invalid_callers = 0
    for file_result in files:
        functions = file_result["functions"]
        for call in file_result["calls"]:
            total += 1
            caller_index = int(call["caller_index"])
            if caller_index < 0 or caller_index >= len(functions):
                invalid_callers += 1
                continue
            caller_id = functions[caller_index]["id"]
            if caller_id not in allowed_ids:
                continue
            tail = call["callee_name"].split("::")[-1]
            local = by_file_tail.get((file_result["file_path"], tail), [])
            callee_id = choose(local)
            candidates = local
            if callee_id is None and not local:
                candidates = by_tail.get(tail, [])
                callee_id = choose(candidates)
            if callee_id and callee_id != caller_id:
                edges.add((caller_id, callee_id))
            elif candidates:
                ambiguous += 1
            else:
                unresolved += 1

    stats = {
        "lexical_calls": total,
        "resolved_unique_edges": len(edges),
        "ambiguous": ambiguous,
        "unresolved_or_external": unresolved,
        "invalid_callers": invalid_callers,
    }
    return sorted(edges), stats


def build_abstractions(
    chunks: list[dict],
    calls: list[tuple[str, str]],
    module_path: Path,
    concept_path: Path,
) -> dict:
    functions = {
        chunk["id"]: {
            "f.name": chunk["meta"]["name"],
            "f.file_path": chunk["meta"]["file_path"],
            "f.start_line": chunk["meta"]["start_line"],
            "f.end_line": chunk["meta"]["end_line"],
        }
        for chunk in chunks
    }

    modules = ModuleAbstraction(cache_path=str(module_path))
    modules._build_modules(functions, calls)
    for module in modules.modules.values():
        module.name = module.id
        module.summary = "Tree-sitter deterministic module"
    modules.save()

    concepts = ConceptAbstraction(cache_path=str(concept_path))
    all_concepts = []
    for module in modules.modules.values():
        for cid, fids in concepts._cluster_module(module, functions, calls).items():
            concept_id = f"{module.id}:concept:{cid}"
            all_concepts.append(Concept(
                id=concept_id,
                parent_module_id=module.id,
                name=concept_id,
                summary="Tree-sitter deterministic concept",
                function_ids=fids,
                file_paths=sorted({functions[fid]["f.file_path"] for fid in fids}),
            ))
    concepts.concepts = {concept.id: concept for concept in all_concepts}
    concepts.save()
    return {"modules": len(modules.modules), "concepts": len(concepts.concepts)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extraction", type=Path, default=ROOT / "results/tree_sitter_cpp_extraction_20260715.json")
    parser.add_argument("--baseline-index", type=Path, default=ROOT / "data/qa_embedding_index.json")
    parser.add_argument("--index", type=Path, default=ROOT / "data/qa_embedding_index_tree_sitter.json")
    parser.add_argument("--module-cache", type=Path, default=ROOT / "data/module_abstraction_tree_sitter.json")
    parser.add_argument("--concept-cache", type=Path, default=ROOT / "data/concept_abstraction_tree_sitter.json")
    parser.add_argument("--report", type=Path, default=ROOT / "results/tree_sitter_qa_assets_report_20260715.json")
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if args.index.exists() and not args.force:
        print(f"Index already exists: {args.index}")
        return 0

    repo_root = get_repo_root().resolve()
    extraction = json.loads(args.extraction.read_text(encoding="utf-8"))
    chunks, retained_files = make_chunks(extraction["files"], repo_root)
    print(f"QA chunks: {len(chunks)} from {len(retained_files)} files", flush=True)

    embeddings, embedding_stats = embed_chunks(chunks, args.baseline_index, args.batch_size)
    args.index.write_text(
        json.dumps({"chunks": chunks, "embeddings": embeddings}, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"Saved index: {args.index}", flush=True)

    calls, call_stats = resolve_lexical_calls(retained_files)
    abstraction_stats = build_abstractions(
        chunks, calls, args.module_cache, args.concept_cache
    )
    report = {
        "parser": "tree-sitter-cpp",
        "chunks": len(chunks),
        "files": len(retained_files),
        "embedding": embedding_stats,
        "calls": call_stats,
        **abstraction_stats,
        "index": str(args.index),
        "module_cache": str(args.module_cache),
        "concept_cache": str(args.concept_cache),
    }
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
