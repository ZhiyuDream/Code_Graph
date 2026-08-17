"""Baseline: read first N lines of every unique file in the Top-20 function pool.

This tests the coverage ceiling of a simple, deterministic traversal of the
retrieved candidate files (no LLM-driven navigation). It establishes a strong
baseline for any ReAct / worklist agent.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import get_repo_root
from src.qa.retrievers.fast_embedding import FastEmbeddingRetriever
from src.qa.candidate_pool import build_function_pool
from src.search.code_reader import read_file_lines

BENCH_PATH = ROOT / "datasets" / "benchmark_hard.json"
RESULT_DIR = ROOT / "results"
REPO_ROOT = get_repo_root()


def normalize_path(path: str) -> str:
    if path.startswith(str(REPO_ROOT)):
        path = path[len(str(REPO_ROOT)):].lstrip("/")
    return path.lstrip("./").lstrip("/")


def file_coverage(gold_files, visited_files):
    if not gold_files:
        return 1.0
    g = {normalize_path(f) for f in gold_files}
    v = {normalize_path(f) for f in visited_files}
    return len(g & v) / len(g)


def main(lines_per_file: int = 100, top_k_files: int = 20, top_m_functions: int = 5):
    with open(BENCH_PATH) as f:
        bench = json.load(f)["items"]

    retriever = FastEmbeddingRetriever()

    per_item = []
    for item in bench:
        qa_id = item["qa_id"]
        question = item["question"]

        query_emb = retriever.encode_queries([question])
        pool = build_function_pool(
            query_emb,
            retriever,
            top_k_files=top_k_files,
            top_m_functions=top_m_functions,
        )

        unique_files = []
        seen = set()
        for c in pool:
            fp = normalize_path(c["file_path"])
            if fp and fp not in seen:
                seen.add(fp)
                unique_files.append(fp)

        visited_files = []
        for fp in unique_files:
            try:
                read_file_lines(fp, 1, lines_per_file)
                visited_files.append(fp)
            except Exception as e:
                print(f"[{qa_id}] failed to read {fp}: {e}")

        gold_files = [
            ev["file"]
            for ev in item["gold_evidence"]
            if not ev["file"].endswith((".h", ".hpp"))
        ]
        cov = file_coverage(gold_files, visited_files)

        per_item.append({
            "qa_id": qa_id,
            "question": question,
            "pool_size": len(pool),
            "unique_files": unique_files,
            "visited_files": visited_files,
            "gold_files": gold_files,
            "coverage": cov,
        })

    full = sum(1 for x in per_item if x["coverage"] >= 1.0)
    avg = sum(x["coverage"] for x in per_item) / len(per_item)
    summary = {
        "total": len(per_item),
        "full_coverage": full,
        "full_coverage_rate": full / len(per_item),
        "avg_coverage": avg,
        "lines_per_file": lines_per_file,
        "top_k_files": top_k_files,
        "top_m_functions": top_m_functions,
    }

    out = {
        "summary": summary,
        "per_item": per_item,
    }

    out_path = RESULT_DIR / f"readall_top{top_k_files}_baseline_hard_L{lines_per_file}.json"
    with open(out_path, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Saved to {out_path}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--lines", type=int, default=100)
    parser.add_argument("--top-k-files", type=int, default=20)
    parser.add_argument("--top-m-functions", type=int, default=5)
    args = parser.parse_args()
    main(args.lines, args.top_k_files, args.top_m_functions)
