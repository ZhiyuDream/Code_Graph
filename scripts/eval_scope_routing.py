#!/usr/bin/env python3
"""
评估 Scope Routing 效果。

运行：
    python scripts/eval_scope_routing.py
    python scripts/eval_scope_routing.py --with-noise   # 同时对比噪音（需要调用 embedding API）
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# 把项目根目录加入路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.qa.scope_planner import SearchScopePlanner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

REPO_ROOT = "/data/users/zzy/RUC/llama.cpp"
BENCHMARK_PATH = Path(__file__).resolve().parent.parent / "datasets" / "posthoc_audit_benchmark_v2.json"


def load_benchmark(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data["items"]


def extract_gold_files(item: dict) -> set[str]:
    """从 benchmark item 提取正确文件列表。"""
    gold = item.get("gold_evidence", [])
    files = set()
    for ev in gold:
        f = ev.get("file", "")
        if f:
            files.add(f)
    return files


def eval_one(item: dict, planner: SearchScopePlanner, with_noise: bool) -> dict:
    """评估单个问题。"""
    question = item["question"]
    gold_files = extract_gold_files(item)

    # 1. Scope routing
    scope = planner.plan(question)
    scope_files = set(scope.files)

    # 2. File hit rate
    hit_files = gold_files & scope_files
    file_hit_rate = len(hit_files) / len(gold_files) if gold_files else 1.0

    result = {
        "question": question,
        "target_symbol": item.get("target_symbol", ""),
        "gold_files": sorted(gold_files),
        "scope_files": sorted(scope_files),
        "scope_sections": scope.documents,
        "file_hit_rate": file_hit_rate,
        "scope_size": len(scope_files),
    }

    # 3. 噪音对比（可选）
    if with_noise and scope_files:
        from src.qa.retrievers.embedding import EmbeddingRetriever

        emb = EmbeddingRetriever()

        # 全仓检索
        global_res = emb.retrieve(question, top_k=10)
        global_rel = sum(
            1 for r in global_res
            if r.metadata.get("file_path", "") in gold_files
        )
        global_noise = 1 - global_rel / len(global_res) if global_res else 0

        # 限制范围检索
        scoped_res = emb.retrieve(
            question, top_k=10, file_filter=scope_files,
        )
        scoped_rel = sum(
            1 for r in scoped_res
            if r.metadata.get("file_path", "") in gold_files
        )
        scoped_noise = 1 - scoped_rel / len(scoped_res) if scoped_res else 0

        result.update({
            "global_results": len(global_res),
            "global_relevant": global_rel,
            "global_noise": global_noise,
            "scoped_results": len(scoped_res),
            "scoped_relevant": scoped_rel,
            "scoped_noise": scoped_noise,
        })

    return result


def run_eval(items: list[dict], with_noise: bool = False, workers: int = 1) -> list[dict]:
    planner = SearchScopePlanner(REPO_ROOT)

    if workers <= 1:
        results = []
        for i, item in enumerate(items):
            if i % 10 == 0:
                logger.info("Evaluated %d/%d", i, len(items))
            results.append(eval_one(item, planner, with_noise))
    else:
        # 多线程（scope routing 是本地计算，可以并行）
        results = [None] * len(items)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(eval_one, item, planner, with_noise): idx
                for idx, item in enumerate(items)
            }
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    results[idx] = future.result()
                except Exception as e:
                    logger.error("Item %d failed: %s", idx, e)
                    results[idx] = {
                        "question": items[idx].get("question", ""),
                        "error": str(e),
                        "file_hit_rate": 0,
                        "scope_size": 0,
                    }

    return results


def print_summary(results: list[dict], with_noise: bool) -> None:
    total = len(results)
    errors = sum(1 for r in results if "error" in r)
    ok_results = [r for r in results if "error" not in r]
    n = len(ok_results)

    # File hit rate
    hit_rates = [r["file_hit_rate"] for r in ok_results]
    avg_hit_rate = sum(hit_rates) / n if n else 0

    perfect_hit = sum(1 for r in ok_results if r["file_hit_rate"] == 1.0)
    partial_hit = sum(1 for r in ok_results if 0 < r["file_hit_rate"] < 1.0)
    zero_hit = sum(1 for r in ok_results if r["file_hit_rate"] == 0)

    # Scope size
    avg_scope_size = sum(r["scope_size"] for r in ok_results) / n if n else 0
    empty_scope = sum(1 for r in ok_results if r["scope_size"] == 0)

    print("\n" + "=" * 70)
    print("Scope Routing Evaluation Summary")
    print("=" * 70)
    print(f"Total questions : {total}")
    print(f"Errors          : {errors}")
    print(f"OK              : {n}")
    print()
    print("--- File Hit Rate (gold files covered by scope) ---")
    print(f"  Average       : {avg_hit_rate:.1%}")
    print(f"  Perfect (1.0) : {perfect_hit}/{n} ({perfect_hit/n:.1%})")
    print(f"  Partial       : {partial_hit}/{n} ({partial_hit/n:.1%})")
    print(f"  Zero (0.0)    : {zero_hit}/{n} ({zero_hit/n:.1%})")
    print()
    print("--- Scope Size ---")
    print(f"  Average files : {avg_scope_size:.1f}")
    print(f"  Empty scope   : {empty_scope}/{n}")

    if with_noise:
        global_noises = [r["global_noise"] for r in ok_results if "global_noise" in r]
        scoped_noises = [r["scoped_noise"] for r in ok_results if "scoped_noise" in r]
        avg_global = sum(global_noises) / len(global_noises) if global_noises else 0
        avg_scoped = sum(scoped_noises) / len(scoped_noises) if scoped_noises else 0

        print()
        print("--- Noise Ratio (Embedding top-10) ---")
        print(f"  Global search : {avg_global:.1%}")
        print(f"  Scoped search : {avg_scoped:.1%}")
        print(f"  Improvement   : {avg_global - avg_scoped:+.1%}pp")

    print("=" * 70)

    # 打印 Zero hit 的详情（帮助 debug）
    if zero_hit > 0:
        print("\n--- Zero Hit Cases (need debugging) ---")
        for r in ok_results:
            if r["file_hit_rate"] == 0 and r["gold_files"]:
                print(f"  Q: {r['question'][:70]}")
                print(f"     Gold: {r['gold_files']}")
                print(f"     Scope ({r['scope_size']} files): {r['scope_files'][:5]}...")
                print(f"     Sections: {r['scope_sections']}")
                print()


def main():
    parser = argparse.ArgumentParser(description="Evaluate scope routing")
    parser.add_argument("--with-noise", action="store_true", help="Also compare noise ratio (needs embedding API)")
    parser.add_argument("--workers", type=int, default=1, help="Parallel workers for scope routing")
    parser.add_argument("--limit", type=int, default=0, help="Limit to first N questions (0=all)")
    args = parser.parse_args()

    items = load_benchmark(BENCHMARK_PATH)
    logger.info("Loaded %d benchmark items", len(items))

    if args.limit > 0:
        items = items[:args.limit]
        logger.info("Limited to first %d items", args.limit)

    results = run_eval(items, with_noise=args.with_noise, workers=args.workers)

    # 保存详细结果
    output_dir = Path("results")
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "scope_routing_eval.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    logger.info("Detailed results saved to %s", output_path)

    print_summary(results, with_noise=args.with_noise)


if __name__ == "__main__":
    main()
