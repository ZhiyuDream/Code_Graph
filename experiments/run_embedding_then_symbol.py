#!/usr/bin/env python3
"""
Embedding → Symbol Selection → Expansion。

核心思路：不直接从问题文本猜 symbol，而是先用 embedding 召回相关文件（region），
然后让 LLM 从 region 内容中根据问题选择关键 symbol，再做结构化扩展。

流程：
1. Question → Embedding Top-k 候选文件
2. 读取候选文件内容（截断）
3. LLM 根据 Question + 候选内容选择 1-3 个关键 symbol
4. 对每个 symbol grep，合并相关文件，按优先级读取前 M 个
5. 生成答案

验证：Region 内容是否能帮助 LLM 正确识别 symbol，从而弥合端到端差距。

用法:
    python experiments/run_embedding_then_symbol.py \
        --benchmark datasets/benchmark_hard.json \
        --range 0,15 \
        --top-k 5 \
        --max-files-per-symbol 3 \
        --max-symbols 3 \
        -w 2 \
        -o results/embedding_then_symbol_0_15.json
"""
import json
import re
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.qa.retrievers.embedding import EmbeddingRetriever
from src.qa.investigation.base import LLMClient, load_prompt
from src.search.code_reader import read_full_file
from src.search.grep_search_v2 import grep_files


def normalize_path(path: str) -> str:
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


def extract_cited_files(answer: str) -> set[str]:
    cited = set()
    marker = "## 引用文件清单"
    idx = answer.rfind(marker)
    if idx >= 0:
        list_section = answer[idx + len(marker):]
        for line in list_section.split('\n'):
            line = line.strip()
            if line.startswith('- '):
                content = line[2:].strip()
                m = re.search(r'`?([\w/\-\.]+\.(?:cpp|c|h|hpp))(?::\d+)?`?', content)
                if m:
                    cited.add(m.group(1))
    return cited


def compute_coverage(gold_files: list[str], answer: str) -> float:
    if not gold_files:
        return 1.0
    cited = extract_cited_files(answer)
    cited_normalized = {normalize_path(f) for f in cited}
    gold_normalized = {normalize_path(f) for f in gold_files}
    hit = len(gold_normalized & cited_normalized)
    return hit / len(gold_normalized)


def select_symbols_from_region(question: str, candidates: list[str], files_content: dict, llm: LLMClient, max_symbols: int) -> list[str]:
    """让 LLM 根据问题 + 候选文件内容选择关键 symbol。"""
    prompt_template = load_prompt("select_symbols_from_region")

    # Prepare file summaries
    summaries = []
    for fp in candidates:
        content = files_content.get(fp, "")
        summary = content[:3000] if len(content) <= 6000 else content[:3000] + "\n\n... [中间省略] ...\n\n" + content[-1500:]
        summaries.append(f"=== {fp} ===\n{summary}")

    prompt = prompt_template.format(
        question=question,
        max_symbols=max_symbols,
        file_summaries="\n\n".join(summaries),
    )
    text = llm.call(prompt, max_tokens=1500).strip()

    symbols = []
    for line in text.split('\n'):
        line = line.strip()
        if not line:
            continue
        # Remove markdown formatting and numbering
        line = re.sub(r'^[-\d\.\*\+]+\s*', '', line).strip()
        line = line.strip('`"')
        if line and line.upper() != "UNKNOWN" and len(line) >= 2:
            symbols.append(line)

    return symbols[:max_symbols]


def run_item(item: dict, retriever: EmbeddingRetriever, llm: LLMClient, top_k: int, max_symbols: int, max_files_per_symbol: int) -> dict:
    # Stage 1: semantic localization
    results = retriever.retrieve(item["question"], top_k=top_k)
    candidates = []
    seen = set()
    for r in results:
        fp = r.metadata.get("file_path", "")
        if fp and fp not in seen:
            candidates.append(fp)
            seen.add(fp)

    # Read candidate contents
    files_content = {}
    for fp in candidates:
        try:
            files_content[fp] = read_full_file(fp)
        except Exception:
            pass

    # Stage 2: symbol selection from region
    symbols = select_symbols_from_region(item["question"], candidates, files_content, llm, max_symbols)

    # Stage 3: symbol-guided expansion
    all_files = set()
    symbol_details = []
    for symbol in symbols:
        files = grep_files(symbol, str(Path(__file__).resolve().parent.parent / ".." / "llama.cpp"), limit=100)
        files = [normalize_path(f) for f in files]
        files = sorted(set(files), key=lambda f: (-file_priority(f), f))
        selected = files[:max_files_per_symbol]

        for f in selected:
            if f not in files_content:
                try:
                    files_content[f] = read_full_file(f)
                except Exception:
                    pass
        all_files.update(selected)
        symbol_details.append({
            "symbol": symbol,
            "files": selected,
        })

    # Generate answer
    answer_prompt = load_prompt("generate_answer")
    files_summary = "\n\n".join(
        f"=== {fp} ===\n{files_content.get(fp, '')[:2000]}"
        for fp in sorted(all_files)
    )
    prompt = answer_prompt.format(
        question=item["question"],
        evidence_log="embedding → symbol selection → expansion",
        files_summary=files_summary,
    )
    answer = llm.call(prompt, max_tokens=8000)
    coverage = compute_coverage(item["gold_files"], answer)

    # Recall from visited files
    gold_set = set(item["gold_files"])
    visited_set = set(all_files)
    recall_coverage = len(gold_set & visited_set) / len(gold_set) if gold_set else 1.0

    return {
        "qa_id": item["qa_id"],
        "question": item["question"],
        "gold_files": item["gold_files"],
        "candidates": candidates,
        "selected_symbols": symbols,
        "symbol_details": symbol_details,
        "visited_files": sorted(all_files),
        "recall_coverage": recall_coverage,
        "answer_coverage": coverage,
        "answer": answer,
    }


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--range", default="0,15")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--max-symbols", type=int, default=3)
    parser.add_argument("--max-files-per-symbol", type=int, default=3)
    parser.add_argument("-w", "--workers", type=int, default=2)
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args()

    items = load_items(args.benchmark, args.range)
    print(f"加载 {len(items)} 题，top_k={args.top_k}, max_symbols={args.max_symbols}, "
          f"max_files_per_symbol={args.max_files_per_symbol}, workers={args.workers}")

    retriever = EmbeddingRetriever()
    llm = LLMClient()

    results = []
    completed = 0

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                run_item, item, retriever, llm, args.top_k, args.max_symbols, args.max_files_per_symbol
            ): item for item in items
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            completed += 1
            print(f"  [{completed}/{len(items)}] {result['qa_id']}: "
                  f"symbols={result['selected_symbols']} "
                  f"recall={result['recall_coverage']*100:.0f}% answer={result['answer_coverage']*100:.0f}%")

    results.sort(key=lambda x: x["qa_id"])

    avg_recall = sum(r["recall_coverage"] for r in results) / len(results)
    avg_answer = sum(r["answer_coverage"] for r in results) / len(results)
    full = sum(1 for r in results if r["answer_coverage"] >= 1.0)

    print(f"\n{'='*60}")
    print("Embedding → Symbol Selection → Expansion 结果")
    print(f"{'='*60}")
    print(f"总题数: {len(results)}")
    print(f"引用全: {full}/{len(results)} ({full/len(results)*100:.0f}%)")
    print(f"平均 recall coverage: {avg_recall*100:.1f}%")
    print(f"平均 answer coverage: {avg_answer*100:.1f}%")

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
