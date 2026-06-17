#!/usr/bin/env python3
"""
Coarse Retrieval + Fine Reading 实验。

流程：
1. Question → Embedding Top-k 候选文件
2. 直接读取这 k 个文件的内容
3. 生成答案

验证：如果 embedding 已经把相关文件压缩到 top-k，直接读这些文件是否足够回答问题。

用法:
    python experiments/run_coarse_retrieval_fine_reading.py \
        --benchmark datasets/benchmark_hard.json \
        --range 0,15 \
        --top-k 5 \
        -w 2 \
        -o results/coarse_fine_0_15.json
"""
import json
import re
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.qa.retrievers.embedding import EmbeddingRetriever
from src.qa.investigation import DynamicInvestigator
from src.qa.investigation.base import InvestigationState


def extract_cited_files(answer: str) -> set[str]:
    cited = set()
    marker = "## 引用文件清单"
    idx = answer.rfind(marker)
    if idx >= 0:
        list_section = answer[idx + len(marker):]
        for line in list_section.split('\n'):
            line = line.strip()
            if line.startswith('- '):
                content = line[2:].strip()
                m = re.search(r'`?([\w/\-\.]+\.(?:cpp|c|h|hpp))(?::\d+)?`?', content)
                if m:
                    cited.add(m.group(1))
    return cited


def normalize_path(path: str) -> str:
    return path.lstrip('./')


def compute_coverage(gold_files: list[str], answer: str) -> float:
    if not gold_files:
        return 1.0
    cited = extract_cited_files(answer)
    cited_normalized = {normalize_path(f) for f in cited}
    gold_normalized = {normalize_path(f) for f in gold_files}
    hit = len(gold_normalized & cited_normalized)
    return hit / len(gold_normalized)


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


def run_item(item: dict, retriever: EmbeddingRetriever, top_k: int) -> dict:
    results = retriever.retrieve(item["question"], top_k=top_k)
    candidates = [r.metadata.get("file_path", "") for r in results if r.metadata.get("file_path")]

    inv = DynamicInvestigator(max_steps=1)
    inv.state = InvestigationState(
        question=item["question"],
        entry_file=candidates[0] if candidates else "",
    )

    visited = []
    contents = []
    for fp in candidates:
        if not fp or fp in visited:
            continue
        try:
            content = inv.read_file(fp)
            visited.append(fp)
            contents.append(f"=== {fp} ===\n{content}")
        except Exception:
            pass

    combined = "\n\n".join(contents)
    inv.state.evidence_log.append({
        "file_path": "combined_input",
        "key_facts": ["coarse retrieval fine reading combined content"],
        "new_hypothesis": "",
        "suspicious_symbols": [],
        "suspicious_files": visited,
    })
    for fp in visited:
        if fp not in inv.state.files_content:
            inv.state.files_content[fp] = combined
    answer = inv.generate_answer()
    coverage = compute_coverage(item["gold_files"], answer)

    entry_hit = any(fp in item["gold_files"] for fp in visited)

    return {
        "qa_id": item["qa_id"],
        "question": item["question"],
        "gold_files": item["gold_files"],
        "candidates": candidates,
        "visited_files": visited,
        "entry_hit": entry_hit,
        "num_visited": len(visited),
        "coverage": coverage,
        "answer": answer,
    }


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--range", default="0,15")
    parser.add_argument("--top-k", type=int, default=5, help="embedding 返回的候选文件数")
    parser.add_argument("-w", "--workers", type=int, default=2)
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
            print(f"  [{completed}/{len(items)}] {result['qa_id']}: "
                  f"hit={result['entry_hit']} coverage={result['coverage']*100:.0f}%")

    results.sort(key=lambda x: x["qa_id"])

    avg_cov = sum(r["coverage"] for r in results) / len(results)
    full = sum(1 for r in results if r["coverage"] >= 1.0)
    entry_hit_rate = sum(1 for r in results if r["entry_hit"]) / len(results) * 100

    print(f"\n{'='*60}")
    print("Coarse Retrieval + Fine Reading 结果")
    print(f"{'='*60}")
    print(f"总题数: {len(results)}")
    print(f"Entry Hit Rate: {entry_hit_rate:.1f}%")
    print(f"引用全: {full}/{len(results)} ({full/len(results)*100:.0f}%)")
    print(f"平均覆盖率: {avg_cov*100:.1f}%")

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
