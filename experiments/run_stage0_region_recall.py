#!/usr/bin/env python3
"""
Stage 0: Question → Region Recall。

测量 embedding 检索能把多少 gold evidence 文件放进 Top-K 候选池。

输出每个 K（1, 5, 10, 20）的：
- Question-level Recall@K：至少有一个 gold file 在 Top-K 中的题目比例
- Gold-file-level Recall@K：所有 gold files 中被 Top-K 覆盖的比例
- 每题的命中详情

用法:
    python experiments/run_stage0_region_recall.py \
        --benchmark datasets/benchmark_hard.json \
        --range 0,50 \
        --top-k 20 \
        -o results/stage0_region_recall_0_50.json
"""
import json
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

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


def evaluate_recall(candidates: list[str], gold_files: list[str], ks: list[int]) -> dict:
    """计算不同 K 下的 recall 指标。"""
    norm_candidates = [normalize_path(c) for c in candidates]
    norm_gold = [normalize_path(g) for g in gold_files]

    # Find which gold files are hit at which position
    hit_positions = {}
    for g in norm_gold:
        for i, c in enumerate(norm_candidates):
            if g == c or g in c or c in g:
                hit_positions[g] = i + 1  # 1-based rank
                break

    results = {}
    for k in ks:
        top_k = norm_candidates[:k]
        question_hit = any(g in top_k or any(g in c or c in g for c in top_k) for g in norm_gold)
        gold_hits = [g for g in norm_gold if g in top_k or any(g in c or c in g for c in top_k)]
        gold_recall = len(gold_hits) / len(norm_gold) if norm_gold else 1.0

        results[f"@K={k}"] = {
            "question_hit": question_hit,
            "gold_hits": sorted(gold_hits),
            "gold_recall": gold_recall,
            "num_gold_hits": len(gold_hits),
            "num_gold_total": len(norm_gold),
        }

    return {
        "hit_positions": hit_positions,
        "per_k": results,
    }


def run_item(item: dict, retriever: EmbeddingRetriever, top_k: int) -> dict:
    results = retriever.retrieve(item["question"], top_k=top_k)
    candidates = []
    seen = set()
    candidate_details = []
    for r in results:
        fp = r.metadata.get("file_path", "")
        name = r.metadata.get("name", "")
        if fp and fp not in seen:
            candidates.append(fp)
            seen.add(fp)
            candidate_details.append({
                "file_path": fp,
                "name": name,
                "score": r.score,
            })

    recall = evaluate_recall(candidates, item["gold_files"], ks=[1, 5, 10, 20])

    return {
        "qa_id": item["qa_id"],
        "question": item["question"],
        "gold_files": item["gold_files"],
        "candidates": candidates,
        "candidate_details": candidate_details,
        "hit_positions": recall["hit_positions"],
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
            print(f"  [{completed}/{len(items)}] {result['qa_id']}: "
                  f"q_hit@10={k10['question_hit']} gold_recall@10={k10['gold_recall']*100:.0f}% "
                  f"({k10['num_gold_hits']}/{k10['num_gold_total']})")

    results.sort(key=lambda x: x["qa_id"])

    # Summary
    ks = [1, 5, 10, 20]
    summary = {}
    for k in ks:
        key = f"@K={k}"
        q_hits = sum(1 for r in results if r["recall"][key]["question_hit"])
        total_gold = sum(r["recall"][key]["num_gold_total"] for r in results)
        total_gold_hits = sum(r["recall"][key]["num_gold_hits"] for r in results)
        summary[key] = {
            "question_recall": q_hits / len(results),
            "gold_file_recall": total_gold_hits / total_gold if total_gold else 0,
            "question_hits": q_hits,
            "total_questions": len(results),
            "total_gold_hits": total_gold_hits,
            "total_gold_files": total_gold,
        }

    print(f"\n{'='*60}")
    print("Stage 0: Question → Region Recall")
    print(f"{'='*60}")
    for k in ks:
        key = f"@K={k}"
        s = summary[key]
        print(f"K={k}: Question Recall={s['question_recall']*100:.1f}%  "
              f"Gold File Recall={s['gold_file_recall']*100:.1f}%  "
              f"({s['total_gold_hits']}/{s['total_gold_files']} gold files)")

    output = {
        "summary": summary,
        "per_item": results,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
