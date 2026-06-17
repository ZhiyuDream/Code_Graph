#!/usr/bin/env python3
"""
Multi-Entry Symbol Expansion Growth Curve（确定性版本）。

目的：测量"从 embedding Top-k 候选中扩展多个入口"能否召回更多 gold files。
本脚本避免使用 LLM 做 symbol 选择，减少运行方差。

流程：
1. Question → Embedding Top-k 候选文件
2. 对前 M 个候选文件分别做确定性 symbol fast path：
   - 从入口文件提取本地 symbol
   - 选择最相关 symbol（按定义位置、长度、优先级排序）
   - grep symbol 找到所有出现位置
   - 按文件优先级排序，读取前 K 个文件
3. 合并所有访问过的文件
4. 计算 visited-files recall coverage
5. 对每个 M 单独调用 LLM 生成答案，计算 answer-based coverage

用法:
    python experiments/run_multi_entry_growth_curve.py \
        --benchmark datasets/benchmark_hard.json \
        --range 0,15 \
        --top-k 10 \
        --evidence-per-entry 3 \
        -w 4 \
        -o results/multi_entry_growth_curve_0_15.json
"""
import json
import re
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.qa.retrievers.embedding import EmbeddingRetriever
from src.qa.investigation.base import BaseInvestigator, LLMClient
from src.search.code_reader import read_full_file
from src.search.grep_search_v2 import grep_files


def normalize_path(path: str) -> str:
    return path.lstrip('./')


def extract_symbols(content: str) -> list[str]:
    """轻量版本地 symbol 提取（复用 BaseInvestigator 逻辑但避免实例化）。"""
    symbols = set()
    func_pattern = re.compile(
        r'(?:^|\n)\s*(?:static\s+|inline\s+|virtual\s+|constexpr\s+)?'
        r'(?:[\w:\*\&<>\[\]]+\s+)+'
        r'(\w+)\s*\([^)]*\)\s*(?:const\s*)?(?:noexcept\s*)?(?:override\s*)?\s*(?:try\s*)?\{',
        re.MULTILINE
    )
    for m in func_pattern.finditer(content):
        symbols.add(m.group(1))
    decl_pattern = re.compile(
        r'(?:^|\n)\s*(?:static\s+|inline\s+|virtual\s+|constexpr\s+)?'
        r'(?:[\w:\*\&<>\[\]]+\s+)+'
        r'(\w+)\s*\([^)]*\)\s*(?:const\s*)?(?:noexcept\s*)?(?:override\s*)?\s*;',
        re.MULTILINE
    )
    for m in decl_pattern.finditer(content):
        symbols.add(m.group(1))
    class_pattern = re.compile(
        r'(?:^|\n)\s*(?:class|struct)\s+(?:[A-Z_]+\s+)?(\w+)',
        re.MULTILINE
    )
    for m in class_pattern.finditer(content):
        symbols.add(m.group(1))
    stopwords = {
        'if', 'while', 'for', 'switch', 'return', 'else', 'catch', 'try',
        'class', 'struct', 'namespace', 'using', 'template', 'public',
        'private', 'protected', 'default', 'delete', 'override', 'final',
        'const', 'static', 'inline', 'virtual', 'explicit', 'operator',
        'true', 'false', 'nullptr', 'NULL', 'void', 'int', 'bool', 'size_t',
        'char', 'float', 'double', 'long', 'short', 'unsigned', 'signed',
        'auto', 'decltype', 'typename', 'public', 'private', 'protected',
        'noexcept', 'constexpr', 'consteval', 'constinit', 'mutable',
        'volatile', 'register', 'extern', 'friend', 'typedef', 'union',
        'enum', 'goto', 'case', 'break', 'continue', 'throw', 'new', 'delete',
    }
    return [s for s in symbols if s not in stopwords and len(s) >= 2]


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


def select_best_symbol(symbols: list[str], entry_file: str, repo_path: str) -> str | None:
    """选择最适合 grep 扩展的 symbol。优先：在 repo 中出现次数适中、不在 tests 过多。"""
    if not symbols:
        return None

    def score(symbol: str) -> float:
        files = grep_files(symbol, repo_path, limit=200)
        priors = [file_priority(f) for f in files]
        if not priors:
            return -1
        max_prior = max(priors)
        count = len(files)
        # Prefer symbols with some high-priority files but not too many hits
        return max_prior * 10 - min(count, 100)

    return max(symbols, key=score)


def expand_from_entry(entry_file: str, repo_path: str, evidence_per_entry: int, all_files_content: dict) -> list[str]:
    """确定性从一个入口扩展，返回访问的文件列表（包含入口）。"""
    visited = []
    try:
        content = read_full_file(entry_file)
        all_files_content[entry_file] = content
        visited.append(entry_file)
    except Exception:
        return visited

    symbols = extract_symbols(content)
    symbol = select_best_symbol(symbols, entry_file, repo_path)
    if not symbol:
        return visited

    files = grep_files(symbol, repo_path, limit=100)
    files = [normalize_path(f) for f in files]
    files = sorted(set(files), key=lambda f: (-file_priority(f), f))

    for f in files[:evidence_per_entry]:
        if f in visited:
            continue
        try:
            all_files_content[f] = read_full_file(f)
            visited.append(f)
        except Exception:
            pass

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


def generate_answer_for_visited(question: str, visited: list[str], files_content: dict, llm: LLMClient) -> str:
    """基于访问文件生成答案。"""
    from src.qa.investigation.base import load_prompt
    answer_prompt = load_prompt("generate_answer")
    files_summary = "\n\n".join(
        f"=== {fp} ===\n{files_content.get(fp, '')[:2000]}"
        for fp in visited
    )
    prompt = answer_prompt.format(
        question=question,
        evidence_log="multi-entry deterministic symbol expansion",
        files_summary=files_summary,
    )
    return llm.call(prompt, max_tokens=8000)


def run_item_for_n(item: dict, candidates: list[str], repo_path: str, n: int, evidence_per_entry: int, llm: LLMClient) -> dict:
    files_content = {}
    all_visited = []
    entry_details = []

    for entry_file in candidates[:n]:
        visited = expand_from_entry(entry_file, repo_path, evidence_per_entry, files_content)
        entry_details.append({
            "entry": entry_file,
            "visited": visited,
            "entry_hit": entry_file in item["gold_files"],
        })
        for f in visited:
            if f not in all_visited:
                all_visited.append(f)

    # Recall coverage from visited files
    gold_set = set(item["gold_files"])
    visited_set = set(all_visited)
    visited_hit = gold_set & visited_set
    recall_coverage = len(visited_hit) / len(gold_set) if gold_set else 1.0

    # Answer-based coverage
    answer = generate_answer_for_visited(item["question"], all_visited, files_content, llm)
    answer_coverage = compute_coverage(item["gold_files"], answer)

    return {
        "qa_id": item["qa_id"],
        "n": n,
        "gold_files": item["gold_files"],
        "candidates": candidates,
        "entries": entry_details,
        "visited_files": all_visited,
        "visited_hit_files": sorted(visited_hit),
        "recall_coverage": recall_coverage,
        "answer_coverage": answer_coverage,
        "answer": answer,
    }


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--range", default="0,15")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--evidence-per-entry", type=int, default=3)
    parser.add_argument("-w", "--workers", type=int, default=4)
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args()

    items = load_items(args.benchmark, args.range)
    print(f"加载 {len(items)} 题，top_k={args.top_k}, evidence_per_entry={args.evidence_per_entry}, workers={args.workers}")

    from config import get_repo_root
    repo_path = str(get_repo_root())
    retriever = EmbeddingRetriever()
    llm = LLMClient()

    ns = [1, 2, 3, 5]
    all_results = {n: [] for n in ns}

    # Pre-compute candidates once per item
    item_candidates = []
    for item in items:
        results = retriever.retrieve(item["question"], top_k=args.top_k)
        candidates = []
        seen = set()
        for r in results:
            fp = r.metadata.get("file_path", "")
            if fp and fp not in seen:
                candidates.append(fp)
                seen.add(fp)
        item_candidates.append((item, candidates))

    total_tasks = len(items) * len(ns)
    completed = 0

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {}
        for item, candidates in item_candidates:
            for n in ns:
                future = executor.submit(
                    run_item_for_n, item, candidates, repo_path, n, args.evidence_per_entry, llm
                )
                futures[future] = (item["qa_id"], n)

        for future in as_completed(futures):
            result = future.result()
            qa_id, n = futures[future]
            all_results[n].append(result)
            completed += 1
            print(f"  [{completed}/{total_tasks}] {qa_id} n={n}: "
                  f"recall={result['recall_coverage']*100:.0f}% answer={result['answer_coverage']*100:.0f}% "
                  f"visited={len(result['visited_files'])}")

    summary = {}
    print(f"\n{'='*60}")
    print("Multi-Entry Growth Curve（确定性 Symbol Expansion）")
    print(f"{'='*60}")
    for n in ns:
        results = all_results[n]
        avg_recall = sum(r["recall_coverage"] for r in results) / len(results)
        avg_answer = sum(r["answer_coverage"] for r in results) / len(results)
        full = sum(1 for r in results if r["answer_coverage"] >= 1.0)
        summary[n] = {
            "avg_recall_coverage": avg_recall,
            "avg_answer_coverage": avg_answer,
            "full_correct": full,
            "total": len(results),
        }
        print(f"n={n}: recall={avg_recall*100:.1f}% answer={avg_answer*100:.1f}% full={full}/{len(results)}")

    output = {
        "ns": ns,
        "summary": summary,
        "per_item": all_results,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
