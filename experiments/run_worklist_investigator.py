"""Run WorklistInvestigator on the hard benchmark.

Evaluates file-level coverage of the LLM-steered worklist agent.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import get_repo_root
from src.qa.retrievers.fast_embedding import FastEmbeddingRetriever
from src.qa.candidate_pool import build_function_pool
from src.qa.investigation.worklist import WorklistInvestigator

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


def main(
    max_scan: int = 20,
    max_steps: int = 8,
    lines_per_scan: int = 100,
    top_k_files: int = 20,
    top_m_functions: int = 5,
):
    with open(BENCH_PATH) as f:
        bench = json.load(f)["items"]

    retriever = FastEmbeddingRetriever()

    per_item = []
    for item in bench:
        qa_id = item["qa_id"]
        question = item["question"]
        print(f"\n=== {qa_id} ===")

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

        investigator = WorklistInvestigator(
            max_scan=max_scan,
            max_steps=max_steps,
            lines_per_scan=lines_per_scan,
            coverage_only=True,
        )
        result = investigator.run(question, unique_files)

        gold_files = [
            ev["file"]
            for ev in item["gold_evidence"]
            if not ev["file"].endswith((".h", ".hpp"))
        ]
        cov = file_coverage(gold_files, result["visited_files"])

        per_item.append({
            "qa_id": qa_id,
            "question": question,
            "visited_files": result["visited_files"],
            "gold_files": gold_files,
            "coverage": cov,
            "action_log": result["action_log"],
            "answer": result["answer"],
        })

        print(f"  coverage: {cov*100:.0f}% | visited: {len(result['visited_files'])} | actions: {len(result['action_log'])}")

    full = sum(1 for x in per_item if x["coverage"] >= 1.0)
    avg = sum(x["coverage"] for x in per_item) / len(per_item)
    avg_visited = sum(len(x["visited_files"]) for x in per_item) / len(per_item)
    summary = {
        "total": len(per_item),
        "full_coverage": full,
        "full_coverage_rate": full / len(per_item),
        "avg_coverage": avg,
        "avg_visited": avg_visited,
        "max_scan": max_scan,
        "max_steps": max_steps,
        "lines_per_scan": lines_per_scan,
        "top_k_files": top_k_files,
        "top_m_functions": top_m_functions,
    }

    out = {"summary": summary, "per_item": per_item}
    out_path = RESULT_DIR / (
        f"worklist_investigator_hard_S{max_scan}_T{max_steps}_L{lines_per_scan}.json"
    )
    with open(out_path, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print("\n" + json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Saved to {out_path}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-scan", type=int, default=20)
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--lines", type=int, default=100)
    parser.add_argument("--top-k-files", type=int, default=20)
    parser.add_argument("--top-m-functions", type=int, default=5)
    args = parser.parse_args()
    main(
        args.max_scan,
        args.max_steps,
        args.lines,
        args.top_k_files,
        args.top_m_functions,
    )
