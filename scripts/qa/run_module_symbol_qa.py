#!/usr/bin/env python3
"""
端到端 QA：Module-Symbol Guided Investigation。

对比 Baseline 与 Module + Symbol 在端到端 QA 中的效果。
支持并行执行与 DeepSeek / OpenAI answer generation。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from config import LLM_MODEL, REPO_ROOT
from src.core.llm_client import call_llm
from src.core.module_abstraction import ModuleAbstraction
from src.qa.prompts import PromptBuilder
from src.qa.retrievers.fast_embedding import FastEmbeddingRetriever
from scripts.analysis.eval_region_compression import (
    load_benchmark,
    load_embedding_index,
    build_helpers,
)


def extract_symbols(question: str) -> List[str]:
    """从问题中提取候选 symbols（同实验十六/十七）。"""
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


def symbol_score(fid: str, symbols: List[str], chunks_by_id: Dict) -> float:
    """计算 function 与 question symbols 的匹配分数。"""
    if not symbols:
        return 0.0
    meta = chunks_by_id.get(fid, {}).get("meta", {})
    name = meta.get("name", "").lower()
    file_path = meta.get("file_path", "").lower()
    signature = meta.get("signature", "").lower()
    text = chunks_by_id.get(fid, {}).get("text", "").lower()
    text_all = f"{name} {file_path} {signature} {text}"

    matched = 0.0
    for sym in symbols:
        if sym in name:
            matched += 2.0
        elif sym in text_all:
            matched += 1.0
    return matched / (len(symbols) * 2.0)


def read_code(file_path: str, start_line: int, end_line: int, repo_root: Path, max_chars: int = 3000) -> str:
    """读取函数代码。"""
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


def build_context(fids: List[str], chunks_by_id: Dict, repo_root: Path, max_funcs: int = 20) -> str:
    """构建 LLM context。"""
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


def module_symbol_retrieve(
    question: str,
    q_emb: np.ndarray,
    ma: ModuleAbstraction,
    retriever: FastEmbeddingRetriever,
    chunks_by_id: Dict,
    top_modules: int = 5,
    top_funcs: int = 50,
) -> List[str]:
    """Module-Symbol guided retrieval。"""
    symbols = extract_symbols(question)

    # Retrieve modules by embedding max similarity
    module_candidates = ma.retrieve_modules(
        q_emb, retriever.doc_matrix, retriever.chunk_index_by_id, top_k=top_modules, scoring="max"
    )

    candidate_fids = set()
    for m, _ in module_candidates:
        candidate_fids.update(m.function_ids)

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
) -> List[str]:
    """Baseline: pure embedding retrieval。"""
    results = retriever.retrieve(
        np.asarray([q_emb], dtype=np.float32),
        top_k=top_funcs,
    )
    return [f"{r['metadata']['file_path']}:{r['metadata']['name']}:{r['metadata']['start_line']}"
            for r in results]


def generate_answer(question: str, context: str, model: str) -> str:
    """调用 LLM 生成答案（统一走 call_llm）。"""
    prompt = PromptBuilder.answer_generation(question, context)
    return call_llm(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=4000,
        model=model,
    )


def process_one(
    item: Dict,
    q_emb: np.ndarray,
    mode: str,
    ma: ModuleAbstraction,
    retriever: FastEmbeddingRetriever,
    chunks_by_id: Dict,
    repo_root: Path,
    model: str,
) -> Dict:
    """处理单个问题。"""
    if mode == "baseline":
        fids = baseline_retrieve(item["question"], q_emb, retriever)
    elif mode.startswith("module_top"):
        top_modules = int(mode.split("_")[1].replace("top", ""))
        fids = module_symbol_retrieve(
            item["question"], q_emb, ma, retriever, chunks_by_id,
            top_modules=top_modules, top_funcs=50
        )
    else:
        raise ValueError(f"Unknown mode: {mode}")

    context = build_context(fids, chunks_by_id, repo_root)
    answer = generate_answer(item["question"], context, model)

    return {
        "qa_id": item["qa_id"],
        "question": item["question"],
        "answer": answer,
        "retrieved_functions": fids,
        "model": model,
        "mode": mode,
    }


def run_qa_parallel(
    mode: str,
    items: List[Dict],
    q_embs: np.ndarray,
    ma: ModuleAbstraction,
    retriever: FastEmbeddingRetriever,
    chunks_by_id: Dict,
    repo_root: Path,
    model: str,
    workers: int = 10,
) -> List[Dict]:
    """并行运行 QA。"""
    results = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                process_one,
                item, q_emb, mode, ma, retriever, chunks_by_id, repo_root, model
            ): idx
            for idx, (item, q_emb) in enumerate(zip(items, q_embs))
        }
        for future in as_completed(futures):
            idx = futures[future]
            try:
                result = future.result()
                results.append(result)
                print(f"[{len(results)}/{len(items)}] {mode}: {result['qa_id']} done", flush=True)
            except Exception as e:
                print(f"[ERROR] {mode} idx={idx}: {e}", flush=True)

    # 按原始顺序排序
    qa_id_to_idx = {item["qa_id"]: i for i, item in enumerate(items)}
    results.sort(key=lambda r: qa_id_to_idx.get(r["qa_id"], 0))
    return results


def main():
    parser = argparse.ArgumentParser(description="Run Module-Symbol end-to-end QA")
    parser.add_argument("--mode", choices=["baseline", "module_top5_symbol", "module_top10_symbol"],
                        default="module_top5_symbol", help="QA mode")
    parser.add_argument("--model", default=None,
                        help="Answer generation model (默认: LLM_MODEL 环境变量，否则 deepseek-v4-pro)")
    parser.add_argument("--workers", type=int, default=15, help="Parallel workers")
    parser.add_argument("--output", default=None, help="Output JSON path")
    args = parser.parse_args()

    model = args.model or LLM_MODEL or "deepseek-v4-pro"

    benchmark_path = _ROOT / "datasets" / "benchmark_hard.json"
    index_path = _ROOT / "data" / "qa_embedding_index.json"
    module_cache = _ROOT / "data" / "module_abstraction.json"
    repo_root = Path(REPO_ROOT) if REPO_ROOT else _ROOT

    items = load_benchmark(benchmark_path)
    chunks, doc_matrix = load_embedding_index(index_path)
    _, chunks_by_id, _, _, _ = build_helpers(chunks)

    retriever = FastEmbeddingRetriever(index_path=index_path)
    retriever.chunk_index_by_id = {ch["id"]: idx for idx, ch in enumerate(retriever.chunks)}
    print("Encoding questions...")
    q_embs = retriever.encode_queries([item["question"] for item in items])

    ma = ModuleAbstraction(cache_path=str(module_cache))
    if not ma.load():
        raise RuntimeError("Module abstraction not found. Please build it first.")

    print(f"\nRunning {args.mode} QA with {model} (workers={args.workers})...")
    results = run_qa_parallel(
        args.mode, items, q_embs, ma, retriever, chunks_by_id, repo_root,
        model, workers=args.workers
    )

    if args.output:
        output_path = Path(args.output)
    else:
        suffix = model.replace("/", "_")
        output_path = _ROOT / "results" / f"qa_{args.mode}_{suffix}.json"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\nSaved results to: {output_path}")


if __name__ == "__main__":
    main()
