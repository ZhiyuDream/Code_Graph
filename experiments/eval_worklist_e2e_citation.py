"""Re-evaluate worklist end-to-end results with LLM citation judge.

Uses evals.eval_v2.llm_citation_judge to compute coverage_ratio, cited_files,
missing_files, and missing reasons.
"""
import json
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from evals.eval_v2 import llm_citation_judge

BENCH_PATH = ROOT / "datasets" / "benchmark_hard.json"
RESULT_DIR = ROOT / "results"


def main(e2e_result_path: str, max_workers: int = 20):
    with open(BENCH_PATH) as f:
        bench = json.load(f)["items"]
    e2e = json.load(open(e2e_result_path))
    per_item = e2e["per_item"]

    results = []
    lock = __import__("threading").Lock()

    def process(i: int):
        item = per_item[i]
        bench_item = bench[i]
        qa_id = item["qa_id"]
        question = item["question"]
        answer = item.get("answer", "")
        reference = bench_item.get("reference_answer", "")

        # Gold files excluding .h/.hpp
        gold_files = [
            ev["file"].lstrip("./").lstrip("/")
            for ev in bench_item["gold_evidence"]
            if not ev["file"].endswith((".h", ".hpp"))
        ]

        judgment = llm_citation_judge(question, reference, answer, gold_files)

        with lock:
            print(f"[{qa_id}] LLM citation: {judgment['coverage_ratio']*100:.0f}% | cited={len(judgment['cited_files'])} | missing={len(judgment['missing_files'])}")

        return {
            "qa_id": qa_id,
            "question": question,
            "answer": answer,
            "gold_files": gold_files,
            "llm_judgment": judgment,
        }

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(process, i): i for i in range(len(per_item))}
        for future in as_completed(futures):
            i = futures[future]
            results.insert(i, future.result())

    full_citation = sum(1 for r in results if r["llm_judgment"]["coverage_ratio"] >= 1.0)
    avg_coverage = sum(r["llm_judgment"]["coverage_ratio"] for r in results) / len(results)

    summary = {
        "total": len(results),
        "llm_full_citation_rate": full_citation / len(results),
        "llm_avg_citation_coverage": avg_coverage,
        "e2e_result": e2e_result_path,
        "max_workers": max_workers,
    }

    out = {"summary": summary, "per_item": results}
    out_path = RESULT_DIR / f"llm_citation_eval_{Path(e2e_result_path).stem}.json"
    with open(out_path, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print("\n" + json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Saved to {out_path}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--e2e-result", required=True)
    parser.add_argument("--max-workers", type=int, default=20)
    args = parser.parse_args()
    main(args.e2e_result, args.max_workers)
