#!/usr/bin/env python3
"""
实验 2：固定 Symbol + 工具驱动扩展。

流程：
1. 用 LLM 从 entry file 中选出 1 个最关键 symbol
2. 用 grep 找到所有包含该 symbol 的文件
3. 按规则排序（生产代码优先，测试/示例降权）
4. 自动读取前 N 个文件
5. 生成答案

这个实验验证：如果 symbol 选对了，且用结构化工具扩展而不是 LLM 猜文件，coverage 能到多少。

用法:
    python experiments/run_symbol_fastpath.py \
        --benchmark datasets/benchmark_hard.json \
        --range 0,5 \
        --top-n 5 \
        -w 2 \
        -o results/symbol_fastpath_0_5.json
"""
import json
import re
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.qa.investigation import DynamicInvestigator
from src.qa.investigation.base import InvestigationState
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

    if range_str == "easy":
        start, end = 0, min(50, len(items))
    elif range_str == "hard":
        start, end = min(50, len(items)), len(items)
    elif "," in range_str:
        start, end = map(int, range_str.split(","))
    else:
        start, end = 0, len(items)

    selected = []
    for idx in range(start, end):
        item = items[idx]
        definitions = [ev["file"] for ev in item.get("gold_evidence", []) if ev.get("type") == "definition"]
        if definitions:
            entry_file = definitions[0]
        elif item.get("gold_evidence"):
            entry_file = item["gold_evidence"][0]["file"]
        else:
            entry_file = ""

        gold_files = sorted(set(
            ev["file"] for ev in item.get("gold_evidence", [])
            if not ev["file"].endswith((".h", ".hpp"))
        ))

        selected.append({
            "qa_id": item.get("qa_id", f"q{idx}"),
            "question": item.get("question", ""),
            "gold_files": gold_files,
            "entry_file": entry_file,
        })

    return selected


def file_priority(path: str) -> int:
    """文件优先级：生产代码高，测试/示例低。"""
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


def run_item(item: dict, top_n: int) -> dict:
    inv = DynamicInvestigator(max_steps=1)
    inv.state = InvestigationState(
        question=item["question"],
        entry_file=item["entry_file"],
    )

    # 1. Read entry file
    entry_content = inv.read_file(item["entry_file"])

    # 2. Use init_suspicion to get top symbols
    suspicion, _ = inv._init_suspicion(item["question"], item["entry_file"], entry_content)
    symbols = suspicion.suspicious_symbols
    if not symbols:
        return {
            "qa_id": item["qa_id"],
            "coverage": 0.0,
            "selected_symbol": "",
            "visited_files": [item["entry_file"]],
            "answer": "",
        }

    selected_symbol = symbols[0]

    # 3. Grep symbol comprehensively
    files = grep_files(selected_symbol, inv.repo_path, limit=100)
    files = [inv._normalize_file_path(f) for f in files]

    # 4. Sort by priority and deduplicate
    files = sorted(set(files), key=lambda f: (-file_priority(f), f))

    # 5. Read top N files + entry file
    visited = [item["entry_file"]]
    contents = [f"=== {item['entry_file']} ===\n{entry_content}"]

    for f in files[:top_n]:
        if f in visited:
            continue
        try:
            content = inv.read_file(f)
            visited.append(f)
            contents.append(f"=== {f} ===\n{content}")
        except Exception:
            pass

    # 6. Generate answer
    combined = "\n\n".join(contents)
    # Feed combined content into state so generate_answer() can use it
    inv.state.evidence_log.append({
        "file_path": "combined_input",
        "key_facts": ["symbol fast path combined content"],
        "new_hypothesis": "",
        "suspicious_symbols": [selected_symbol],
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
        "entry_file": item["entry_file"],
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
    parser.add_argument("--range", default="0,5")
    parser.add_argument("--top-n", type=int, default=5, help="读取 symbol 相关文件的前 N 个")
    parser.add_argument("-w", "--workers", type=int, default=2)
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args()

    items = load_items(args.benchmark, args.range)
    print(f"加载 {len(items)} 题，top_n={args.top_n}, workers={args.workers}")

    results = []
    completed = 0

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(run_item, item, args.top_n): item for item in items}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            completed += 1
            print(f"  [{completed}/{len(items)}] {result['qa_id']}: "
                  f"symbol={result['selected_symbol']} coverage={result['coverage']*100:.0f}%")

    results.sort(key=lambda x: x["qa_id"])

    avg_cov = sum(r["coverage"] for r in results) / len(results)
    full = sum(1 for r in results if r["coverage"] >= 1.0)

    print(f"\n{'='*60}")
    print("Symbol Fast Path 实验结果")
    print(f"{'='*60}")
    print(f"总题数: {len(results)}")
    print(f"引用全: {full}/{len(results)} ({full/len(results)*100:.0f}%)")
    print(f"平均覆盖率: {avg_cov*100:.1f}%")

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
