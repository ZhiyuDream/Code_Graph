"""End-to-end evaluation: use worklist-retrieved files to generate answers.

Loads a worklist result file (e.g., symbol-graph worklist with 90% retrieval
coverage), reads the visited files, generates an answer for each question using
the existing answer-generation prompt, and evaluates whether the answer cites
all gold evidence files.

Evaluation is purely string-based: a gold file is considered "cited" if its
file path appears in the generated answer text.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, get_repo_root
from openai import OpenAI

from src.search.code_reader import read_full_file

BENCH_PATH = ROOT / "datasets" / "benchmark_hard.json"
RESULT_DIR = ROOT / "results"
REPO_ROOT = Path(get_repo_root())

client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url=DEEPSEEK_BASE_URL or "https://api.deepseek.com/v1",
)

ANSWER_PROMPT = (ROOT / "prompts" / "generate_answer.txt").read_text(encoding="utf-8")


def normalize_path(path: str) -> str:
    if path.startswith(str(REPO_ROOT)):
        path = path[len(str(REPO_ROOT)):].lstrip("/")
    return path.lstrip("./").lstrip("/")


def extract_cited_files(answer: str) -> set[str]:
    """Extract file paths that appear in the answer text."""
    pattern = r"[\w\-/.]+\.(?:cpp|c|h|hpp)"
    return set(normalize_path(p) for p in re.findall(pattern, answer))


def generate_answer(question: str, files_summary: str) -> str:
    """Generate answer from files summary using deepseek-v4-pro."""
    prompt = ANSWER_PROMPT.format(
        question=question,
        evidence_log="（见下方已访问文件内容摘要）",
        files_summary=files_summary,
    )
    try:
        resp = client.chat.completions.create(
            model="deepseek-v4-pro",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=8000,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        return f"[ERROR: {e}]"


def main(worklist_result_path: str, max_files: int = 60, chars_per_file: int = 1500):
    with open(BENCH_PATH) as f:
        bench = json.load(f)["items"]
    worklist = json.load(open(worklist_result_path))
    per_item = worklist["per_item"]

    results = []
    for i, item in enumerate(per_item):
        qa_id = item["qa_id"]
        question = item["question"]
        visited_files = item["visited_files"][:max_files]

        print(f"\n=== {qa_id} ({i+1}/50) ===")

        # Build files summary
        file_parts = []
        for fp in visited_files:
            try:
                content = read_full_file(fp)
                snippet = content[:chars_per_file]
                file_parts.append(f"=== {fp} ===\n{snippet}")
            except Exception as e:
                file_parts.append(f"=== {fp} ===\n[读取失败: {e}]")
        files_summary = "\n\n".join(file_parts)

        # Generate answer
        answer = generate_answer(question, files_summary)

        # Evaluate citation completeness
        gold_files = [
            normalize_path(ev["file"])
            for ev in bench[i]["gold_evidence"]
            if not ev["file"].endswith((".h", ".hpp"))
        ]
        cited_files = extract_cited_files(answer)
        missing = [f for f in gold_files if f not in cited_files]
        coverage = len([f for f in gold_files if f in cited_files]) / len(gold_files) if gold_files else 1.0

        print(f"  visited: {len(visited_files)} | gold: {len(gold_files)} | cited: {len(gold_files)-len(missing)}/{len(gold_files)} | coverage: {coverage*100:.0f}%")
        if missing:
            print(f"  missing: {missing}")

        results.append({
            "qa_id": qa_id,
            "question": question,
            "visited_files": visited_files,
            "gold_files": gold_files,
            "answer": answer,
            "cited_files": sorted(cited_files),
            "missing_gold_files": missing,
            "citation_coverage": coverage,
        })

    full_citation = sum(1 for r in results if r["citation_coverage"] >= 1.0)
    avg_coverage = sum(r["citation_coverage"] for r in results) / len(results)

    summary = {
        "total": len(results),
        "full_citation_rate": full_citation / len(results),
        "avg_citation_coverage": avg_coverage,
        "worklist_result": worklist_result_path,
        "max_files": max_files,
        "chars_per_file": chars_per_file,
    }

    out = {"summary": summary, "per_item": results}
    out_path = RESULT_DIR / f"worklist_e2e_{Path(worklist_result_path).stem}.json"
    with open(out_path, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print("\n" + json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Saved to {out_path}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--worklist-result", required=True, help="Path to worklist result JSON")
    parser.add_argument("--max-files", type=int, default=60)
    parser.add_argument("--chars-per-file", type=int, default=1500)
    args = parser.parse_args()
    main(args.worklist_result, args.max_files, args.chars_per_file)
