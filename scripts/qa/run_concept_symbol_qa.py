#!/usr/bin/env python3
"""
端到端 QA：Concept-Symbol Guided Investigation vs Baseline Embedding。

流程（Deterministic Investigation Policy）：
Question
  ↓
Extract symbols
  ↓
Retrieve top concepts by embedding
  ↓
Collect functions in concepts
  ↓
Rerank by embedding + symbol match
  ↓
Read function code
  ↓
Generate answer with LLM

对比：
- baseline: top-50 embedding retrieval + answer generation
- concept_symbol: top concepts + symbol rerank + answer generation

输出格式兼容 evals/eval_v2.py。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from config import OPENAI_API_KEY, OPENAI_BASE_URL, DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, REPO_ROOT
from openai import OpenAI

from src.core.concept_abstraction import ConceptAbstraction
from src.core.module_abstraction import ModuleAbstraction
from src.qa.prompts import PromptBuilder
from src.qa.retrievers.fast_embedding import FastEmbeddingRetriever
from scripts.analysis.eval_region_compression import (
    detect_modules,
    fetch_call_graph,
    fetch_functions_and_calls,
    load_benchmark,
    load_embedding_index,
    build_helpers,
)


def extract_symbols(question: str) -> list:
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", question)
    stopwords = {
        "How", "What", "Where", "When", "Why", "Is", "Are", "Does", "Do", "Did",
        "the", "a", "an", "and", "or", "in", "on", "at", "to", "for", "of",
        "with", "by", "from", "as", "it", "its", "this", "that", "these", "those",
        "AI", "帮我", "现有", "是否", "什么", "怎么", "如何", "为什么",
        "担心", "看", "确认", "顺一下", "理解",
    }
    symbols = []
    for t in tokens:
        if len(t) < 3 or t in stopwords:
            continue
        symbols.append(t.lower())
        parts = re.findall(r"[A-Z][a-z]+|[a-z]+|[A-Z]+", t)
        if len(parts) > 1:
            for p in parts:
                if len(p) >= 3 and p not in stopwords:
                    symbols.append(p.lower())
    return list(set(symbols))


def symbol_score(fid: str, symbols: list, chunks_by_id: dict) -> float:
    if not symbols:
        return 0.0
    meta = chunks_by_id.get(fid, {}).get("meta", {})
    name = meta.get("name", "").lower()
    file_path = meta.get("file_path", "").lower()
    signature = meta.get("signature", "").lower()
    text = chunks_by_id.get(fid, {}).get("text", "").lower()
    text_all = f"{name} {file_path} {signature} {text}"
    matched = 0
    for sym in symbols:
        if sym in name:
            matched += 2.0
        elif sym in text_all:
            matched += 1.0
    return matched / (len(symbols) * 2.0)


def read_code(file_path: str, start_line: int, end_line: int, repo_root: Path, max_chars: int = 3000) -> str:
    abs_path = repo_root / file_path
    if not abs_path.exists():
        return ""
    try:
        with open(abs_path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        s = max(0, start_line - 1)
        e = min(len(lines), end_line)
        code = "".join(lines[s:e])
        if len(code) > max_chars:
            code = code[:max_chars] + "\n... (truncated)"
        return code
    except Exception:
        return ""


def build_context(fids: list, chunks_by_id: dict, repo_root: Path, max_funcs: int = 20) -> str:
    parts = []
    for fid in fids[:max_funcs]:
        meta = chunks_by_id.get(fid, {}).get("meta", {})
        name = meta.get("name", "")
        file_path = meta.get("file_path", "")
        start_line = meta.get("start_line", 0)
        end_line = meta.get("end_line", 0)
        signature = meta.get("signature", "")
        code = read_code(file_path, start_line, end_line, repo_root)
        parts.append(
            f"--- Function: {name} ---\n"
            f"File: {file_path}:{start_line}-{end_line}\n"
            f"Signature: {signature}\n\n"
            f"{code}\n"
        )
    return "\n".join(parts)


def concept_symbol_retrieve(
    question: str,
    q_emb: np.ndarray,
    ca: ConceptAbstraction,
    retriever: FastEmbeddingRetriever,
    chunks_by_id: dict,
    top_concepts: int = 20,
    top_funcs: int = 50,
) -> list:
    """Concept-Symbol guided retrieval。"""
    symbols = extract_symbols(question)

    # Retrieve concepts
    concepts = ca.retrieve_concepts(
        q_emb, retriever.doc_matrix, retriever.chunk_index_by_id, top_k=top_concepts, scoring="max"
    )

    candidate_fids = set()
    for c, _ in concepts:
        candidate_fids.update(c.function_ids)

    # Rerank by embedding + symbol
    scored = []
    for fid in candidate_fids:
        idx = retriever.chunk_index_by_id.get(fid)
        if idx is None:
            continue
        sim = float(retriever.doc_matrix[idx] @ q_emb)
        sym = symbol_score(fid, symbols, chunks_by_id)
        score = sim + 0.5 * sym
        scored.append((fid, score))

    scored.sort(key=lambda x: -x[1])
    return [fid for fid, _ in scored[:top_funcs]]


def baseline_retrieve(
    question: str,
    q_emb: np.ndarray,
    retriever: FastEmbeddingRetriever,
    top_funcs: int = 50,
) -> list:
    """Baseline: pure embedding retrieval。"""
    results = retriever.retrieve(
        np.asarray([q_emb], dtype=np.float32),
        top_k=top_funcs,
    )
    return [f"{r['metadata']['file_path']}:{r['metadata']['name']}:{r['metadata']['start_line']}"
            for r in results]


def generate_answer(question: str, context: str, client: OpenAI, model: str = "gpt-4.1-mini") -> str:
    prompt = PromptBuilder.answer_generation(question, context)
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        max_tokens=4000,
    )
    return resp.choices[0].message.content.strip()


def run_qa(mode: str, items, q_embs, ca, retriever, chunks_by_id, repo_root, client, model="gpt-4.1-mini"):
    results = []
    for idx, (item, q_emb) in enumerate(zip(items, q_embs), 1):
        print(f"[{idx}/{len(items)}] {mode}: {item['qa_id']} (model={model})", flush=True)

        if mode == "concept_symbol":
            fids = concept_symbol_retrieve(item["question"], q_emb, ca, retriever, chunks_by_id)
        else:
            fids = baseline_retrieve(item["question"], q_emb, retriever)

        context = build_context(fids, chunks_by_id, repo_root)
        answer = generate_answer(item["question"], context, client, model)

        results.append({
            "qa_id": item["qa_id"],
            "question": item["question"],
            "answer": answer,
            "retrieved_functions": fids,
            "model": model,
        })

    return results


def main():
    benchmark_path = _ROOT / "datasets" / "benchmark_hard.json"
    index_path = _ROOT / "data" / "qa_embedding_index.json"
    module_cache = _ROOT / "data" / "module_abstraction.json"
    concept_cache = _ROOT / "data" / "concept_abstraction.json"
    repo_root = Path(REPO_ROOT) if REPO_ROOT else _ROOT

    items = load_benchmark(benchmark_path)
    chunks, doc_matrix = load_embedding_index(index_path)
    line_lookup, chunks_by_id, chunk_index_by_id, func_to_file, file_to_funcs = build_helpers(chunks)

    retriever = FastEmbeddingRetriever(index_path=index_path)
    # FastEmbeddingRetriever 没有 chunk_index_by_id，手动构建
    retriever.chunk_index_by_id = {ch["id"]: idx for idx, ch in enumerate(retriever.chunks)}
    print("Encoding questions...")
    q_embs = retriever.encode_queries([item["question"] for item in items])

    ma = ModuleAbstraction(cache_path=str(module_cache))
    if not ma.load():
        ma.build_from_neo4j()
        ma.save()

    ca = ConceptAbstraction(cache_path=str(concept_cache))
    if not ca.load():
        funcs, calls = fetch_functions_and_calls()
        ca.build_from_modules(ma, functions=funcs, calls=calls)
        ca.save()

    # Run concept_symbol with DeepSeek V4 Flash for answer generation
    ds_client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL or None)

    for ds_model in ["deepseek-v4-pro", "deepseek-v4-flash"]:
        suffix = "deepseek" if ds_model == "deepseek-v4-pro" else "deepseek_flash"
        mode = f"concept_symbol_{suffix}"
        output_path = _ROOT / "results" / f"qa_{mode}.json"
        if output_path.exists():
            print(f"\nSkipping {mode} (already exists: {output_path})")
            continue

        print(f"\n{'='*60}")
        print(f"Running {mode} QA with {ds_model}...")
        print(f"{'='*60}")
        results = run_qa("concept_symbol", items, q_embs, ca, retriever, chunks_by_id, repo_root, ds_client, ds_model)

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"Saved to {output_path}")


if __name__ == "__main__":
    main()
