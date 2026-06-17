#!/usr/bin/env python3
"""
Retrieval Curves and Fixed Budget Comparison.

Pre-computes query embeddings once, then simulates:
1. Single-stage global retrieval with various top_k
2. Two-stage retrieval with various (top_k_files, top_m_functions)
3. Fixed-budget comparison: same total candidate count

This avoids repeated API calls.
"""
import json
import sys
from pathlib import Path
from collections import defaultdict

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from openai import OpenAI
from config import OPENAI_API_KEY, OPENAI_BASE_URL, EMBEDDING_MODEL


def normalize_path(path: str) -> str:
    return path.lstrip('./')


def cosine_sim(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def load_index(index_path: Path):
    with open(index_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    chunks = []
    for c in data["chunks"]:
        if c.get("type") == "function":
            chunks.append(c)
    embeddings = [data["embeddings"][i] for i, c in enumerate(data["chunks"]) if c.get("type") == "function"]
    return chunks, embeddings


def get_query_embedding(question: str):
    client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)
    resp = client.embeddings.create(model=EMBEDDING_MODEL, input=[question])
    return resp.data[0].embedding


def global_retrieve(query_emb, chunks, embeddings, top_k):
    scores = []
    for i, emb in enumerate(embeddings):
        sim = cosine_sim(query_emb, emb)
        scores.append((sim, i))
    scores.sort(key=lambda x: -x[0])

    results = []
    seen = set()
    for sim, idx in scores:
        meta = chunks[idx].get("meta", {})
        key = (meta.get("file_path", ""), meta.get("name", ""))
        if key[0] and key not in seen:
            seen.add(key)
            results.append({
                "file_path": meta.get("file_path", ""),
                "name": meta.get("name", ""),
                "score": sim,
            })
        if len(results) >= top_k:
            break
    return results


def two_stage_retrieve(query_emb, chunks, embeddings, top_k_files, top_m_functions):
    # Stage 1: file-level aggregation
    file_scores = defaultdict(float)
    for i, emb in enumerate(embeddings):
        meta = chunks[i].get("meta", {})
        fp = normalize_path(meta.get("file_path", ""))
        if not fp:
            continue
        sim = cosine_sim(query_emb, emb)
        file_scores[fp] = max(file_scores[fp], sim)

    top_files = sorted(file_scores.items(), key=lambda x: -x[1])[:top_k_files]
    top_file_set = {fp for fp, _ in top_files}

    # Stage 2: function-level within each file
    results = []
    seen = set()
    for fp in top_file_set:
        file_funcs = []
        for i, emb in enumerate(embeddings):
            meta = chunks[i].get("meta", {})
            if normalize_path(meta.get("file_path", "")) == fp:
                sim = cosine_sim(query_emb, emb)
                name = meta.get("name", "")
                if name:
                    file_funcs.append((sim, name))
        file_funcs.sort(key=lambda x: -x[0])
        for sim, name in file_funcs[:top_m_functions]:
            key = (fp, name)
            if key not in seen:
                seen.add(key)
                results.append({"file_path": fp, "name": name, "score": sim})

    results.sort(key=lambda x: -x["score"])
    return results


def evaluate(results, gold_files, gold_symbols):
    cand_files = {normalize_path(r["file_path"]) for r in results}
    cand_names = {r["name"] for r in results}

    file_hit = any(
        g in cand_files or any(g in cf or cf in g for cf in cand_files)
        for g in gold_files
    )
    symbol_hit = bool(gold_symbols & cand_names)
    return file_hit, symbol_hit


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, default=Path("datasets/benchmark_hard.json"))
    parser.add_argument("--oracle", type=Path, default=Path("results/gold_function_expansion_oracle_0_15.json"))
    parser.add_argument("--index", type=Path, default=Path("data/qa_embedding_index.json"))
    parser.add_argument("--range", default="0,15")
    parser.add_argument("-o", "--output", type=Path, default=Path("results/retrieval_curves_0_15.json"))
    args = parser.parse_args()

    with open(args.benchmark, "r", encoding="utf-8") as f:
        bench = json.load(f)
    items = bench["items"]
    if "," in args.range:
        start, end = map(int, args.range.split(","))
    else:
        start, end = 0, len(items)
    items = items[start:end]

    with open(args.oracle, "r", encoding="utf-8") as f:
        oracle = json.load(f)
    oracle_by_qa = {r["qa_id"]: r for r in oracle["per_item"]}

    print("Loading index...")
    chunks, embeddings = load_index(args.index)
    print(f"Index loaded: {len(chunks)} function chunks")

    # Pre-compute query embeddings
    print("Computing query embeddings...")
    query_embs = {}
    for item in items:
        qa_id = item["qa_id"]
        print(f"  {qa_id}")
        query_embs[qa_id] = get_query_embedding(item["question"])

    # Top-K File Curve
    file_curve_ks = [5, 10, 15, 20, 30, 50]
    file_curve_results = []
    for k in file_curve_ks:
        file_hits = 0
        symbol_hits = 0
        for item in items:
            qa_id = item["qa_id"]
            gold_files = {normalize_path(g) for g in item.get("gold_files", [])}
            gold_symbols = set(oracle_by_qa[qa_id].get("gold_symbols", []))

            results = two_stage_retrieve(query_embs[qa_id], chunks, embeddings, k, 5)
            fh, sh = evaluate(results, gold_files, gold_symbols)
            file_hits += fh
            symbol_hits += sh
        total = len(items)
        file_curve_results.append({
            "k_files": k,
            "file_recall": file_hits / total,
            "symbol_recall": symbol_hits / total,
        })
        print(f"K_files={k}: file_recall={file_hits/total*100:.1f}%, symbol_recall={symbol_hits/total*100:.1f}%")

    # Fixed Budget Comparison
    budgets = [10, 20, 30, 50]
    fixed_budget_results = []
    for budget in budgets:
        # Global top-budget
        global_results_list = []
        for item in items:
            results = global_retrieve(query_embs[item["qa_id"]], chunks, embeddings, budget)
            global_results_list.append(results)

        # Two-stage: choose k and m such that k*m ≈ budget
        # e.g., budget=50 -> k=10, m=5 or k=5, m=10 etc.
        configs = []
        for k in [5, 10, 15, 20, 25, 50]:
            m = budget // k
            if m >= 1:
                configs.append((k, m))

        budget_entry = {"budget": budget, "global": None, "two_stage": []}

        # Evaluate global
        gh_total = 0
        sh_total = 0
        for item, results in zip(items, global_results_list):
            gold_files = {normalize_path(g) for g in item.get("gold_files", [])}
            gold_symbols = set(oracle_by_qa[item["qa_id"]].get("gold_symbols", []))
            fh, sh = evaluate(results, gold_files, gold_symbols)
            gh_total += fh
            sh_total += sh
        total = len(items)
        budget_entry["global"] = {
            "file_recall": gh_total / total,
            "symbol_recall": sh_total / total,
        }
        print(f"\nBudget={budget} Global: file_recall={gh_total/total*100:.1f}%, symbol_recall={sh_total/total*100:.1f}%")

        # Evaluate two-stage configs
        for k, m in configs:
            fh_total = 0
            sh_total = 0
            for item in items:
                results = two_stage_retrieve(query_embs[item["qa_id"]], chunks, embeddings, k, m)
                gold_files = {normalize_path(g) for g in item.get("gold_files", [])}
                gold_symbols = set(oracle_by_qa[item["qa_id"]].get("gold_symbols", []))
                fh, sh = evaluate(results, gold_files, gold_symbols)
                fh_total += fh
                sh_total += sh
            budget_entry["two_stage"].append({
                "k_files": k,
                "m_funcs": m,
                "file_recall": fh_total / total,
                "symbol_recall": sh_total / total,
            })
            print(f"  Two-Stage k={k}, m={m}: file_recall={fh_total/total*100:.1f}%, symbol_recall={sh_total/total*100:.1f}%")

        fixed_budget_results.append(budget_entry)

    # Unique gold symbols
    all_gold_symbols = []
    for item in items:
        qa_id = item["qa_id"]
        all_gold_symbols.extend(oracle_by_qa[qa_id].get("gold_symbols", []))
    symbol_counts = defaultdict(int)
    for s in all_gold_symbols:
        symbol_counts[s] += 1

    print(f"\nUnique gold symbols: {len(symbol_counts)}")
    print("Symbol frequencies:")
    for sym, cnt in sorted(symbol_counts.items(), key=lambda x: -x[1]):
        print(f"  {sym}: {cnt}")

    output = {
        "file_curve": file_curve_results,
        "fixed_budget": fixed_budget_results,
        "symbol_frequencies": dict(symbol_counts),
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
