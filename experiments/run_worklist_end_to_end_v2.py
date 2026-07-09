"""End-to-end evaluation v2: deepseek-only + two-stage parallel execution.

Stage 1: For every (item, file) pair, use deepseek-v4-flash to extract evidence.
Stage 2: For each item, use deepseek-v4-pro to generate answer from extracted evidence.

All LLM calls use the DeepSeek series (no GPT).
"""
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import get_repo_root
from src.qa.investigation.base import BaseInvestigator, InvestigationState, SuspicionState

BENCH_PATH = ROOT / "datasets" / "benchmark_hard.json"
RESULT_DIR = ROOT / "results"
REPO_ROOT = Path(get_repo_root())


def normalize_path(path: str) -> str:
    if path.startswith(str(REPO_ROOT)):
        path = path[len(str(REPO_ROOT)):].lstrip("/")
    return path.lstrip("./").lstrip("/")


def extract_cited_files(answer: str) -> set[str]:
    pattern = r"[\w\-/.]+\.(?:cpp|c|h|hpp)"
    return set(normalize_path(p) for p in re.findall(pattern, answer))


def extract_evidence_for_file(args: tuple) -> tuple[str, str, dict]:
    """Extract evidence for one file. Returns (qa_id, file_path, evidence)."""
    qa_id, question, file_path, extract_prompt_name = args
    try:
        inv = BaseInvestigator(max_steps=0, model="deepseek-v4-flash", extract_prompt_name=extract_prompt_name)
        inv.state = InvestigationState(
            question=question,
            entry_file=file_path,
            suspicion=SuspicionState(current_question=question),
        )
        content = inv.read_file(file_path)
        evidence = inv.extract_evidence(file_path, content)
        return qa_id, file_path, evidence
    except Exception as e:
        return qa_id, file_path, {
            "file_path": file_path,
            "key_facts": [],
            "new_hypothesis": "",
            "suspicious_symbols": [],
            "error": str(e),
        }


def generate_answer_for_item(args: tuple) -> tuple[str, str, str, int, list]:
    """Generate answer for one item. Returns (qa_id, answer, prompt, prompt_tokens, evidence_list)."""
    qa_id, question, evidence_list = args
    try:
        inv = BaseInvestigator(max_steps=0, model="deepseek-v4-pro")
        inv.state = InvestigationState(
            question=question,
            entry_file=evidence_list[0]["file_path"] if evidence_list else "",
            suspicion=SuspicionState(current_question=question),
            evidence_log=list(evidence_list),
        )
        # Populate files_content for generate_answer
        for ev in evidence_list:
            fp = ev["file_path"]
            if fp not in inv.state.files_content:
                try:
                    inv.state.files_content[fp] = inv.read_file(fp)
                except Exception:
                    pass

        prompt, prompt_tokens = inv.build_answer_prompt()
        answer = inv.generate_answer()
        return qa_id, answer, prompt, prompt_tokens, evidence_list
    except Exception as e:
        return qa_id, f"[ERROR: {e}]", "", 0, evidence_list


def main(worklist_result_path: str, max_files: int = 60, file_workers: int = 50, answer_workers: int = 10, subset: int = None, extract_prompt_name: str = "extract_evidence"):
    with open(BENCH_PATH) as f:
        bench = json.load(f)["items"]
    worklist = json.load(open(worklist_result_path))
    per_item = worklist["per_item"]

    if subset:
        per_item = per_item[:subset]
        bench = bench[:subset]

    # Build file extraction tasks
    file_tasks = []
    item_questions = {}
    for i, item in enumerate(per_item):
        qa_id = item["qa_id"]
        question = item["question"]
        item_questions[qa_id] = (i, question)
        for fp in item["visited_files"][:max_files]:
            file_tasks.append((qa_id, question, fp, extract_prompt_name))

    print(f"Stage 1: extracting evidence for {len(file_tasks)} (item, file) pairs with {file_workers} workers...")

    # Stage 1: parallel evidence extraction across all files
    evidence_by_item: dict[str, list] = {item["qa_id"]: [] for item in per_item}
    with ThreadPoolExecutor(max_workers=file_workers) as executor:
        futures = {executor.submit(extract_evidence_for_file, task): task for task in file_tasks}
        for future in as_completed(futures):
            qa_id, file_path, evidence = future.result()
            if (evidence.get("key_facts") or evidence.get("suspicious_symbols") or evidence.get("new_hypothesis")
                    or evidence.get("importance") in ("essential", "relevant")):
                evidence_by_item[qa_id].append(evidence)

    print(f"Stage 1 done. Avg evidence files per item: {sum(len(v) for v in evidence_by_item.values()) / len(evidence_by_item):.1f}")

    # Stage 2: parallel answer generation
    print(f"Stage 2: generating answers for {len(per_item)} items with {answer_workers} workers...")
    answer_tasks = [
        (item["qa_id"], item_questions[item["qa_id"]][1], evidence_by_item[item["qa_id"]])
        for item in per_item
    ]
    answers = {}
    prompts = {}
    prompt_tokens = {}
    final_evidence = {}
    with ThreadPoolExecutor(max_workers=answer_workers) as executor:
        futures = {executor.submit(generate_answer_for_item, task): task for task in answer_tasks}
        for future in as_completed(futures):
            qa_id, answer, prompt, ptok, ev_list = future.result()
            answers[qa_id] = answer
            prompts[qa_id] = prompt
            prompt_tokens[qa_id] = ptok
            final_evidence[qa_id] = ev_list

    print("Stage 2 done.")

    # Evaluate citation completeness
    results = []
    for i, item in enumerate(per_item):
        qa_id = item["qa_id"]
        question = item["question"]
        answer = answers.get(qa_id, "")

        gold_files = [
            normalize_path(ev["file"])
            for ev in bench[i]["gold_evidence"]
            if not ev["file"].endswith((".h", ".hpp"))
        ]
        cited_files = extract_cited_files(answer)
        missing = [f for f in gold_files if f not in cited_files]
        coverage = len([f for f in gold_files if f in cited_files]) / len(gold_files) if gold_files else 1.0

        print(f"[{qa_id}] citation: {len(gold_files)-len(missing)}/{len(gold_files)} = {coverage*100:.0f}%")

        # Compute gold file positions in evidence_files
        evidence_file_paths = [e["file_path"] for e in evidence_by_item[qa_id]]
        gold_positions = {}
        for gf in gold_files:
            for rank, fp in enumerate(evidence_file_paths):
                if gf in fp or fp in gf or Path(gf).name in fp:
                    gold_positions[gf] = rank
                    break

        results.append({
            "qa_id": qa_id,
            "question": question,
            "visited_files": item["visited_files"][:max_files],
            "evidence_files": evidence_file_paths,
            "evidence_log": final_evidence.get(qa_id, []),
            "gold_files": gold_files,
            "gold_positions": gold_positions,
            "answer": answer,
            "prompt": prompts.get(qa_id, ""),
            "prompt_tokens": prompt_tokens.get(qa_id, 0),
            "cited_files": sorted(cited_files),
            "missing_gold_files": missing,
            "citation_coverage": coverage,
        })

    full_citation = sum(1 for r in results if r["citation_coverage"] >= 1.0)
    avg_coverage = sum(r["citation_coverage"] for r in results) / len(results)
    avg_evidence = sum(len(r["evidence_files"]) for r in results) / len(results)

    summary = {
        "total": len(results),
        "full_citation_rate": full_citation / len(results),
        "avg_citation_coverage": avg_coverage,
        "avg_evidence_files": avg_evidence,
        "worklist_result": worklist_result_path,
        "max_files": max_files,
        "file_workers": file_workers,
        "answer_workers": answer_workers,
    }

    out = {"summary": summary, "per_item": results}
    suffix = f"_subset{subset}" if subset else ""
    prompt_suffix = f"_{extract_prompt_name}" if extract_prompt_name != "extract_evidence" else ""
    out_path = RESULT_DIR / f"worklist_e2e_v2_{Path(worklist_result_path).stem}{prompt_suffix}{suffix}.json"
    with open(out_path, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print("\n" + json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Saved to {out_path}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--worklist-result", required=True)
    parser.add_argument("--max-files", type=int, default=60)
    parser.add_argument("--file-workers", type=int, default=50)
    parser.add_argument("--answer-workers", type=int, default=10)
    parser.add_argument("--subset", type=int, default=None, help="Only process first N items for quick diagnostic")
    parser.add_argument("--extract-prompt", type=str, default="extract_evidence", help="Name of extract evidence prompt file")
    args = parser.parse_args()
    main(args.worklist_result, args.max_files, args.file_workers, args.answer_workers, args.subset, args.extract_prompt)
