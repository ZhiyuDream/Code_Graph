#!/usr/bin/env python3
"""
Benchmark: Symbol Fast Path vs Baseline

运行：
    python scripts/run_benchmark_symbol_fastpath.py --config symbol_fastpath --workers 20
    python scripts/run_benchmark_symbol_fastpath.py --config baseline --workers 20
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.qa.pipeline import QAPipeline
from src.qa.runner import QARunner
from src.qa.retrievers.embedding import EmbeddingRetriever
from src.qa.retrievers.grep import GrepRetriever

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

REPO_ROOT = "/data/users/zzy/RUC/llama.cpp"
BENCHMARK_PATH = Path(__file__).resolve().parent.parent / "datasets" / "posthoc_audit_benchmark_v2.json"


def load_questions(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data["items"]


def build_pipeline(config: str) -> QAPipeline:
    """构建 pipeline。"""
    retrievers = [
        EmbeddingRetriever(),
        GrepRetriever(repo_root=REPO_ROOT),
    ]

    if config == "symbol_fastpath":
        # 新配置：启用 Symbol Fast Path
        pipeline = QAPipeline(
            retrievers=retrievers,
            repo_root=REPO_ROOT,
            max_react_steps=5,
            enable_react=True,
            enable_symbol_fastpath=True,
        )
    elif config == "baseline":
        # 基线：禁用 Symbol Fast Path，走全局搜索
        pipeline = QAPipeline(
            retrievers=retrievers,
            repo_root=REPO_ROOT,
            max_react_steps=5,
            enable_react=True,
            enable_symbol_fastpath=False,
        )
    else:
        raise ValueError(f"Unknown config: {config}")

    return pipeline


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="symbol_fastpath", choices=["symbol_fastpath", "baseline"])
    parser.add_argument("--workers", type=int, default=20)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    questions = load_questions(BENCHMARK_PATH)
    if args.offset > 0:
        questions = questions[args.offset:]
    if args.limit > 0:
        questions = questions[:args.limit]
    logger.info("Loaded %d questions (offset=%d, limit=%d)", len(questions), args.offset, args.limit)

    pipeline = build_pipeline(args.config)
    runner = QARunner(pipeline, output_dir="results")

    if not args.output:
        ts = time.strftime("%Y%m%d_%H%M%S")
        args.output = f"results/benchmark_{args.config}_{ts}.json"

    logger.info("Running benchmark: config=%s, workers=%d", args.config, args.workers)
    results = runner.run_benchmark(
        questions,
        output_path=args.output,
        workers=args.workers,
        checkpoint_every=10,
    )

    logger.info("Done: %d results saved", len(results))


if __name__ == "__main__":
    main()
