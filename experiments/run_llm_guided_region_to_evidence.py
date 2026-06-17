#!/usr/bin/env python3
"""
LLM-Guided Region → Evidence。

核心思路：先用 embedding 把问题定位到一个候选区域（Top-k 文件），
然后完全由 LLM 根据问题 + 候选文件内容决定：
1. 哪些符号是关键线索；
2. 需要进一步阅读哪些文件（可以是候选文件，也可以是通过 grep 发现的文件）；
3. 最终如何组织答案。

不预设文件优先级、不硬编码仓库命名规则。所有判断交给 LLM。

流程：
1. Question → Embedding Top-k 候选文件（region）
2. 读取 region 文件内容
3. LLM 输出调查计划：关键 symbols + 需要阅读的文件列表
4. 读取计划中的文件（若来自 grep 则补充内容）
5. LLM 基于所有阅读内容生成带引用的答案

用法:
    python experiments/run_llm_guided_region_to_evidence.py \
        --benchmark datasets/benchmark_hard.json \
        --range 0,15 \
        --top-k 5 \
        --max-extra-files 5 \
        -w 2 \
        -o results/llm_guided_region_to_evidence_0_15.json
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
from src.qa.investigation.base import LLMClient, load_prompt
from src.search.code_reader import read_full_file
from src.search.grep_search_v2 import grep_files


def normalize_path(path: str) -> str:
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


def llm_plan_investigation(question: str, region_files: dict[str, str], llm: LLMClient, max_extra_files: int) -> dict:
    """让 LLM 基于 region 内容制定调查计划。"""
    prompt_template = load_prompt("llm_guided_region_to_evidence_plan")

    summaries = []
    for fp, content in region_files.items():
        snippet = content[:4000] if len(content) <= 8000 else content[:4000] + "\n\n... [中间省略] ...\n\n" + content[-2000:]
        summaries.append(f"=== {fp} ===\n{snippet}")

    prompt = prompt_template.format(
        question=question,
        max_extra_files=max_extra_files,
        region_files="\n\n".join(summaries),
    )
    return llm.call_json(prompt, max_tokens=2000)


def run_item(item: dict, retriever: EmbeddingRetriever, llm: LLMClient, repo_path: str, top_k: int, max_extra_files: int) -> dict:
    # Stage 1: semantic localization
    results = retriever.retrieve(item["question"], top_k=top_k)
    candidates = []
    seen = set()
    for r in results:
        fp = r.metadata.get("file_path", "")
        if fp and fp not in seen:
            candidates.append(fp)
            seen.add(fp)

    # Read region files
    region_files = {}
    for fp in candidates:
        try:
            region_files[fp] = read_full_file(fp)
        except Exception:
            pass

    # Stage 2: LLM plans investigation from region
    plan = llm_plan_investigation(item["question"], region_files, llm, max_extra_files)

    selected_symbols = plan.get("symbols", [])[:5]
    files_to_read = plan.get("files_to_read", [])[:max_extra_files + top_k]

    # Normalize and read all planned files
    all_files_content = dict(region_files)
    visited_files = list(region_files.keys())

    for fp in files_to_read:
        fp = normalize_path(fp)
        if fp not in all_files_content:
            try:
                all_files_content[fp] = read_full_file(fp)
                visited_files.append(fp)
            except Exception:
                # Try grep if file not directly readable (maybe symbol reference)
                pass

    # If symbols were selected, also grep them and let LLM pick from grep results in next step
    # For simplicity, we add grep files to a pool and ask LLM to rank them
    grep_pool = {}
    for symbol in selected_symbols:
        files = grep_files(symbol, repo_path, limit=50)
        for f in files:
            f = normalize_path(f)
            if f not in grep_pool:
                grep_pool[f] = symbol

    # Generate answer from all visited + top grep results
    answer_prompt = load_prompt("generate_answer")
    files_summary = "\n\n".join(
        f"=== {fp} ===\n{all_files_content.get(fp, '')[:2500]}"
        for fp in visited_files
    )
    grep_summary = "\n".join(f"- {fp} (matched symbol: {s})" for fp, s in list(grep_pool.items())[:20])
    prompt = answer_prompt.format(
        question=item["question"],
        evidence_log=f"Selected symbols: {selected_symbols}\nPlanned files: {files_to_read}\nGrep results:\n{grep_summary}",
        files_summary=files_summary,
    )
    answer = llm.call(prompt, max_tokens=8000)
    coverage = compute_coverage(item["gold_files"], answer)

    # Recall from visited files
    gold_set = set(item["gold_files"])
    visited_set = set(visited_files)
    recall_coverage = len(gold_set & visited_set) / len(gold_set) if gold_set else 1.0

    return {
        "qa_id": item["qa_id"],
        "question": item["question"],
        "gold_files": item["gold_files"],
        "candidates": candidates,
        "plan": plan,
        "selected_symbols": selected_symbols,
        "files_to_read": files_to_read,
        "visited_files": visited_files,
        "grep_pool": list(grep_pool.keys())[:20],
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
    parser.add_argument("--max-extra-files", type=int, default=5)
    parser.add_argument("-w", "--workers", type=int, default=2)
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args()

    items = load_items(args.benchmark, args.range)
    print(f"加载 {len(items)} 题，top_k={args.top_k}, max_extra_files={args.max_extra_files}, workers={args.workers}")

    retriever = EmbeddingRetriever()
    llm = LLMClient()
    repo_path = str(get_repo_root())

    results = []
    completed = 0

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                run_item, item, retriever, llm, repo_path, args.top_k, args.max_extra_files
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
    print("LLM-Guided Region → Evidence 结果")
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
