"""Streaming Tree-sitter extraction into bounded JSONL shards.

The shard format is intentionally simple so it can be consumed by a later
single-writer Neo4j importer or by offline indexing jobs.
"""
from __future__ import annotations

import gc
import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Iterator

from .tree_sitter_cpp_extractor import iter_parsed_batches


def extract_to_shards(
    repo_root: Path,
    output_dir: Path,
    batch_size: int = 512,
    max_files: int | None = None,
) -> dict:
    """Parse with a bounded working set and write one JSONL record per file."""
    output_dir.mkdir(parents=True, exist_ok=True)
    shard_dir = output_dir / "files"
    shard_dir.mkdir(exist_ok=True)
    started = time.perf_counter()
    total = parsed = functions = classes = calls = 0
    shard_index = 0
    current_file = None

    def close_shard():
        nonlocal current_file
        if current_file is not None:
            current_file.close()
            current_file = None

    try:
        for batch in iter_parsed_batches(repo_root, batch_size=batch_size):
            if max_files is not None and total >= max_files:
                break
            for result, diagnostics in batch:
                if max_files is not None and total >= max_files:
                    break
                total += 1
                if current_file is None:
                    path = shard_dir / f"shard-{shard_index:06d}.jsonl"
                    current_file = path.open("w", encoding="utf-8")
                    shard_index += 1
                record = {
                    "file_result": asdict(result),
                    "diagnostics": asdict(diagnostics),
                }
                current_file.write(
                    json.dumps(record, ensure_ascii=False) + "\n"
                )
                parsed += 1
                functions += len(result.functions)
                classes += len(result.classes)
                calls += len(result.calls)
            close_shard()
            del batch
            gc.collect()

    finally:
        close_shard()

    report = {
        "repo_root": str(repo_root),
        "output_dir": str(output_dir),
        "batch_size": batch_size,
        "files_seen": total,
        "files_parsed": parsed,
        "files_failed": 0,
        "functions": functions,
        "classes": classes,
        "calls": calls,
        "shards": shard_index,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def iter_shard_records(shard_dir: Path) -> Iterator[dict]:
    """Read shard records lazily, without rebuilding a global FileResult list."""
    for path in sorted(shard_dir.glob("shard-*.jsonl")):
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    yield json.loads(line)
