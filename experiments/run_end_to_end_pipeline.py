#!/usr/bin/env python3
"""
端到端 pipeline 实验：Question → Entry(embedding) → Symbol Fast Path → Answer。

验证完整的两层流程：
1. Layer 1: 用 embedding 检索找到入口文件
2. Layer 2: 从入口文件用 symbol + 工具扩展找到证据
3. 生成答案并评估 coverage

用法:
    python experiments/run_end_to_end_pipeline.py \
        --benchmark datasets/benchmark_hard.json \
        --range 0,15 \
        --entry-top-k 1 \
        --evidence-top-n 5 \
        -w 2 \
        -o results/end_to_end_0_15.json
"""
import json
import re
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.qa.retrievers.embedding import EmbeddingRetriever
from src.qa.investigation import DynamicInvestigator
from src.qa.investigation.base import InvestigationState, load_prompt
from src.search.grep_search_v2 import grep_files


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


def normalize_path(path: str) -> str:
    return path.lstrip('./')


def compute_coverage(gold_files: list[str], answer: str) -> float:
    if not gold_files:
        return 1.0
    cited = extract_cited_files(answer)
    cited_normalized = {normalize_path(f) for f in cited}
    gold_normalized = {normalize_path(f) for f in gold_files}
    hit = len(gold_normalized & cited_normalized)
    return hit / len(gold_normalized)


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


def select_best_entry(question: str, candidates: list[str], llm_client, use_llm: bool = True) -> str:
    """从 embedding 返回的候选中选择最可能作为调查入口的文件。"""
    if not candidates:
        return ""
    if len(candidates) == 1:
        return candidates[0]

    # First, apply hard rules to filter and rank candidates
    def entry_score(path: str) -> int:
        p = path.lower()
        score = 0
        # Exclude tests and examples
        if "/tests/" in p or p.startswith("tests/"):
            score -= 1000
        if "/examples/" in p or p.startswith("examples/"):
            score -= 1000
        # Prefer implementation files
        if p.endswith(".cpp"):
            score += 100
        if p.endswith(".hpp") or p.endswith(".h"):
            score += 50
        # Prefer common / src / ggml-src paths
        if "/common/" in p or p.startswith("common/"):
            score += 30
        if "/src/" in p or p.startswith("src/"):
            score += 30
        if "/ggml/src/" in p or p.startswith("ggml/src/"):
            score += 30
        return score

    ranked = sorted(candidates, key=lambda c: (-entry_score(c), c))
    filtered = [c for c in ranked if entry_score(c) > -500]

    if not use_llm or len(filtered) <= 1:
        return filtered[0] if filtered else candidates[0]

    prompt_template = load_prompt("select_best_entry")
    prompt = prompt_template.format(
        question=question,
        candidates="\n".join(f"- {c}" for c in filtered[:5]),
    )
    result = llm_client.call(prompt, max_tokens=200).strip()
    for c in filtered:
        if c in result:
            return c
    return filtered[0]


def run_item(item: dict, retriever: EmbeddingRetriever, entry_top_k: int, evidence_top_n: int) -> dict:
    # Layer 1: Question → Entry candidates → LLM selection
    entry_results = retriever.retrieve(item["question"], top_k=entry_top_k)
    entry_candidates = [r.metadata.get("file_path", "") for r in entry_results if r.metadata.get("file_path")]

    inv = DynamicInvestigator(max_steps=1)
    entry_file = select_best_entry(item["question"], entry_candidates, inv.llm)

    entry_hit = entry_file in item["gold_files"]

    if not entry_file:
        return {
            "qa_id": item["qa_id"],
            "question": item["question"],
            "gold_files": item["gold_files"],
            "entry_file": "",
            "entry_hit": False,
            "visited_files": [],
            "coverage": 0.0,
            "answer": "",
        }

    # Layer 2: Entry → Evidence (Symbol Fast Path)
    inv = DynamicInvestigator(max_steps=1)
    inv.state = InvestigationState(
        question=item["question"],
        entry_file=entry_file,
    )

    try:
        entry_content = inv.read_file(entry_file)
    except Exception as e:
        return {
            "qa_id": item["qa_id"],
            "question": item["question"],
            "gold_files": item["gold_files"],
            "entry_file": entry_file,
            "entry_hit": entry_hit,
            "visited_files": [entry_file],
            "coverage": 0.0,
            "answer": f"[读取入口文件失败: {e}]",
        }

    suspicion, _ = inv._init_suspicion(item["question"], entry_file, entry_content)
    symbols = suspicion.suspicious_symbols
    selected_symbol = symbols[0] if symbols else ""

    visited = [entry_file]
    contents = [f"=== {entry_file} ===\n{entry_content}"]

    if selected_symbol:
        files = grep_files(selected_symbol, inv.repo_path, limit=100)
        files = [inv._normalize_file_path(f) for f in files]
        files = sorted(set(files), key=lambda f: (-file_priority(f), f))

        for f in files[:evidence_top_n]:
            if f in visited:
                continue
            try:
                content = inv.read_file(f)
                visited.append(f)
                contents.append(f"=== {f} ===\n{content}")
            except Exception:
                pass

    combined = "\n\n".join(contents)
    inv.state.evidence_log.append({
        "file_path": "combined_input",
        "key_facts": ["end-to-end pipeline combined content"],
        "new_hypothesis": "",
        "suspicious_symbols": [selected_symbol] if selected_symbol else [],
        "suspicious_files": visited,
    })
    for fp in visited:
        if fp not in inv.state.files_content:
            inv.state.files_content[fp] = combined
    answer = inv.generate_answer()
    coverage = compute_coverage(item["gold_files"], answer)

    return {
        "qa_id": item["qa_id"],
        "question": item["question"],
        "gold_files": item["gold_files"],
        "entry_file": entry_file,
        "entry_hit": entry_hit,
        "selected_symbol": selected_symbol,
        "visited_files": visited,
        "num_visited": len(visited),
        "coverage": coverage,
        "answer": answer,
    }


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--range", default="0,15")
    parser.add_argument("--entry-top-k", type=int, default=1, help="embedding 返回的入口文件数")
    parser.add_argument("--evidence-top-n", type=int, default=5, help="symbol 扩展读取的文件数")
    parser.add_argument("-w", "--workers", type=int, default=2)
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args()

    items = load_items(args.benchmark, args.range)
    print(f"加载 {len(items)} 题，entry_top_k={args.entry_top_k}, evidence_top_n={args.evidence_top_n}, workers={args.workers}")

    retriever = EmbeddingRetriever()
    results = []
    completed = 0

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(run_item, item, retriever, args.entry_top_k, args.evidence_top_n): item
            for item in items
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            completed += 1
            print(f"  [{completed}/{len(items)}] {result['qa_id']}: "
                  f"entry={result['entry_file']} hit={result['entry_hit']} coverage={result['coverage']*100:.0f}%")

    results.sort(key=lambda x: x["qa_id"])

    avg_cov = sum(r["coverage"] for r in results) / len(results)
    full = sum(1 for r in results if r["coverage"] >= 1.0)
    entry_hit_rate = sum(1 for r in results if r["entry_hit"]) / len(results) * 100

    print(f"\n{'='*60}")
    print("端到端 Pipeline 实验结果")
    print(f"{'='*60}")
    print(f"总题数: {len(results)}")
    print(f"Entry Hit Rate: {entry_hit_rate:.1f}%")
    print(f"引用全: {full}/{len(results)} ({full/len(results)*100:.0f}%)")
    print(f"平均覆盖率: {avg_cov*100:.1f}%")

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
