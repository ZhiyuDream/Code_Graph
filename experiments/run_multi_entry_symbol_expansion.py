#!/usr/bin/env python3
"""
Multi-Entry Symbol Expansion 实验。

流程：
1. Question → Embedding Top-k 候选文件
2. 对前 M 个候选文件分别做短 symbol fast path（每个读 2-3 个相关文件）
3. 合并所有访问过的文件
4. 生成答案

验证：多入口扩展是否能补全 embedding 只召回部分证据的问题。

用法:
    python experiments/run_multi_entry_symbol_expansion.py \
        --benchmark datasets/benchmark_hard.json \
        --range 0,15 \
        --top-k 10 \
        --num-entries 3 \
        --evidence-per-entry 3 \
        -w 2 \
        -o results/multi_entry_0_15.json
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
from src.search.grep_search_v2 import grep_files


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


def file_priority(path: str) -> int:
    p = path.lower()
    if "/src/" in p or p.startswith("src/"):
        return 100
    if "/common/" in p or p.startswith("common/"):
        return 90
    if "/ggml/src/" in p or p.startswith("ggml/src/"):
        return 85
    if "/include/" in p or p.startswith("include/"):
        return 50
    if "/tests/" in p or p.startswith("tests/"):
        return 20
    if "/examples/" in p or p.startswith("examples/"):
        return 10
    if "/tools/" in p or p.startswith("tools/"):
        return 30
    return 40


def expand_from_entry(question: str, entry_file: str, inv: DynamicInvestigator, max_evidence: int) -> list[str]:
    """从一个入口文件做短 symbol fast path，返回访问的文件列表（包含入口）。"""
    visited = []
    try:
        content = inv.read_file(entry_file)
        visited.append(entry_file)
    except Exception:
        return visited

    suspicion, _ = inv._init_suspicion(question, entry_file, content)
    symbols = suspicion.suspicious_symbols
    if not symbols:
        return visited

    selected_symbol = symbols[0]
    files = grep_files(selected_symbol, inv.repo_path, limit=50)
    files = [inv._normalize_file_path(f) for f in files]
    files = sorted(set(files), key=lambda f: (-file_priority(f), f))

    for f in files[:max_evidence]:
        if f in visited:
            continue
        try:
            inv.read_file(f)
            visited.append(f)
        except Exception:
            pass

    return visited


def run_item(item: dict, retriever: EmbeddingRetriever, top_k: int, num_entries: int, evidence_per_entry: int) -> dict:
    # Layer 1: get top-k candidates
    results = retriever.retrieve(item["question"], top_k=top_k)
    candidates = []
    seen = set()
    for r in results:
        fp = r.metadata.get("file_path", "")
        if fp and fp not in seen:
            candidates.append(fp)
            seen.add(fp)

    # Layer 2: expand from top-M entries
    inv = DynamicInvestigator(max_steps=1)
    inv.state = InvestigationState(
        question=item["question"],
        entry_file=candidates[0] if candidates else "",
    )

    all_visited = []
    entry_details = []
    for entry_file in candidates[:num_entries]:
        visited = expand_from_entry(item["question"], entry_file, inv, evidence_per_entry)
        entry_details.append({
            "entry": entry_file,
            "visited": visited,
            "entry_hit": entry_file in item["gold_files"],
        })
        for f in visited:
            if f not in all_visited:
                all_visited.append(f)

    # Generate answer from all visited files
    contents = []
    for fp in all_visited:
        content = inv.state.files_content.get(fp, "")
        if content:
            contents.append(f"=== {fp} ===\n{content}")

    combined = "\n\n".join(contents)
    inv.state.evidence_log.append({
        "file_path": "combined_input",
        "key_facts": ["multi-entry symbol expansion combined content"],
        "new_hypothesis": "",
        "suspicious_symbols": [],
        "suspicious_files": all_visited,
    })
    answer = inv.generate_answer()
    coverage = compute_coverage(item["gold_files"], answer)

    entry_hit = any(fp in item["gold_files"] for fp in all_visited)

    return {
        "qa_id": item["qa_id"],
        "question": item["question"],
        "gold_files": item["gold_files"],
        "candidates": candidates,
        "entries": entry_details,
        "visited_files": all_visited,
        "entry_hit": entry_hit,
        "num_visited": len(all_visited),
        "coverage": coverage,
        "answer": answer,
    }


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--range", default="0,15")
    parser.add_argument("--top-k", type=int, default=10, help="embedding 返回的候选文件数")
    parser.add_argument("--num-entries", type=int, default=3, help="从多少个候选入口做扩展")
    parser.add_argument("--evidence-per-entry", type=int, default=3, help="每个入口扩展读取多少个文件")
    parser.add_argument("-w", "--workers", type=int, default=2)
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args()

    items = load_items(args.benchmark, args.range)
    print(f"加载 {len(items)} 题，top_k={args.top_k}, num_entries={args.num_entries}, evidence_per_entry={args.evidence_per_entry}, workers={args.workers}")

    retriever = EmbeddingRetriever()
    results = []
    completed = 0

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(run_item, item, retriever, args.top_k, args.num_entries, args.evidence_per_entry): item
            for item in items
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            completed += 1
            print(f"  [{completed}/{len(items)}] {result['qa_id']}: "
                  f"hit={result['entry_hit']} coverage={result['coverage']*100:.0f}% visited={result['num_visited']}")

    results.sort(key=lambda x: x["qa_id"])

    avg_cov = sum(r["coverage"] for r in results) / len(results)
    full = sum(1 for r in results if r["coverage"] >= 1.0)
    entry_hit_rate = sum(1 for r in results if r["entry_hit"]) / len(results) * 100

    print(f"\n{'='*60}")
    print("Multi-Entry Symbol Expansion 结果")
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
