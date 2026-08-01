#!/usr/bin/env python3
"""端到端 QA：Concept-Symbol + 完整文件代码 + 并发子 Agent 审计。

流程：
Question
  ↓
Extract symbols
  ↓
Retrieve top concepts by embedding
  ↓
Collect functions in concepts (+ optional semantic relation expansion)
  ↓
按文件分组
  ↓
并发子 Agent：读取文件内候选函数完整代码，汇报相关函数
  ↓
主 Agent：汇总相关函数，生成最终答案

对比目标：验证完整代码 + 子 Agent 审计是否比签名/截断代码更能提升 citation coverage。
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
from scripts.analysis.eval_region_compression import (
    fetch_functions_and_calls,
    load_benchmark,
    load_embedding_index,
    build_helpers,
)

# 通用 stopwords，不针对特定数据集
STOPWORDS = {
    "how", "what", "where", "when", "why", "is", "are", "does", "do", "did",
    "the", "a", "an", "and", "or", "in", "on", "at", "to", "for", "of",
    "with", "by", "from", "as", "it", "its", "this", "that", "these", "those",
}


def extract_symbols(question: str) -> list:
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", question)
    symbols = []
    for t in tokens:
        if len(t) < 3 or t.lower() in STOPWORDS:
            continue
        symbols.append(t.lower())
        parts = re.findall(r"[A-Z][a-z]+|[a-z]+|[A-Z]+", t)
        if len(parts) > 1:
            for p in parts:
                if len(p) >= 3 and p.lower() not in STOPWORDS:
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


def read_code(file_path: str, start_line: int, end_line: int, repo_root: Path, max_chars: int = 5000) -> str:
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


def concept_symbol_retrieve(
    question: str,
    q_emb: np.ndarray,
    ca: ConceptAbstraction,
    retriever: FastEmbeddingRetriever,
    chunks_by_id: dict,
    top_concepts: int = 20,
    top_funcs: int = 50,
) -> list:
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
    return [fid for fid, _ in scored[:top_funcs]]


def load_semantic_relations(path: Path) -> dict:
    if not path.exists():
        return {"temporal_calls": [], "implicit_concept": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    temporal_calls = defaultdict(list)
    implicit_concept = defaultdict(list)
    for r in data.get("temporal_relations", []):
        if r["type"] == "CALLS":
            temporal_calls[r["source"]].append(r["target"])
    for r in data.get("implicit_logical_relations", []):
        if r["type"] == "SAME_CONCEPT":
            implicit_concept[r["source"]].append(r["target"])
    return {"temporal_calls": temporal_calls, "implicit_concept": implicit_concept}


def expand_with_relations(fids: list, semantic_relations: dict, max_expansion: int = 20) -> list:
    """沿 CALLS 和 SAME_CONCEPT 扩展 1-hop。"""
    expanded = set(fids)
    for fid in fids:
        for target in semantic_relations["temporal_calls"].get(fid, []):
            expanded.add(target)
        for target in semantic_relations["implicit_concept"].get(fid, []):
            expanded.add(target)
    return list(expanded)[:max_expansion]


def build_file_groups(fids: list, chunks_by_id: dict, repo_root: Path, max_chars_per_func: int = 4000) -> dict:
    """按文件分组，读取完整代码。"""
    file_groups = defaultdict(list)
    for fid in fids:
        meta = chunks_by_id.get(fid, {}).get("meta", {})
        file_path = meta.get("file_path", "")
        if not file_path:
            continue
        start_line = meta.get("start_line", 0)
        end_line = meta.get("end_line", 0)
        code = read_code(file_path, start_line, end_line, repo_root, max_chars=max_chars_per_func)
        file_groups[file_path].append({
            "fid": fid,
            "name": meta.get("name", ""),
            "start_line": start_line,
            "end_line": end_line,
            "signature": meta.get("signature", ""),
            "code": code,
        })
    return dict(file_groups)


def load_prompt(name: str) -> str:
    path = _ROOT / "prompts" / f"{name}.txt"
    return path.read_text(encoding="utf-8")


SUBAGENT_PROMPT = load_prompt("subagent_file_audit")
MAIN_PROMPT = load_prompt("main_answer_from_audit")


def audit_file(question: str, file_path: str, funcs: list, model: str) -> dict:
    """子 Agent：审计单个文件中的函数与问题的相关性，并提取关键代码片段。"""
    functions_code = ""
    for f in funcs:
        functions_code += f"--- Function: {f['name']} ({f['start_line']}-{f['end_line']}) ---\n"
        functions_code += f"Signature: {f['signature']}\n\n{f['code']}\n\n"

    prompt = SUBAGENT_PROMPT.format(
        question=question,
        file_path=file_path,
        functions_code=functions_code,
    )

    result = call_llm_json(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2000,
        model=model,
    )
    if result is None:
        return {
            "file_path": file_path,
            "relevant_functions": [],
            "uncertain_functions": [],
            "audit_error": True,
        }

    return {
        "file_path": file_path,
        "relevant_functions": result.get("relevant_functions", []),
        "uncertain_functions": result.get("uncertain_functions", []),
        "audit_error": False,
    }


def generate_final_answer(question: str, audit_results: list, model: str) -> str:
    """主 Agent：汇总审计结果，生成最终答案。"""
    audit_text = ""
    for r in audit_results:
        audit_text += f"--- File: {r['file_path']} ---\n"

        # 相关函数
        if r.get("relevant_functions"):
            audit_text += "Relevant functions:\n"
            for fn in r["relevant_functions"]:
                if isinstance(fn, dict):
                    audit_text += f"  - {fn.get('name', '')}: {fn.get('reason', '')}\n"
                    if fn.get("key_code"):
                        audit_text += f"    Code:\n{fn['key_code']}\n"
                else:
                    audit_text += f"  - {fn}\n"
        else:
            audit_text += "Relevant functions: (none)\n"

        # 不确定函数
        if r.get("uncertain_functions"):
            audit_text += "Uncertain functions (please judge yourself):\n"
            for fn in r["uncertain_functions"]:
                if isinstance(fn, dict):
                    audit_text += f"  - {fn.get('name', '')}: {fn.get('reason', '')}\n"
                    if fn.get("key_code"):
                        audit_text += f"    Code:\n{fn['key_code']}\n"
                else:
                    audit_text += f"  - {fn}\n"

        audit_text += "\n"

    prompt = MAIN_PROMPT.format(question=question, audit_results=audit_text)
    return call_llm(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=None,
        model=model,
    )


def process_one(
    item: dict,
    q_emb: np.ndarray,
    ca: ConceptAbstraction,
    retriever: FastEmbeddingRetriever,
    chunks_by_id: dict,
    repo_root: Path,
    semantic_relations: dict,
    model: str,
    use_relation_expansion: bool = True,
    top_concepts: int = 20,
    top_funcs: int = 50,
    max_expansion: int = 50,
    max_chars_per_func: int = 4000,
    workers: int = 5,
) -> dict:
    """处理单个问题。"""
    # 1. Concept-Symbol retrieval
    fids = concept_symbol_retrieve(
        item["question"], q_emb, ca, retriever, chunks_by_id,
        top_concepts=top_concepts, top_funcs=top_funcs
    )

    # 2. 语义关系扩展
    if use_relation_expansion:
        fids = expand_with_relations(fids, semantic_relations, max_expansion=max_expansion)

    # 3. 按文件分组
    file_groups = build_file_groups(fids, chunks_by_id, repo_root, max_chars_per_func=max_chars_per_func)

    # 4. 并发子 Agent 审计
    audit_results = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(audit_file, item["question"], fp, funcs, model): fp
            for fp, funcs in file_groups.items()
        }
        for future in as_completed(futures):
            try:
                result = future.result()
                audit_results.append(result)
            except Exception as e:
                fp = futures[future]
                audit_results.append({
                    "file_path": fp,
                    "relevant_functions": [],
                    "reason": f"audit exception: {e}",
                    "audit_error": True,
                })

    # 5. 主 Agent 生成答案
    answer = generate_final_answer(item["question"], audit_results, model)

    return {
        "qa_id": item["qa_id"],
        "question": item["question"],
        "answer": answer,
        "retrieved_functions": fids,
        "audit_results": audit_results,
        "model": model,
    }


def run_qa(
    items: list,
    q_embs: np.ndarray,
    ca: ConceptAbstraction,
    retriever: FastEmbeddingRetriever,
    chunks_by_id: dict,
    repo_root: Path,
    semantic_relations: dict,
    model: str,
    use_relation_expansion: bool = True,
    top_concepts: int = 20,
    top_funcs: int = 50,
    max_expansion: int = 50,
    max_chars_per_func: int = 4000,
    workers: int = 5,
    qa_workers: int = 3,
) -> list:
    """并行运行 QA。"""
    results = []
    with ThreadPoolExecutor(max_workers=qa_workers) as executor:
        futures = {
            executor.submit(
                process_one, item, q_emb, ca, retriever, chunks_by_id,
                repo_root, semantic_relations, model, use_relation_expansion,
                top_concepts, top_funcs, max_expansion, max_chars_per_func, workers
            ): idx
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
    return results


def main():
    parser = argparse.ArgumentParser(description="Run Concept-Symbol Full-File QA with Sub-Agent Audit")
    parser.add_argument("--model", default=None, help="Answer generation model")
    parser.add_argument("--subagent-model", default=None, help="Sub-agent audit model (default: same as model)")
    parser.add_argument("--workers", type=int, default=10, help="Sub-agent workers per question")
    parser.add_argument("--qa-workers", type=int, default=5, help="Parallel question workers")
    parser.add_argument("--use-relation-expansion", action="store_true", default=True)
    parser.add_argument("--no-relation-expansion", dest="use_relation_expansion", action="store_false")
    parser.add_argument("--semantic-relations", type=Path, default=Path("data/semantic_relations.json"))
    parser.add_argument("--output", default=None, help="Output JSON path")
    parser.add_argument("--range", default="all", choices=["all", "easy", "hard"], help="Question range")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of questions (for testing)")
    parser.add_argument("--top-concepts", type=int, default=20)
    parser.add_argument("--top-funcs", type=int, default=50)
    parser.add_argument("--max-expansion", type=int, default=50)
    parser.add_argument("--max-chars-per-func", type=int, default=4000)
    parser.add_argument("--benchmark", type=Path, default=Path("datasets/benchmark_hard.json"))
    args = parser.parse_args()

    model = args.model or LLM_MODEL or "deepseek-v4-pro"
    subagent_model = args.subagent_model or model

    benchmark_path = _ROOT / args.benchmark if not args.benchmark.is_absolute() else args.benchmark
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

    semantic_relations = load_semantic_relations(args.semantic_relations)
    print(f"Loaded semantic relations: {len(semantic_relations['temporal_calls'])} CALLS sources, "
          f"{len(semantic_relations['implicit_concept'])} SAME_CONCEPT sources")

    # 处理 range
    if args.range == "easy":
        items = items[:50]
        q_embs = q_embs[:50]
    elif args.range == "hard":
        items = items[50:]
        q_embs = q_embs[50:]

    if args.limit:
        items = items[:args.limit]
        q_embs = q_embs[:args.limit]

    print(f"\nRunning full-file QA with {model} (subagent: {subagent_model}, relation_expansion: {args.use_relation_expansion})")
    print(f"Questions: {len(items)}, QA workers: {args.qa_workers}, Sub-agent workers: {args.workers}")

    results = run_qa(
        items, q_embs, ca, retriever, chunks_by_id, repo_root,
        semantic_relations, model, args.use_relation_expansion,
        top_concepts=args.top_concepts,
        top_funcs=args.top_funcs,
        max_expansion=args.max_expansion,
        max_chars_per_func=args.max_chars_per_func,
        workers=args.workers, qa_workers=args.qa_workers,
    )

    if args.output:
        output_path = Path(args.output)
    else:
        suffix = model.replace("/", "_")
        output_path = _ROOT / "results" / f"qa_concept_symbol_fullfile_{suffix}.json"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\nSaved results to: {output_path}")


if __name__ == "__main__":
    main()
