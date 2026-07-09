"""Dynamic Focus: LLM rapid file relevance filtering before function retrieval.

For each item:
  1. Present the question + visited file paths to LLM (flash)
  2. LLM classifies each file as essential/relevant/background/irrelevant
  3. Keep only essential + relevant files
  4. Retrieve functions only within retained files
  5. Compare function recall@50 with baseline (all visited files)
"""
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from openai import OpenAI

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL
from src.qa.retrievers.fast_embedding import FastEmbeddingRetriever

BENCH_PATH = ROOT / "datasets" / "benchmark_hard.json"
WORKLIST_PATH = ROOT / "results" / "symgraph_worklist_hard_L100_K20_E5_M15_H2_S5.json"
RESULT_DIR = ROOT / "results"

PROMPT_TEMPLATE = """You are investigating a code repository to answer a post-hoc audit question.

Your task: quickly classify each candidate file by its likely importance for answering the question.

【Question】
{question}

【Candidate Files】
{file_list}

For each file, choose one of:
- essential: Core evidence is likely here; must read
- relevant: Related; may provide supporting evidence
- background: Peripheral; low value
- irrelevant: Clearly unrelated

Output ONLY a JSON object mapping file paths to labels:
{{
  "common/chat.cpp": "essential",
  "tests/test-chat.cpp": "background",
  ...
}}

Be decisive. Use your prior knowledge of typical C++ repository organization and the file paths."""


def normalize_path(path: str) -> str:
    return path.lstrip("./").lstrip("/")


def call_llm(prompt: str, model: str = "deepseek-v4-flash") -> str:
    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL or None)
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        max_tokens=2000,
    )
    return resp.choices[0].message.content or ""


def parse_classification(text: str) -> dict[str, str]:
    """Extract JSON mapping from LLM output."""
    # Try to find JSON block
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0]
    elif "```" in text:
        text = text.split("```")[1].split("```")[0]
    try:
        result = json.loads(text.strip())
        if isinstance(result, dict):
            return {k.strip(): v.strip() for k, v in result.items()}
    except Exception:
        pass
    return {}


def classify_files(qa_id: str, question: str, file_paths: list[str]) -> dict[str, str]:
    """Classify visited files by relevance. Returns {file_path: label}."""
    file_list = "\n".join(f"{i+1}. {fp}" for i, fp in enumerate(file_paths))
    prompt = PROMPT_TEMPLATE.format(question=question, file_list=file_list)
    try:
        text = call_llm(prompt)
        mapping = parse_classification(text)
        # Ensure all files have a label
        result = {}
        for fp in file_paths:
            result[fp] = mapping.get(fp, "background")
        return result
    except Exception as e:
        print(f"[{qa_id}] classification error: {e}")
        return {fp: "background" for fp in file_paths}


def match_gold_function(gold: dict, results: list[dict]) -> bool:
    """Check if gold function appears in retrieved results."""
    gold_file = normalize_path(gold["file"])
    gold_symbol = gold.get("symbol", "")
    gold_start = gold.get("line_start", 0)
    gold_end = gold.get("line_end", 0)

    for r in results:
        meta = r["metadata"]
        fp = meta.get("file_path", "")
        name = meta.get("name", "")
        start = meta.get("start_line", 0)
        end = meta.get("end_line", 0)
        if normalize_path(fp) != gold_file:
            continue
        if gold_symbol and (name == gold_symbol or gold_symbol in name or name in gold_symbol):
            return True
        if gold_start and gold_end and start and end:
            if not (end < gold_start or start > gold_end):
                return True
    return False


def main(max_workers: int = 10, subset: int = None):
    with open(BENCH_PATH) as f:
        bench = json.load(f)["items"]
    with open(WORKLIST_PATH) as f:
        worklist = json.load(f)["per_item"]

    if subset:
        bench = bench[:subset]
        worklist = worklist[:subset]

    print("Loading embedding index...")
    retriever = FastEmbeddingRetriever(repo_root="/data/users/zzy/RUC/llama.cpp")

    print("Classifying files with LLM...")
    classifications = {}
    tasks = [
        (item["qa_id"], item["question"], wl_item["visited_files"])
        for item, wl_item in zip(bench, worklist)
    ]
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(classify_files, qa_id, q, fps): qa_id for qa_id, q, fps in tasks}
        for future in as_completed(futures):
            qa_id = futures[future]
            classifications[qa_id] = future.result()
            print(f"[{qa_id}] classified")

    print("Encoding questions...")
    questions = [item["question"] for item in bench]
    query_embs = retriever.encode_queries(questions)

    # Evaluate function recall
    baseline_recalls = {k: 0 for k in [5, 10, 20, 50, 100]}
    focus_recalls = {k: 0 for k in [5, 10, 20, 50, 100]}
    total_gold = 0

    print("Evaluating function recall...")
    for i, item in enumerate(bench):
        qa_id = item["qa_id"]
        visited_files = [normalize_path(f) for f in worklist[i]["visited_files"]]
        cls = classifications[qa_id]
        retained_files = set()
        for fp in visited_files:
            label = cls.get(fp, "background")
            if label in ("essential", "relevant"):
                retained_files.add(fp)

        for ev in item["gold_evidence"]:
            if ev["file"].endswith((".h", ".hpp")):
                continue
            gold_file = normalize_path(ev["file"])
            if gold_file not in set(visited_files):
                continue
            total_gold += 1

            # Baseline: retrieve from all visited files
            baseline_results = retriever.retrieve(query_embs[i:i+1], top_k=100, file_filter=set(visited_files))
            baseline_hit = match_gold_function(ev, baseline_results)

            # Focus: retrieve only from retained files
            focus_hit = False
            if retained_files and gold_file in retained_files:
                focus_results = retriever.retrieve(query_embs[i:i+1], top_k=100, file_filter=retained_files)
                focus_hit = match_gold_function(ev, focus_results)

            for k in [5, 10, 20, 50, 100]:
                if baseline_hit and match_gold_function(ev, baseline_results[:k]):
                    baseline_recalls[k] += 1
                if focus_hit and match_gold_function(ev, focus_results[:k]):
                    focus_recalls[k] += 1

    print("\n" + "=" * 70)
    print("RESULTS: Dynamic Focus File Filtering")
    print("=" * 70)
    print(f"Total gold functions in visited files: {total_gold}")
    print()
    print(f'{"K":>5} {"Baseline Recall":>18} {"Focus Recall":>16} {"Improvement":>14}')
    print("-" * 60)
    for k in [5, 10, 20, 50, 100]:
        base_pct = baseline_recalls[k] / total_gold * 100 if total_gold else 0
        focus_pct = focus_recalls[k] / total_gold * 100 if total_gold else 0
        improv = focus_pct - base_pct
        print(f"{k:>5} {base_pct:>17.1f}% {focus_pct:>15.1f}% {improv:>13.1f}%")

    # Save classifications for inspection
    out = {
        "classifications": classifications,
        "summary": {
            "total_gold": total_gold,
            "baseline_recalls": baseline_recalls,
            "focus_recalls": focus_recalls,
        },
    }
    suffix = f"_subset{subset}" if subset else ""
    out_path = RESULT_DIR / f"dynamic_focus_file_filter{suffix}.json"
    with open(out_path, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-workers", type=int, default=10)
    parser.add_argument("--subset", type=int, default=None)
    args = parser.parse_args()
    main(args.max_workers, args.subset)
