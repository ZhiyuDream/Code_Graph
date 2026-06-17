#!/usr/bin/env python3
"""
Fast Retrieval Curves and Fixed Budget Comparison using numpy.
"""
import json
import sys
from pathlib import Path
from collections import defaultdict

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import numpy as np
from openai import OpenAI
from config import OPENAI_API_KEY, OPENAI_BASE_URL, EMBEDDING_MODEL


def normalize_path(path: str) -> str:
    return path.lstrip('./')


def load_index(index_path: Path):
    with open(index_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    func_indices = [i for i, c in enumerate(data["chunks"]) if c.get("type") == "function"]
    chunks = [data["chunks"][i] for i in func_indices]
    embeddings = np.array([data["embeddings"][i] for i in func_indices], dtype=np.float32)
    # Normalize embeddings for cosine similarity
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    embeddings_norm = embeddings / np.maximum(norms, 1e-10)
    return chunks, embeddings_norm


def get_query_embeddings(questions: list[str]):
    client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)
    embs = []
    for q in questions:
        resp = client.embeddings.create(model=EMBEDDING_MODEL, input=[q])
        embs.append(resp.data[0].embedding)
    embs = np.array(embs, dtype=np.float32)
    norms = np.linalg.norm(embs, axis=1, keepdims=True)
    return embs / np.maximum(norms, 1e-10)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, default=Path("datasets/benchmark_hard.json"))
    parser.add_argument("--oracle", type=Path, default=Path("results/gold_function_expansion_oracle_0_15.json"))
    parser.add_argument("--index", type=Path, default=Path("data/qa_embedding_index.json"))
    parser.add_argument("--range", default="0,15")
    parser.add_argument("-o", "--output", type=Path, default=Path("results/retrieval_curves_fast_0_15.json"))
    args = parser.parse_args()

    with open(args.benchmark, "r", encoding="utf-8") as f:
        bench = json.load(f)
    items = bench["items"]
    if "," in args.range:
        start, end = map(int, args.range.split(","))
    else:
        start, end = 0, len(items)
    items = items[start:end]

    # Load oracle if available, otherwise extract gold symbols from benchmark
    oracle_by_qa = {}
    try:
        with open(args.oracle, "r", encoding="utf-8") as f:
            oracle = json.load(f)
        oracle_by_qa = {r["qa_id"]: r for r in oracle["per_item"]}
    except Exception:
        pass

    # Extract gold symbols from gold_evidence if oracle missing
    for item in items:
        qa_id = item["qa_id"]
        if qa_id not in oracle_by_qa:
            symbols = sorted(set(ev.get("symbol", "") for ev in item.get("gold_evidence", []) if ev.get("symbol")))
            oracle_by_qa[qa_id] = {
                "gold_symbols": symbols,
                "best_symbol": symbols[0] if symbols else None,
            }

    print("Loading index...")
    chunks, emb_matrix = load_index(args.index)
    print(f"Index loaded: {len(chunks)} function chunks, dim={emb_matrix.shape[1]}")

    # Pre-compute file indices
    file_to_indices = defaultdict(list)
    for i, c in enumerate(chunks):
        fp = normalize_path(c.get("meta", {}).get("file_path", ""))
        if fp:
            file_to_indices[fp].append(i)

    # Pre-compute unique (file, name) mapping
    idx_to_key = []
    for i, c in enumerate(chunks):
        meta = c.get("meta", {})
        idx_to_key.append((normalize_path(meta.get("file_path", "")), meta.get("name", "")))

    # Pre-compute query embeddings
    print("Computing query embeddings...")
    query_embs = get_query_embeddings([item["question"] for item in items])

    # Compute all similarities at once: [num_questions, num_chunks]
    print("Computing similarities...")
    all_sims = query_embs @ emb_matrix.T

    print("\n=== Top-K File Curve ===")
    file_curve = []
    for k_files in [5, 10, 15, 20, 30, 50]:
        m_funcs = 5
        symbol_hits = 0
        file_hits = 0
        for q_idx, item in enumerate(items):
            sims = all_sims[q_idx]

            # Stage 1: top files
            file_best = {}
            for fp, indices in file_to_indices.items():
                file_best[fp] = sims[indices].max()
            top_files = sorted(file_best.items(), key=lambda x: -x[1])[:k_files]
            top_file_set = {fp for fp, _ in top_files}

            # Stage 2: collect functions from top files
            cand_names = set()
            for fp in top_file_set:
                indices = file_to_indices[fp]
                file_sims = sims[indices]
                top_idx = np.argsort(-file_sims)[:m_funcs]
                for ti in top_idx:
                    _, name = idx_to_key[indices[ti]]
                    if name:
                        cand_names.add(name)

            gold_files = {normalize_path(ev["file"]) for ev in item.get("gold_evidence", []) if not ev["file"].endswith((".h", ".hpp"))}
            gold_symbols = set(oracle_by_qa[item["qa_id"]].get("gold_symbols", []))

            fh = bool(gold_files & top_file_set) or any(
                any(g in tf or tf in g for tf in top_file_set) for g in gold_files
            )
            sh = bool(gold_symbols & cand_names)
            file_hits += fh
            symbol_hits += sh

        total = len(items)
        file_curve.append({
            "k_files": k_files,
            "m_funcs": m_funcs,
            "file_recall": file_hits / total,
            "symbol_recall": symbol_hits / total,
        })
        print(f"K_files={k_files}, M_funcs={m_funcs}: file={file_hits/total*100:.1f}%, symbol={symbol_hits/total*100:.1f}%")

    print("\n=== Fixed Budget Comparison ===")
    fixed_budget = []
    for budget in [10, 20, 30, 50]:
        # Global top-budget
        gh = 0
        sh = 0
        for q_idx, item in enumerate(items):
            sims = all_sims[q_idx]
            # Get unique (file, name) top-k
            order = np.argsort(-sims)
            seen = set()
            names = set()
            files = set()
            for idx in order:
                fp, name = idx_to_key[idx]
                key = (fp, name)
                if key not in seen and name:
                    seen.add(key)
                    names.add(name)
                    files.add(fp)
                if len(seen) >= budget:
                    break
            gold_files = {normalize_path(ev["file"]) for ev in item.get("gold_evidence", []) if not ev["file"].endswith((".h", ".hpp"))}
            gold_symbols = set(oracle_by_qa[item["qa_id"]].get("gold_symbols", []))
            fh = bool(gold_files & files) or any(
                any(g in cf or cf in g for cf in files) for g in gold_files
            )
            sh_g = bool(gold_symbols & names)
            gh += fh
            sh += sh_g

        total = len(items)
        entry = {
            "budget": budget,
            "global": {"file_recall": gh / total, "symbol_recall": sh / total},
            "two_stage": [],
        }
        print(f"\nBudget={budget} Global: file={gh/total*100:.1f}%, symbol={sh/total*100:.1f}%")

        # Two-stage configs with same budget
        configs = []
        for k in [5, 10, 15, 20, 25, 50]:
            if budget % k == 0:
                configs.append((k, budget // k))
        if budget == 30:
            configs.extend([(6, 5), (10, 3)])
        if budget == 50:
            configs.extend([(10, 5), (25, 2)])

        for k_files, m_funcs in configs:
            fh2 = 0
            sh2 = 0
            for q_idx, item in enumerate(items):
                sims = all_sims[q_idx]
                file_best = {fp: sims[indices].max() for fp, indices in file_to_indices.items()}
                top_files = sorted(file_best.items(), key=lambda x: -x[1])[:k_files]
                top_file_set = {fp for fp, _ in top_files}

                cand_names = set()
                for fp in top_file_set:
                    indices = file_to_indices[fp]
                    top_idx = np.argsort(-sims[indices])[:m_funcs]
                    for ti in top_idx:
                        _, name = idx_to_key[indices[ti]]
                        if name:
                            cand_names.add(name)

                gold_files = {normalize_path(ev["file"]) for ev in item.get("gold_evidence", []) if not ev["file"].endswith((".h", ".hpp"))}
                gold_symbols = set(oracle_by_qa[item["qa_id"]].get("gold_symbols", []))

                fh = bool(gold_files & top_file_set) or any(
                    any(g in tf or tf in g for tf in top_file_set) for g in gold_files
                )
                sh = bool(gold_symbols & cand_names)
                fh2 += fh
                sh2 += sh

            entry["two_stage"].append({
                "k_files": k_files,
                "m_funcs": m_funcs,
                "file_recall": fh2 / total,
                "symbol_recall": sh2 / total,
            })
            print(f"  Two-Stage k={k_files}, m={m_funcs}: file={fh2/total*100:.1f}%, symbol={sh2/total*100:.1f}%")

        fixed_budget.append(entry)

    # Unique gold symbols
    all_gold = []
    for item in items:
        all_gold.extend(oracle_by_qa[item["qa_id"]].get("gold_symbols", []))
    symbol_counts = defaultdict(int)
    for s in all_gold:
        symbol_counts[s] += 1

    print(f"\nUnique gold symbols: {len(symbol_counts)}")
    print("Top symbol frequencies:")
    for sym, cnt in sorted(symbol_counts.items(), key=lambda x: -x[1])[:10]:
        print(f"  {sym}: {cnt}")

    output = {
        "file_curve": file_curve,
        "fixed_budget": fixed_budget,
        "symbol_frequencies": dict(symbol_counts),
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
