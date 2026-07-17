#!/usr/bin/env python3
"""Run Concept-Symbol QA against isolated Tree-sitter assets."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import REPO_ROOT
from scripts.analysis.eval_region_compression import load_benchmark
from scripts.qa.run_concept_symbol_qa import run_qa
from src.core.concept_abstraction import ConceptAbstraction
from src.qa.retrievers.fast_embedding import FastEmbeddingRetriever


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--benchmark", type=Path, default=ROOT / "datasets/benchmark_hard.json")
    parser.add_argument("--index", type=Path, default=ROOT / "data/qa_embedding_index_tree_sitter.json")
    parser.add_argument("--concept-cache", type=Path, default=ROOT / "data/concept_abstraction_tree_sitter.json")
    parser.add_argument("--output", type=Path, default=ROOT / "results/qa_concept_symbol_tree_sitter_deepseek-v4-flash.json")
    args = parser.parse_args()

    if args.output.exists():
        print(f"Output already exists: {args.output}")
        return 0

    items = load_benchmark(args.benchmark)
    retriever = FastEmbeddingRetriever(index_path=args.index)
    retriever.chunk_index_by_id = {
        chunk["id"]: idx for idx, chunk in enumerate(retriever.chunks)
    }
    chunks_by_id = {chunk["id"]: chunk for chunk in retriever.chunks}
    print("Encoding questions...", flush=True)
    q_embs = retriever.encode_queries([item["question"] for item in items])

    concepts = ConceptAbstraction(cache_path=str(args.concept_cache))
    if not concepts.load():
        raise RuntimeError(f"Concept cache not found: {args.concept_cache}")

    repo_root = Path(REPO_ROOT) if REPO_ROOT else ROOT
    results = run_qa(
        "concept_symbol",
        items,
        q_embs,
        concepts,
        retriever,
        chunks_by_id,
        repo_root,
        model=args.model,
        workers=args.workers,
    )
    args.output.write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Saved to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
