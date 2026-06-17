#!/usr/bin/env python3
"""
Stage 0 (Function-Level): Question → Function Region Recall。

使用 function-chunk embedding 检索，测量问题能召回多少 gold files / functions。

评估指标：
- Question Recall@K: 至少有一个 gold function/file 被召回的题目比例
- Gold File Recall@K: gold files 被召回的比例（文件级）
- Gold Function Rank: gold function 在召回结果中的最佳排名

用法:
    python experiments/run_stage0_function_recall.py \
        --benchmark datasets/benchmark_hard.json \
        --range 0,50 \
        --top-k 20 \
        -w 4 \
        -o results/stage0_function_recall_0_50.json
"""
import json
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.qa.retrievers.embedding import EmbeddingRetriever


def normalize_path(path: str) -> str:
    return path.lstrip('./')


def load_items(bench_path: Path, range_str: str) -> list[dict]:
    with open(bench_path, "r", encoding="utf-8") as f:
        bench = json.load(f)
    items = bench["items"]

    if "," in range_str:
        start, end = map(int, range_str.split(","))
    else:
        start, end = 0, len(items)

    selected = []
    for idx in range(start, end):
        item = items[idx]
        gold_files = sorted(set(
            ev["file"] for ev in item.get("gold_evidence", [])
            if not ev["file"].endswith((".h", ".hpp"))
        ))
        selected.append({
            "qa_id": item.get("qa_id", f"q{idx}"),
            "question": item.get("question", ""),
            "gold_files": gold_files,
        })
    return selected


def evaluate_function_recall(candidates: list[dict], gold_files: list[str], ks: list[int]) -> dict:
    """
    candidates: list of {file_path, name, score, signature, content}
    评估文件级 recall 和 function rank。
    """
    norm_gold_files = [normalize_path(g) for g in gold_files]

    # For each gold file, find best rank among its functions
    gold_file_best_rank = {}
    for rank, c in enumerate(candidates, 1):
        c_file = normalize_path(c["file_path"])
        for g in norm_gold_files:
            if g == c_file or g in c_file or c_file in g:
                if g not in gold_file_best_rank or rank < gold_file_best_rank[g]:
                    gold_file_best_rank[g] = rank

    per_k = {}
    for k in ks:
        top_k = candidates[:k]
        top_k_files = set(normalize_path(c["file_path"]) for c in top_k)

        # Question hit: at least one gold file has a function in top-k
        question_hit = any(g in top_k_files for g in norm_gold_files)

        # Gold file hits
        gold_hits = [g for g in norm_gold_files if g in top_k_files]
        gold_file_recall = len(gold_hits) / len(norm_gold_files) if norm_gold_files else 1.0

        per_k[f"@K={k}"] = {
            "question_hit": question_hit,
            "gold_file_hits": sorted(gold_hits),
            "gold_file_recall": gold_file_recall,
            "num_gold_file_hits": len(gold_hits),
            "num_gold_files": len(norm_gold_files),
        }

    return {
        "gold_file_best_rank": gold_file_best_rank,
        "per_k": per_k,
    }


def run_item(item: dict, retriever: EmbeddingRetriever, top_k: int) -> dict:
    results = retriever.retrieve(item["question"], top_k=top_k)

    candidates = []
    seen = set()
    for r in results:
        meta = r.metadata
        key = (meta.get("file_path", ""), meta.get("name", ""))
        if key[0] and key not in seen:
            seen.add(key)
            candidates.append({
                "file_path": meta.get("file_path", ""),
                "name": meta.get("name", ""),
                "signature": meta.get("signature", ""),
                "score": r.score,
                "content": r.content,
            })

    recall = evaluate_function_recall(candidates, item["gold_files"], ks=[1, 5, 10, 20])

    return {
        "qa_id": item["qa_id"],
        "question": item["question"],
        "gold_files": item["gold_files"],
        "candidates": candidates,
        "gold_file_best_rank": recall["gold_file_best_rank"],
        "recall": recall["per_k"],
    }


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--range", default="0,50")
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("-w", "--workers", type=int, default=4)
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args()

    items = load_items(args.benchmark, args.range)
    print(f"加载 {len(items)} 题，top_k={args.top_k}, workers={args.workers}")

    retriever = EmbeddingRetriever()
    results = []
    completed = 0

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(run_item, item, retriever, args.top_k): item for item in items}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            completed += 1
            k10 = result["recall"]["@K=10"]
            ranks = result["gold_file_best_rank"]
            best_rank = min(ranks.values()) if ranks else None
            print(f"  [{completed}/{len(items)}] {result['qa_id']}: "
                  f"q_hit@10={k10['question_hit']} gold_recall@10={k10['gold_file_recall']*100:.0f}% "
                  f"best_rank={best_rank}")

    results.sort(key=lambda x: x["qa_id"])

    # Summary
    ks = [1, 5, 10, 20]
    summary = {}
    for k in ks:
        key = f"@K={k}"
        q_hits = sum(1 for r in results if r["recall"][key]["question_hit"])
        total_gold_files = sum(r["recall"][key]["num_gold_files"] for r in results)
        total_gold_hits = sum(r["recall"][key]["num_gold_file_hits"] for r in results)
        summary[key] = {
            "question_recall": q_hits / len(results),
            "gold_file_recall": total_gold_hits / total_gold_files if total_gold_files else 0,
            "question_hits": q_hits,
            "total_questions": len(results),
            "total_gold_hits": total_gold_hits,
            "total_gold_files": total_gold_files,
        }

    # Rank distribution
    all_ranks = []
    for r in results:
        all_ranks.extend(r["gold_file_best_rank"].values())
    rank_counter = Counter(all_ranks)

    print(f"\n{'='*60}")
    print("Stage 0 (Function-Level): Question → Function Region Recall")
    print(f"{'='*60}")
    for k in ks:
        key = f"@K={k}"
        s = summary[key]
        print(f"K={k:2d}: Question Recall={s['question_recall']*100:.1f}%  "
              f"Gold File Recall={s['gold_file_recall']*100:.1f}%  "
              f"({s['total_gold_hits']}/{s['total_gold_files']} gold files)")

    print(f"\nGold File Best Rank Distribution (within top-{args.top_k}):")
    for rank in sorted(rank_counter.keys())[:15]:
        print(f"  Rank {rank:2d}: {rank_counter[rank]} gold files")
    if all_ranks:
        print(f"  Mean best rank: {sum(all_ranks)/len(all_ranks):.1f}")
        print(f"  Median best rank: {sorted(all_ranks)[len(all_ranks)//2]}")

    output = {
        "summary": summary,
        "rank_distribution": dict(sorted(rank_counter.items())),
        "per_item": results,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
