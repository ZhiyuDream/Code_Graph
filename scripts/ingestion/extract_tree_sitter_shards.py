#!/usr/bin/env python3
"""Run bounded Tree-sitter extraction and persist JSONL shards."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.ingestion.tree_sitter_shards import extract_to_shards


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("repo_root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=512)
    parser.add_argument("--max-files", type=int)
    args = parser.parse_args()
    report = extract_to_shards(
        args.repo_root.resolve(),
        args.output.resolve(),
        batch_size=args.batch_size,
        max_files=args.max_files,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
