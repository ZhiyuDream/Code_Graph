"""Evidence-driven file focus using v2 subset10 evidence_log.

For each item:
  1. Use existing evidence_log from v2 extract_evidence
  2. Score each file based on evidence strength
  3. Keep top-N files
  4. Retrieve functions only within retained files
  5. Compare function recall@K with baseline
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.qa.retrievers.fast_embedding import FastEmbeddingRetriever

V2_SUBSET_PATH = ROOT / "results" / "worklist_e2e_v2_symgraph_worklist_hard_L100_K20_E5_M15_H2_S5.json"
BENCH_PATH = ROOT / "datasets" / "benchmark_hard.json"


def normalize_path(path: str) -> str:
    return path.lstrip("./").lstrip("/")


def score_evidence(evidence: dict) -> float:
    """Heuristic evidence strength score."""
    score = 0.0
    facts = evidence.get("key_facts", [])
    score += len(facts) * 1.0
    score += sum(len(str(f)) for f in facts) / 100.0
    if evidence.get("new_hypothesis"):
        score += 2.0
    symbols = evidence.get("suspicious_symbols", [])
    score += len(symbols) * 0.5
    return score


def match_gold_function(gold: dict, results: list[dict]) -> bool:
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


def main():
    with open(BENCH_PATH) as f:
        bench = json.load(f)["items"][:10]
    with open(V2_SUBSET_PATH) as f:
        v2 = json.load(f)["per_item"]
    v2_map = {x["qa_id"]: x for x in v2}

    print("Loading embedding index...")
    retriever = FastEmbeddingRetriever(repo_root="/data/users/zzy/RUC/llama.cpp")

    print("Encoding questions...")
    questions = [item["question"] for item in bench]
    query_embs = retriever.encode_queries(questions)

    for top_n in [5, 10, 15, 20, 30]:
        baseline_recalls = {k: 0 for k in [5, 10, 20, 50]}
        focus_recalls = {k: 0 for k in [5, 10, 20, 50]}
        total_gold = 0
        retained_files_dist = []

        for i, item in enumerate(bench):
            qa_id = item["qa_id"]
            v2_item = v2_map[qa_id]
            evidence_log = v2_item.get("evidence_log", [])
            visited_files = set(normalize_path(f) for f in v2_item["visited_files"])

            # Score files by evidence
            file_scores = {}
            for ev in evidence_log:
                fp = normalize_path(ev.get("file_path", ""))
                if not fp:
                    continue
                file_scores[fp] = file_scores.get(fp, 0) + score_evidence(ev)

            # Also include visited files not in evidence_log with score 0
            for fp in visited_files:
                if fp not in file_scores:
                    file_scores[fp] = 0.0

            # Keep top-N by evidence score
            ranked_files = sorted(file_scores.items(), key=lambda x: -x[1])
            retained = set(fp for fp, _ in ranked_files[:top_n])
            retained_files_dist.append(len(retained))

            # Also do baseline: all visited files
            baseline_results = retriever.retrieve(query_embs[i:i+1], top_k=50, file_filter=visited_files)
            if retained:
                focus_results = retriever.retrieve(query_embs[i:i+1], top_k=50, file_filter=retained)
            else:
                focus_results = []

            for ev in item["gold_evidence"]:
                if ev["file"].endswith((".h", ".hpp")):
                    continue
                gold_file = normalize_path(ev["file"])
                if gold_file not in visited_files:
                    continue
                total_gold += 1

                for k in [5, 10, 20, 50]:
                    if match_gold_function(ev, baseline_results[:k]):
                        baseline_recalls[k] += 1
                    if retained and match_gold_function(ev, focus_results[:k]):
                        focus_recalls[k] += 1

        print(f"\n=== Top-{top_n} files by evidence score ===")
        print(f"Total gold in visited files: {total_gold}")
        print(f"Avg retained files per item: {sum(retained_files_dist)/len(retained_files_dist):.1f}")
        print(f'{"K":>5} {"Baseline":>12} {"Evidence Focus":>16} {"Improvement":>14}')
        print("-" * 55)
        for k in [5, 10, 20, 50]:
            base_pct = baseline_recalls[k] / total_gold * 100 if total_gold else 0
            focus_pct = focus_recalls[k] / total_gold * 100 if total_gold else 0
            improv = focus_pct - base_pct
            print(f"{k:>5} {base_pct:>11.1f}% {focus_pct:>15.1f}% {improv:>13.1f}%")


if __name__ == "__main__":
    main()
