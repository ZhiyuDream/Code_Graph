#!/usr/bin/env python3
"""Compatibility entrypoint for Tree-sitter-only ingestion.

The old implementation launched multiple clangd workers. Large repositories
now use one bounded Tree-sitter parser and one Neo4j writer instead.
"""
from __future__ import annotations

import argparse

from scripts.ingestion.ingest_code import main as run_ingestion


def main() -> int:
    parser = argparse.ArgumentParser(description="Tree-sitter-only code ingestion")
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Compatibility option; Neo4j writing uses one writer.",
    )
    parser.add_argument("--include-dirs", nargs="+", default=None)
    args = parser.parse_args()
    if args.workers != 1:
        print("Ignoring --workers: Tree-sitter parsing is bounded and Neo4j has one writer.")
    return run_ingestion()


if __name__ == "__main__":
    raise SystemExit(main())
