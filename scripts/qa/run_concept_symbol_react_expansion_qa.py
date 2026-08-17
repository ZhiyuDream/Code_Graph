#!/usr/bin/env python3
"""Concept-Symbol 为主，ReAct 为补充的 QA。

流程：
1. Concept-Symbol retrieval 得到 top-50 函数（主要 retrieval，覆盖 66%）
2. ReAct 有限扩展：对每个函数，用 grep_callers/grep_callees 找相关函数，读取实现
3. 最终答案基于 Concept-Symbol 函数 + ReAct 扩展函数

核心原则：ReAct 是 Concept-Symbol 的补充，不是替代。只在 Concept-Symbol 准确基础上做有限扩展。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from config import LLM_MODEL, REPO_ROOT
from src.core.llm_client import call_llm, call_llm_json
from src.core.concept_abstraction import ConceptAbstraction
from src.core.module_abstraction import ModuleAbstraction
from src.qa.retrievers.fast_embedding import FastEmbeddingRetriever
from src.qa.tools.grep_call_chain import grep_callers, grep_callees, read_function
from scripts.analysis.eval_region_compression import (
    fetch_functions_and_calls,
    load_benchmark,
    load_embedding_index,
    build_helpers,
)


# ── Concept-Symbol Retrieval ────────────────────────────────────────

def extract_symbols(question: str) -> list:
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", question)
    stopwords = {
        "how", "what", "where", "when", "why", "is", "are", "does", "do", "did",
        "the", "a", "an", "and", "or", "in", "on", "at", "to", "for", "of",
        "with", "by", "from", "as", "it", "its", "this", "that", "these", "those",
    }
    symbols = []
    for t in tokens:
        if len(t) < 3 or t.lower() in stopwords:
            continue
        symbols.append(t.lower())
        parts = re.findall(r"[A-Z][a-z]+|[a-z]+|[A-Z]+", t)
        if len(parts) > 1:
            for p in parts:
                if len(p) >= 3 and p.lower() not in stopwords:
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


def concept_symbol_retrieve(
    question: str,
    q_emb: np.ndarray,
    ca: ConceptAbstraction,
    retriever: FastEmbeddingRetriever,
    chunks_by_id: dict,
    top_concepts: int = 20,
    top_funcs: int = 50,
) -> list[dict]:
    """Concept-Symbol retrieval: get top functions with accurate positions."""
    symbols = extract_symbols(question)
    concepts = ca.retrieve_concepts(
        q_emb, retriever.doc_matrix, retriever.chunk_index_by_id, top_k=top_concepts, scoring="max"
    )

    candidate_fids = set()
    for c, _ in concepts:
        candidate_fids.update(c.function_ids)

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

    functions = []
    for fid, score in scored[:top_funcs]:
        meta = chunks_by_id.get(fid, {}).get("meta", {})
        functions.append({
            "fid": fid,
            "name": meta.get("name", ""),
            "file_path": meta.get("file_path", ""),
            "start_line": meta.get("start_line", 0),
            "end_line": meta.get("end_line", 0),
            "signature": meta.get("signature", ""),
            "score": score,
        })
    return functions


# ── ReAct Limited Expansion ─────────────────────────────────────────

def react_expand_functions(
    functions: list[dict],
    repo_root: Path,
    max_expansion_per_func: int = 3,
) -> list[dict]:
    """ReAct limited expansion: for each function, find callers/callees and read their context."""
    expanded = []
    seen_fids = {f["fid"] for f in functions}

    for func in functions:
        # 保留原函数
        expanded.append(func)

        # 用 grep_callers 找调用方，读取调用位置附近的代码
        callers = grep_callers(func["name"], repo_root, limit=max_expansion_per_func)
        for caller in callers:
            caller_fid = f"{caller['file']}:caller_of_{func['name']}:{caller['line']}"
            if caller_fid not in seen_fids:
                seen_fids.add(caller_fid)
                # 读取调用位置附近的代码（前后 20 行）
                try:
                    abs_path = repo_root / caller["file"]
                    with open(abs_path, encoding="utf-8", errors="replace") as f:
                        lines = f.readlines()
                    start = max(0, caller["line"] - 10)
                    end = min(len(lines), caller["line"] + 10)
                    code = "".join(lines[start:end])
                    expanded.append({
                        "fid": caller_fid,
                        "name": f"caller_of_{func['name']}",
                        "file_path": caller["file"],
                        "start_line": start + 1,
                        "end_line": end,
                        "signature": "",
                        "score": func["score"] * 0.9,
                        "source": f"caller_of_{func['name']}",
                        "code": code,
                    })
                except Exception:
                    pass

        # 用 grep_callees 找被调用方，读取实现
        callees = grep_callees(func["name"], func["file_path"], func["start_line"], func["end_line"], repo_root, limit=max_expansion_per_func)
        for callee in callees:
            callee_fid = f"{callee['file']}:{callee['name']}:0"
            if callee_fid not in seen_fids:
                seen_fids.add(callee_fid)
                # 读取被调用方函数实现
                result = read_function(callee["name"], callee["file"], repo_root)
                if "error" not in result:
                    expanded.append({
                        "fid": callee_fid,
                        "name": callee["name"],
                        "file_path": callee["file"],
                        "start_line": result.get("start_line", 0),
                        "end_line": result.get("end_line", 0),
                        "signature": "",
                        "score": func["score"] * 0.9,
                        "source": f"callee_of_{func['name']}",
                        "code": result.get("code", ""),
                    })

    return expanded


# ── Answer Generation ───────────────────────────────────────────────

def build_context(functions: list[dict], repo_root: Path, max_funcs: int = 20) -> str:
    """Build LLM context from functions."""
    parts = []
    for f in functions[:max_funcs]:
        code = ""
        if f.get("file_path") and f.get("start_line") and f.get("end_line"):
            result = read_function(f["name"], f["file_path"], repo_root)
            if "error" not in result:
                code = result.get("code", "")
        parts.append(
            f"--- Function: {f['name']} ---\n"
            f"File: {f['file_path']}:{f['start_line']}-{f['end_line']}\n"
            f"Signature: {f.get('signature', '')}\n\n"
            f"{code}\n"
        )
    return "\n".join(parts)


ANSWER_PROMPT = """请基于以下代码信息，回答问题。

【问题】
{question}

【相关函数代码】
{context}

请生成详细的技术答案，要求：
1. 使用中文回答。
2. 明确引用相关文件路径和函数名。
3. 说明这些函数在解决该问题中的作用和相互关系。
4. 如果涉及执行流程，请按调用/执行顺序描述。
5. 答案末尾列出所有引用的文件路径。
"""


def generate_answer(question: str, context: str, model: str) -> str:
    prompt = ANSWER_PROMPT.format(question=question, context=context)
    return call_llm(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=None,
        model=model,
    )


# ── Main ────────────────────────────────────────────────────────────

def process_one(item, q_emb, ca, retriever, chunks_by_id, repo_root, model, use_react_expansion=True):
    question = item["question"]
    qa_id = item["qa_id"]

    # 1. Concept-Symbol retrieval（主要 retrieval）
    functions = concept_symbol_retrieve(question, q_emb, ca, retriever, chunks_by_id, top_concepts=20, top_funcs=50)

    # 2. ReAct 有限扩展（补充 retrieval）
    if use_react_expansion:
        functions = react_expand_functions(functions, repo_root, max_expansion_per_func=3)

    # 3. 生成答案
    context = build_context(functions, repo_root, max_funcs=20)
    answer = generate_answer(question, context, model)

    return {
        "qa_id": qa_id,
        "question": question,
        "answer": answer,
        "retrieved_functions": [f["fid"] for f in functions],
        "model": model,
    }


def main():
    parser = argparse.ArgumentParser(description="Concept-Symbol + ReAct Limited Expansion QA")
    parser.add_argument("--model", default=None, help="LLM model")
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--output", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--no-react-expansion", action="store_true", help="Disable ReAct expansion")
    args = parser.parse_args()

    model = args.model or LLM_MODEL or "deepseek-v4-flash"

    benchmark_path = _ROOT / "datasets" / "benchmark_hard.json"
    index_path = _ROOT / "data" / "qa_embedding_index.json"
    module_cache = _ROOT / "data" / "module_abstraction.json"
    concept_cache = _ROOT / "data" / "concept_abstraction.json"
    repo_root = Path(REPO_ROOT) if REPO_ROOT else _ROOT

    items = load_benchmark(benchmark_path)
    chunks, doc_matrix = load_embedding_index(index_path)
    line_lookup, chunks_by_id, chunk_index_by_id, func_to_file, file_to_funcs = build_helpers(chunks)

    retriever = FastEmbeddingRetriever(index_path=index_path)
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

    if args.limit:
        items = items[:args.limit]
        q_embs = q_embs[:args.limit]

    print(f"\nRunning Concept-Symbol + ReAct Limited Expansion QA with {model}")
    print(f"Questions: {len(items)}, ReAct expansion: {not args.no_react_expansion}")

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(process_one, item, q_emb, ca, retriever, chunks_by_id, repo_root, model, not args.no_react_expansion): idx
            for idx, (item, q_emb) in enumerate(zip(items, q_embs))
        }
        for future in as_completed(futures):
            idx = futures[future]
            try:
                result = future.result()
                results.append(result)
                print(f"[{len(results)}/{len(items)}] {result['qa_id']} done", flush=True)
            except Exception as e:
                print(f"[ERROR] idx={idx}: {e}", flush=True)

    qa_id_to_idx = {item["qa_id"]: i for i, item in enumerate(items)}
    results.sort(key=lambda r: qa_id_to_idx.get(r["qa_id"], 0))

    if args.output:
        output_path = Path(args.output)
    else:
        suffix = model.replace("/", "_")
        output_path = _ROOT / "results" / f"qa_concept_symbol_react_expansion_{suffix}.json"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\nSaved results to: {output_path}")


if __name__ == "__main__":
    main()
