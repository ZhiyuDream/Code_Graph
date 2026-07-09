"""Pilot WorklistInvestigator on the 5 hardest questions."""
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

TARGET_QIDS = {
    "posthoc_public_001",
    "posthoc_public_002",
    "posthoc_public_012",
    "posthoc_public_014",
    "posthoc_public_034",
}


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


def main(max_scan: int = 20, max_steps: int = 8, lines_per_scan: int = 100):
    with open(BENCH_PATH) as f:
        bench = json.load(f)["items"]

    items = [item for item in bench if item["qa_id"] in TARGET_QIDS]
    retriever = FastEmbeddingRetriever()

    per_item = []
    for item in items:
        qa_id = item["qa_id"]
        question = item["question"]
        print(f"\n=== {qa_id} ===")

        query_emb = retriever.encode_queries([question])
        pool = build_function_pool(query_emb, retriever, top_k_files=20, top_m_functions=5)

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

        print(f"  coverage: {cov*100:.0f}% | visited: {len(result['visited_files'])}")
        for a in result["action_log"]:
            print(f"    step{a['step']}: {a['action']} {a['action_input']} -> {a['observation'][:100]}...")

        per_item.append({
            "qa_id": qa_id,
            "question": question,
            "visited_files": result["visited_files"],
            "gold_files": gold_files,
            "coverage": cov,
            "action_log": result["action_log"],
        })

    full = sum(1 for x in per_item if x["coverage"] >= 1.0)
    avg = sum(x["coverage"] for x in per_item) / len(per_item)
    print(f"\nSubset summary: full={full}/{len(per_item)}, avg={avg*100:.1f}%")

    out_path = RESULT_DIR / f"worklist_subset_pilot_S{max_scan}_T{max_steps}.json"
    with open(out_path, "w") as f:
        json.dump({"per_item": per_item}, f, ensure_ascii=False, indent=2)
    print(f"Saved to {out_path}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-scan", type=int, default=20)
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--lines", type=int, default=100)
    args = parser.parse_args()
    main(args.max_scan, args.max_steps, args.lines)
