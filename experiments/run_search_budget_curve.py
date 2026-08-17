"""Search Budget Curve.

For each budget K in {1,3,5,10,20,50,100,200}, read the first 100 lines of the
Top-K files ranked by embedding retrieval and compute file-level gold coverage.

This produces a curve showing how coverage grows purely with search budget,
without any LLM-driven selection.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import get_repo_root
from src.qa.retrievers.fast_embedding import FastEmbeddingRetriever
from src.search.code_reader import read_file_lines

BENCH_PATH = ROOT / "datasets" / "benchmark_hard.json"
RESULT_DIR = ROOT / "results"
REPO_ROOT = Path(get_repo_root())


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


def main(budgets=None, lines_per_file: int = 100):
    if budgets is None:
        budgets = [1, 3, 5, 10, 20, 50, 100, 200]

    with open(BENCH_PATH) as f:
        bench = json.load(f)["items"]

    retriever = FastEmbeddingRetriever()

    curve_points = []
    for K in budgets:
        per_item = []
        for item in bench:
            qa_id = item["qa_id"]
            question = item["question"]

            query_emb = retriever.encode_queries([question])
            results = retriever.retrieve(query_emb, top_k=K * 3)

            # Deduplicate by file path, keep top K unique files
            seen = set()
            unique_files = []
            for r in results:
                fp = normalize_path(r["metadata"].get("file_path", ""))
                if fp and fp not in seen:
                    seen.add(fp)
                    unique_files.append(fp)
                if len(unique_files) >= K:
                    break

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
                "coverage": cov,
                "visited_files": visited_files,
                "gold_files": gold_files,
            })

        full = sum(1 for x in per_item if x["coverage"] >= 1.0)
        avg = sum(x["coverage"] for x in per_item) / len(per_item)
        avg_files = sum(len(x["visited_files"]) for x in per_item) / len(per_item)

        curve_points.append({
            "budget": K,
            "full_coverage": full,
            "full_coverage_rate": full / len(per_item),
            "avg_coverage": avg,
            "avg_files_read": avg_files,
        })
        print(f"K={K:3d}: full={full}/{len(per_item)} = {full/len(per_item)*100:.0f}%, avg={avg*100:.1f}%")

    out = {"budgets": budgets, "curve": curve_points}
    out_path = RESULT_DIR / f"search_budget_curve_hard_L{lines_per_file}.json"
    with open(out_path, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--budgets", type=int, nargs="+", default=None)
    parser.add_argument("--lines", type=int, default=100)
    args = parser.parse_args()
    main(args.budgets, args.lines)
