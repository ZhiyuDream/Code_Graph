#!/usr/bin/env python3
"""
Retrieval vs Selection Decomposition.

For each question:
1. Two-stage retrieval: 20 files x 5 functions = ~100 candidates
2. Check if gold symbol is in candidate pool (Pool Recall)
3. Ask LLM to select ONE candidate (Single Selection Recall)
4. Ask LLM to select MULTIPLE candidates (Multi Selection Recall)
5. Oracle: force-select gold symbol and expand (Oracle Selection Coverage)

This separates retrieval error from selection error.
"""
import json
import math
import re
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from config import get_repo_root, OPENAI_API_KEY, OPENAI_BASE_URL, EMBEDDING_MODEL
from src.qa.investigation.base import BaseInvestigator, LLMClient, load_prompt
from src.search.code_reader import read_full_file
from src.search.grep_search_v2 import grep_files


def cosine_sim_matrix(query_embs: np.ndarray, doc_embs: np.ndarray) -> np.ndarray:
    qn = np.linalg.norm(query_embs, axis=1, keepdims=True)
    dn = np.linalg.norm(doc_embs, axis=1, keepdims=True)
    qn[qn == 0] = 1e-10
    dn[dn == 0] = 1e-10
    return (query_embs @ doc_embs.T) / (qn @ dn.T)


class FastEmbeddingRetriever:
    def __init__(self, index_path: Path | None = None):
        if index_path is None:
            index_path = Path(__file__).resolve().parent.parent / "data" / "qa_embedding_index.json"
        self.index_path = index_path
        self.chunks = []
        self.doc_matrix = None
        self.file_to_chunks: dict[str, list[int]] = {}
        self._load()

    def _load(self):
        with open(self.index_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.chunks = data["chunks"]
        embs = np.asarray(data["embeddings"], dtype=np.float32)
        self.doc_matrix = embs
        for idx, ch in enumerate(self.chunks):
            fp = ch.get("meta", {}).get("file_path", "")
            self.file_to_chunks.setdefault(fp, []).append(idx)

    def encode_queries(self, queries: list[str]) -> np.ndarray:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)
        resp = client.embeddings.create(model=EMBEDDING_MODEL, input=queries)
        embs = [None] * len(queries)
        for e in resp.data:
            embs[e.index] = e.embedding
        return np.asarray(embs, dtype=np.float32)

    def retrieve(self, query_emb: np.ndarray, top_k: int = 5, file_filter: set[str] | None = None) -> list[dict]:
        if file_filter is not None:
            indices = []
            for fp in file_filter:
                indices.extend(self.file_to_chunks.get(fp, []))
            sub_matrix = self.doc_matrix[indices]
            sims = cosine_sim_matrix(query_emb.reshape(1, -1), sub_matrix)[0]
            pairs = [(sims[i], indices[i]) for i in range(len(indices))]
        else:
            sims = cosine_sim_matrix(query_emb.reshape(1, -1), self.doc_matrix)[0]
            pairs = [(sims[i], i) for i in range(len(self.chunks))]
        pairs.sort(key=lambda x: -x[0])
        results = []
        for sim, idx in pairs[:top_k]:
            ch = self.chunks[idx]
            results.append({
                "score": round(float(sim), 4),
                "metadata": ch.get("meta", {}),
            })
        return results


def normalize_path(path: str) -> str:
    repo_root = str(get_repo_root())
    if path.startswith(repo_root):
        path = path[len(repo_root):].lstrip('/')
    return path.lstrip('./')


def file_priority(path: str) -> int:
    p = path.lower()
    if "/src/" in p or p.startswith("src/"):
        return 100
    if "/common/" in p or p.startswith("common/"):
        return 90
    if "/ggml/src/" in p or p.startswith("ggml/src/"):
        return 85
    if "/include/" in p or p.startswith("include/"):
        return 50
    if "/tests/" in p or p.startswith("tests/"):
        return 20
    if "/examples/" in p or p.startswith("examples/"):
        return 10
    if "/tools/" in p or p.startswith("tools/"):
        return 30
    return 40


def select_definition_file(files: list[str]) -> str | None:
    if not files:
        return None
    return max(files, key=lambda f: (file_priority(f), -len(f)))


def expand_from_function(entry_function: str, repo_path: str,
                         callees_top_k: int, files_per_symbol: int) -> set[str]:
    from collections import Counter
    all_occurrences = grep_files(entry_function, repo_path, limit=100)
    visited = {normalize_path(f) for f in all_occurrences}
    definition_file = select_definition_file(list(visited))
    if definition_file:
        try:
            content = read_full_file(definition_file)
        except Exception:
            content = ""
        if content:
            investigator = BaseInvestigator.__new__(BaseInvestigator)
            symbols = investigator.extract_symbols(content)
            counter = Counter(symbols)
            callee_symbols = [sym for sym, _ in counter.most_common(callees_top_k)
                              if sym != entry_function]
            for sym in callee_symbols:
                files = grep_files(sym, repo_path, limit=50)
                selected = {normalize_path(f) for f in files[:files_per_symbol]}
                visited.update(selected)
    return visited


def load_items(bench_path: Path, range_str: str) -> list[dict]:
    with open(bench_path, "r", encoding="utf-8") as f:
        bench = json.load(f)
    items = bench["items"]
    if "," in range_str:
        start, end = map(int, range_str.split(","))
    else:
        start, end = 0, len(items)
    selected = []
    for idx in range(start, end):
        item = items[idx]
        gold_files = sorted(set(
            ev["file"] for ev in item.get("gold_evidence", [])
            if not ev["file"].endswith((".h", ".hpp"))
        ))
        selected.append({
            "qa_id": item.get("qa_id", f"q{idx}"),
            "question": item.get("question", ""),
            "gold_files": gold_files,
        })
    return selected


def two_stage_retrieve(query_emb: np.ndarray, retriever: FastEmbeddingRetriever,
                       top_k_files: int, top_m_functions: int) -> list[dict]:
    file_results = retriever.retrieve(query_emb, top_k=top_k_files * 3)
    file_scores = {}
    for r in file_results:
        fp = normalize_path(r["metadata"].get("file_path", ""))
        if fp:
            file_scores[fp] = max(file_scores.get(fp, 0), r["score"])
    top_files = sorted(file_scores.items(), key=lambda x: -x[1])[:top_k_files]

    all_functions = []
    seen = set()
    for fp, _ in top_files:
        func_results = retriever.retrieve(query_emb, top_k=top_m_functions, file_filter={fp})
        for r in func_results:
            name = r["metadata"].get("name", "")
            key = (fp, name)
            if name and key not in seen:
                seen.add(key)
                all_functions.append({
                    "file_path": fp,
                    "name": name,
                    "score": r["score"],
                })
    all_functions.sort(key=lambda x: -x["score"])
    return all_functions


def llm_select_single(question: str, candidates: list[dict], llm: LLMClient) -> dict:
    prompt_template = load_prompt("stage1_select_function")
    summaries = []
    for i, c in enumerate(candidates, 1):
        summaries.append(f"[{i}] {c['file_path']}::{c['name']}()")
    prompt = prompt_template.format(
        question=question,
        file_summaries="\n\n".join(summaries),
    )
    text = llm.call(prompt).strip()

    all_ids = []
    for m in re.finditer(r'\d+', text):
        idx = int(m.group(0))
        if 1 <= idx <= len(candidates):
            all_ids.append(idx)

    seen = set()
    unique_ids = []
    for idx in all_ids:
        if idx not in seen:
            unique_ids.append(idx)
            seen.add(idx)

    if unique_ids:
        return candidates[unique_ids[0] - 1]
    return candidates[0]


def llm_select_multiple(question: str, candidates: list[dict], llm: LLMClient) -> list[dict]:
    prompt_template = load_prompt("select_multiple_symbols")
    summaries = []
    for i, c in enumerate(candidates, 1):
        summaries.append(f"[{i}] {c['file_path']}::{c['name']}()")
    prompt = prompt_template.format(
        question=question,
        file_summaries="\n\n".join(summaries),
    )
    text = llm.call(prompt).strip()

    try:
        start = text.find('[')
        end = text.rfind(']')
        if start >= 0 and end > start:
            arr = json.loads(text[start:end+1])
            if isinstance(arr, list):
                valid = []
                seen = set()
                for idx in arr:
                    if isinstance(idx, int) and 1 <= idx <= len(candidates) and idx not in seen:
                        valid.append(idx)
                        seen.add(idx)
                return [candidates[i-1] for i in valid]
    except Exception:
        pass

    valid = []
    seen = set()
    for m in re.finditer(r'\d+', text):
        idx = int(m.group(0))
        if 1 <= idx <= len(candidates) and idx not in seen:
            valid.append(idx)
            seen.add(idx)
    return [candidates[i-1] for i in valid[:5]]


def run_item(item: dict, oracle_item: dict, query_emb: np.ndarray,
             retriever: FastEmbeddingRetriever,
             llm: LLMClient, repo_path: str,
             top_k_files: int, top_m_functions: int,
             top_n: int, files_per_symbol: int) -> dict:
    candidates = two_stage_retrieve(query_emb, retriever, top_k_files, top_m_functions)
    candidate_names = {c["name"] for c in candidates}

    gold_symbols = set(oracle_item.get("gold_symbols", []))
    best_symbol = oracle_item.get("best_symbol")

    pool_recall = bool(gold_symbols & candidate_names)
    best_in_pool = best_symbol in candidate_names if best_symbol else False

    # LLM single selection
    single_selected = llm_select_single(item["question"], candidates, llm)
    single_hit = single_selected["name"] in gold_symbols

    # LLM multi selection
    multi_selected = llm_select_multiple(item["question"], candidates, llm)
    multi_names = {c["name"] for c in multi_selected}
    multi_hit = bool(gold_symbols & multi_names)

    # Oracle: expand from best symbol if in pool
    oracle_coverage = 0.0
    if best_symbol and best_symbol in candidate_names:
        visited = expand_from_function(best_symbol, repo_path, top_n, files_per_symbol)
        gold_norm = {normalize_path(g) for g in item["gold_files"]}
        union_norm = {normalize_path(v) for v in visited}
        oracle_coverage = len(gold_norm & union_norm) / len(gold_norm) if gold_norm else 1.0

    return {
        "qa_id": item["qa_id"],
        "num_candidates": len(candidates),
        "gold_symbols": list(gold_symbols),
        "best_symbol": best_symbol,
        "pool_recall": pool_recall,
        "best_in_pool": best_in_pool,
        "single_selected": single_selected["name"],
        "single_hit": single_hit,
        "multi_selected": sorted(multi_names),
        "multi_hit": multi_hit,
        "oracle_coverage": oracle_coverage,
    }


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, default=Path("datasets/benchmark_hard.json"))
    parser.add_argument("--oracle", type=Path, default=Path("results/gold_function_expansion_oracle_0_15.json"))
    parser.add_argument("--range", default="0,15")
    parser.add_argument("--top-k-files", type=int, default=20)
    parser.add_argument("--top-m-functions", type=int, default=5)
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument("--files-per-symbol", type=int, default=3)
    parser.add_argument("-w", "--workers", type=int, default=2)
    parser.add_argument("-o", "--output", type=Path, default=Path("results/retrieval_selection_decomposition_0_15.json"))
    args = parser.parse_args()

    items = load_items(args.benchmark, args.range)
    with open(args.oracle, "r", encoding="utf-8") as f:
        oracle = json.load(f)
    oracle_by_qa = {r["qa_id"]: r for r in oracle["per_item"]}

    retriever = FastEmbeddingRetriever()
    query_embs = retriever.encode_queries([item["question"] for item in items])
    llm = LLMClient()
    repo_path = str(get_repo_root())

    results = []
    completed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(run_item, item, oracle_by_qa[item["qa_id"]],
                            query_embs[i], retriever,
                            llm, repo_path, args.top_k_files, args.top_m_functions,
                            args.top_n, args.files_per_symbol): item
            for i, item in enumerate(items)
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            completed += 1
            print(f"  [{completed}/{len(items)}] {result['qa_id']}: "
                  f"pool={result['pool_recall']} "
                  f"single={result['single_hit']} "
                  f"multi={result['multi_hit']} "
                  f"oracle_cov={result['oracle_coverage']*100:.0f}%")

    results.sort(key=lambda x: x["qa_id"])
    total = len(results)
    pool_rate = sum(1 for r in results if r["pool_recall"]) / total
    single_rate = sum(1 for r in results if r["single_hit"]) / total
    multi_rate = sum(1 for r in results if r["multi_hit"]) / total
    oracle_avg_cov = sum(r["oracle_coverage"] for r in results) / total

    print(f"\n{'='*60}")
    print(f"Retrieval vs Selection Decomposition ({args.top_k_files} files × {args.top_m_functions} funcs)")
    print(f"{'='*60}")
    print(f"总题数: {total}")
    print(f"Pool Recall (gold symbol in 100 candidates): {pool_rate*100:.1f}%")
    print(f"LLM Single Selection Recall: {single_rate*100:.1f}%")
    print(f"LLM Multi Selection Recall: {multi_rate*100:.1f}%")
    print(f"Oracle Selection Avg Coverage: {oracle_avg_cov*100:.1f}%")

    output = {
        "summary": {
            "total": total,
            "top_k_files": args.top_k_files,
            "top_m_functions": args.top_m_functions,
            "pool_recall": pool_rate,
            "single_selection_recall": single_rate,
            "multi_selection_recall": multi_rate,
            "oracle_avg_coverage": oracle_avg_cov,
        },
        "per_item": results,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
