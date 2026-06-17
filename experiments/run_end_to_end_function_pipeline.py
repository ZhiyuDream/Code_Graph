#!/usr/bin/env python3
"""
端到端 Pipeline: Question → Function → Evidence → Answer（轻量 Symbol Fast Path 版）。

流程：
1. Question → Function-chunk embedding Top-10
2. LLM 选择最相关的 function (name_only)
3. 从选中 function 所在文件出发，运行 Symbol Fast Path：
   - LLM 从文件中选 1 个最关键 symbol
   - grep symbol，按规则排序，读取前 N 个文件
   - generate answer
4. 计算 coverage

用法:
    python experiments/run_end_to_end_function_pipeline.py \
        --benchmark datasets/benchmark_hard.json \
        --range 0,15 \
        --top-k 10 \
        --top-n 5 \
        -w 4 \
        -o results/end_to_end_function_pipeline_0_15.json
"""
import json
import re
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from config import get_repo_root
from src.qa.retrievers.embedding import EmbeddingRetriever
from src.qa.investigation.base import BaseInvestigator, LLMClient, load_prompt
from src.search.code_reader import read_full_file
from src.search.grep_search_v2 import grep_files


def normalize_path(path: str) -> str:
    repo_root = str(get_repo_root())
    if path.startswith(repo_root):
        path = path[len(repo_root):].lstrip('/')
    return path.lstrip('./')


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


def select_function(question: str, candidates: list[dict], llm: LLMClient) -> dict:
    """让 LLM 从 function candidates 中选择一个。"""
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


def expand_from_function(entry_function: str, repo_path: str,
                         callees_top_k: int, files_per_symbol: int) -> set[str]:
    """从 function 出发做完整 upstream + downstream expansion。"""
    from collections import Counter

    # Upstream: grep function name to find all occurrences (callers)
    all_occurrences = grep_files(entry_function, repo_path, limit=100)
    visited = {normalize_path(f) for f in all_occurrences}

    # Downstream: read definition file, extract callees, grep them
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




def run_item(item: dict, retriever: EmbeddingRetriever, llm: LLMClient,
             repo_path: str, top_k: int, top_n: int, files_per_symbol: int) -> dict:
    # Stage 1: retrieve function candidates
    results = retriever.retrieve(item["question"], top_k=top_k)
    candidates = []
    seen = set()
    for r in results:
        meta = r.metadata
        key = (meta.get("file_path", ""), meta.get("name", ""))
        if key[0] and key not in seen:
            seen.add(key)
            candidates.append({
                "file_path": meta.get("file_path", ""),
                "name": meta.get("name", ""),
            })

    if not candidates:
        return {"qa_id": item["qa_id"], "error": "no candidates"}

    # Stage 2: LLM select function
    selected = select_function(item["question"], candidates, llm)
    entry_file = selected["file_path"]


    # Stage 3: Full upstream + downstream expansion from selected function
    visited_set = expand_from_function(
        selected["name"], repo_path, top_n, files_per_symbol
    )
    visited = sorted(visited_set)

    # Generate answer
    files_content = {}
    for fp in visited:
        try:
            files_content[fp] = read_full_file(fp)
        except Exception:
            pass

    answer_prompt = load_prompt("generate_answer")
    files_summary = "\n\n".join(
        f"=== {fp} ===\n{files_content.get(fp, '')[:3000]}"
        for fp in visited
    )
    answer = llm.call(answer_prompt.format(
        question=item["question"],
        evidence_log=f"function-level selection: {selected['file_path']}::{selected['name']}",
        files_summary=files_summary,
    ), max_tokens=8000)
    coverage = compute_coverage(item["gold_files"], answer)

    # Check if selected function's file is in gold files
    selected_file = normalize_path(selected["file_path"])
    gold_files_norm = {normalize_path(g) for g in item["gold_files"]}
    selection_hit = selected_file in gold_files_norm or any(selected_file in g or g in selected_file for g in gold_files_norm)

    return {
        "qa_id": item["qa_id"],
        "question": item["question"],
        "gold_files": item["gold_files"],
        "selected_function": selected,
        "selection_hit": selection_hit,
        "visited_files": visited,
        "answer_coverage": coverage,
        "answer": answer,
    }


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--range", default="0,15")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument("--files-per-symbol", type=int, default=3)
    parser.add_argument("-w", "--workers", type=int, default=4)
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args()

    items = load_items(args.benchmark, args.range)
    print(f"加载 {len(items)} 题，top_k={args.top_k}, top_n={args.top_n}, files_per_symbol={args.files_per_symbol}, workers={args.workers}")

    retriever = EmbeddingRetriever()
    llm = LLMClient()
    repo_path = str(get_repo_root())

    results = []
    completed = 0

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                run_item, item, retriever, llm, repo_path,
                args.top_k, args.top_n, args.files_per_symbol
            ): item for item in items
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            completed += 1
            print(f"  [{completed}/{len(items)}] {result['qa_id']}: "
                  f"selected_hit={result.get('selection_hit')} "
                  f"symbol={result.get('selected_symbol')} "
                  f"coverage={result.get('answer_coverage', 0)*100:.0f}%")

    results.sort(key=lambda x: x["qa_id"])

    total = len([r for r in results if "error" not in r])
    avg_coverage = sum(r["answer_coverage"] for r in results if "error" not in r) / total
    full = sum(1 for r in results if "error" not in r and r["answer_coverage"] >= 1.0)
    selection_accuracy = sum(1 for r in results if "error" not in r and r.get("selection_hit")) / total

    print(f"\n{'='*60}")
    print("End-to-End Function-Level Pipeline (Symbol Fast Path)")
    print(f"{'='*60}")
    print(f"总题数: {total}")
    print(f"Function Selection Accuracy: {selection_accuracy*100:.1f}%")
    print(f"平均 Answer Coverage: {avg_coverage*100:.1f}%")
    print(f"引用全对: {full}/{total} ({full/total*100:.1f}%)")

    output = {
        "summary": {
            "total": total,
            "selection_accuracy": selection_accuracy,
            "avg_coverage": avg_coverage,
            "full_correct": full,
        },
        "per_item": results,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
