"""End-to-end evaluation v3: function-level evidence extraction.

Stage 1: For each item, retrieve function-level chunks from visited files
using the embedding index, rank by query similarity, keep top-K functions.

Stage 2: Build answer prompt from function entries (file, name, line range, code)
and use deepseek-v4-pro to generate answer with citations.
"""
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import get_repo_root
from src.qa.investigation.base import BaseInvestigator, InvestigationState, SuspicionState
from src.qa.retrievers.fast_embedding import FastEmbeddingRetriever

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


def generate_answer_for_item(args: tuple) -> tuple[str, str, str, int, list]:
    """Generate answer for one item. Returns (qa_id, answer, prompt, prompt_tokens, function_entries)."""
    qa_id, question, function_entries = args
    try:
        inv = BaseInvestigator(max_steps=0, model="deepseek-v4-pro")

        # Build a custom evidence_log from function entries
        evidence_log = []
        files_content = {}
        for f in function_entries:
            fp = f["file_path"]
            name = f["name"]
            start = f.get("start_line", 0)
            end = f.get("end_line", 0)
            text = f.get("text", "")
            evidence_log.append({
                "file_path": f"{fp}:{name}",
                "key_facts": [f"lines {start}-{end}: {text[:500]}"],
                "new_hypothesis": "",
                "suspicious_symbols": [name],
            })
            # Also populate files_content with the function text for files_summary
            if fp not in files_content:
                files_content[fp] = ""
            files_content[fp] += f"\n\n--- {name} (lines {start}-{end}) ---\n{text}"

        inv.state = InvestigationState(
            question=question,
            entry_file=function_entries[0]["file_path"] if function_entries else "",
            suspicion=SuspicionState(current_question=question),
            evidence_log=evidence_log,
            files_content=files_content,
        )

        prompt, prompt_tokens = inv.build_answer_prompt()
        answer = inv.generate_answer()
        return qa_id, answer, prompt, prompt_tokens, function_entries
    except Exception as e:
        return qa_id, f"[ERROR: {e}]", "", 0, function_entries


def main(worklist_result_path: str, max_files: int = 60, top_k_functions: int = 50,
         answer_workers: int = 10, subset: int = None):
    with open(BENCH_PATH) as f:
        bench = json.load(f)["items"]
    worklist = json.load(open(worklist_result_path))
    per_item = worklist["per_item"]

    if subset:
        per_item = per_item[:subset]
        bench = bench[:subset]

    print(f"Loading function embedding index...")
    retriever = FastEmbeddingRetriever(repo_root=str(REPO_ROOT))

    print(f"Stage 1: retrieving top-{top_k_functions} functions for {len(per_item)} items...")

    functions_by_item: dict[str, list] = {item["qa_id"]: [] for item in per_item}
    for item in per_item:
        qa_id = item["qa_id"]
        question = item["question"]
        visited_files = item["visited_files"][:max_files]

        # Normalize visited file paths to match embedding index
        normalized_files = set()
        for fp in visited_files:
            np = normalize_path(fp)
            normalized_files.add(np)
            normalized_files.add(fp)  # keep both forms

        # Get function chunks from visited files, ranked by query similarity
        query_emb = retriever.encode_queries([question])
        results = retriever.retrieve(query_emb, top_k=top_k_functions * 3, file_filter=normalized_files)

        # Deduplicate by (file_path, name) and keep top_k
        seen = set()
        unique_functions = []
        for r in results:
            meta = r["metadata"]
            fp = meta.get("file_path", "")
            name = meta.get("name", "")
            key = (fp, name)
            if key in seen:
                continue
            seen.add(key)
            unique_functions.append({
                "file_path": fp,
                "name": name,
                "start_line": meta.get("start_line", 0),
                "end_line": meta.get("end_line", 0),
                "text": r["text"],
                "score": r["score"],
            })
            if len(unique_functions) >= top_k_functions:
                break

        functions_by_item[qa_id] = unique_functions
        print(f"[{qa_id}] retrieved {len(unique_functions)} unique functions from {len(visited_files)} files")

    print(f"Stage 1 done. Avg functions per item: {sum(len(v) for v in functions_by_item.values()) / len(functions_by_item):.1f}")

    # Stage 2: parallel answer generation
    print(f"Stage 2: generating answers for {len(per_item)} items with {answer_workers} workers...")
    answer_tasks = [
        (item["qa_id"], item["question"], functions_by_item[item["qa_id"]])
        for item in per_item
    ]
    answers = {}
    prompts = {}
    prompt_tokens = {}
    final_functions = {}
    with ThreadPoolExecutor(max_workers=answer_workers) as executor:
        futures = {executor.submit(generate_answer_for_item, task): task for task in answer_tasks}
        for future in as_completed(futures):
            qa_id, answer, prompt, ptok, funcs = future.result()
            answers[qa_id] = answer
            prompts[qa_id] = prompt
            prompt_tokens[qa_id] = ptok
            final_functions[qa_id] = funcs

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

        # Gold positions among retrieved functions (by file match)
        func_file_paths = [f["file_path"] for f in final_functions.get(qa_id, [])]
        gold_positions = {}
        for gf in gold_files:
            for rank, fp in enumerate(func_file_paths):
                if gf in fp or fp in gf or Path(gf).name in fp:
                    gold_positions[gf] = rank
                    break

        print(f"[{qa_id}] citation: {len(gold_files)-len(missing)}/{len(gold_files)} = {coverage*100:.0f}%")

        results.append({
            "qa_id": qa_id,
            "question": question,
            "visited_files": item["visited_files"][:max_files],
            "function_entries": final_functions.get(qa_id, []),
            "gold_files": gold_files,
            "gold_positions": gold_positions,
            "answer": answer,
            "prompt": prompts.get(qa_id, ""),
            "prompt_tokens": prompt_tokens.get(qa_id, 0),
            "cited_files": sorted(cited_files),
            "missing_gold_files": missing,
            "citation_coverage": coverage,
        })

    full_count = sum(1 for r in results if r["citation_coverage"] >= 1.0)
    avg_coverage = sum(r["citation_coverage"] for r in results) / len(results)
    avg_functions = sum(len(r["function_entries"]) for r in results) / len(results)
    avg_tokens = sum(r["prompt_tokens"] for r in results) / len(results)

    summary = {
        "total": len(results),
        "full_citation_rate": full_count / len(results),
        "avg_citation_coverage": avg_coverage,
        "avg_functions": avg_functions,
        "avg_prompt_tokens": avg_tokens,
        "worklist_result": worklist_result_path,
        "max_files": max_files,
        "top_k_functions": top_k_functions,
        "answer_workers": answer_workers,
    }

    out = {"summary": summary, "per_item": results}
    suffix = f"_subset{subset}" if subset else ""
    out_path = RESULT_DIR / f"worklist_e2e_v3_{Path(worklist_result_path).stem}{suffix}.json"
    with open(out_path, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print("\n" + json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Saved to {out_path}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--worklist-result", required=True)
    parser.add_argument("--max-files", type=int, default=60)
    parser.add_argument("--top-k-functions", type=int, default=50)
    parser.add_argument("--answer-workers", type=int, default=10)
    parser.add_argument("--subset", type=int, default=None, help="Only process first N items for quick diagnostic")
    args = parser.parse_args()
    main(args.worklist_result, args.max_files, args.top_k_functions, args.answer_workers, args.subset)
