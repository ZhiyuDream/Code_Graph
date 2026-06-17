#!/usr/bin/env python3
"""
调查轨迹实验（Oracle-Entry Gap）。

目标：
1. 在 Entry-only 条件下，记录 Dynamic Investigator 的完整调查轨迹。
2. 在 Gold-files 条件下，直接给出答案作为上限。
3. 比较两种条件的 Coverage，分析差距来源。
4. 产出 5-10 条高质量 trajectory 用于 case study。

用法:
    # Entry-only 模式，5 题
    python experiments/run_investigation_trajectory.py \
        --benchmark datasets/benchmark_hard.json \
        --range 0,5 \
        --condition entry \
        -w 2 \
        -o results/trajectory_entry_0_5.json

    # Gold-files 模式，5 题
    python experiments/run_investigation_trajectory.py \
        --benchmark datasets/benchmark_hard.json \
        --range 0,5 \
        --condition gold \
        -w 2 \
        -o results/trajectory_gold_0_5.json
"""
import json
import re
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.qa.investigation import DynamicInvestigator
from src.qa.investigation.symbol_follow import SymbolFollowInvestigator


def extract_cited_files(answer: str) -> set[str]:
    """从固定格式引用清单中提取文件路径。"""
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
    """路径规范化：去掉 ./ 前缀。"""
    return path.lstrip('./')


def compute_coverage(gold_files: list[str], answer: str) -> float:
    """计算引用覆盖率。"""
    if not gold_files:
        return 1.0
    cited = extract_cited_files(answer)
    cited_normalized = {normalize_path(f) for f in cited}
    gold_normalized = {normalize_path(f) for f in gold_files}
    hit = len(gold_normalized & cited_normalized)
    return hit / len(gold_normalized)


def load_items(bench_path: Path, range_str: str) -> list[dict]:
    """加载 benchmark 题目。"""
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


def run_entry_condition(item: dict, max_steps: int) -> dict:
    """Entry-only 条件：Agent 从入口开始调查。"""
    investigator = DynamicInvestigator(max_steps=max_steps)
    result = investigator.run(item["question"], item["entry_file"])
    coverage = compute_coverage(item["gold_files"], result["answer"])

    return {
        "qa_id": item["qa_id"],
        "condition": "entry",
        "question": item["question"],
        "gold_files": item["gold_files"],
        "entry_file": item["entry_file"],
        "visited_files": result["visited_files"],
        "num_visited": len(result["visited_files"]),
        "num_steps": len(result["trajectory"]),
        "coverage": coverage,
        "answer": result["answer"],
        "trajectory": result["trajectory"],
        "frontier_remaining": result["frontier_files"],
    }


def run_gold_condition(item: dict) -> dict:
    """Gold-files 条件：直接读所有 gold files 生成答案，作为上限。"""
    from src.qa.investigation.base import InvestigationState

    investigator = DynamicInvestigator(max_steps=1)
    investigator.state = InvestigationState(
        question=item["question"],
        entry_file=item["entry_file"],
    )

    for f in item["gold_files"]:
        try:
            content = investigator.read_file(f)
            investigator.state.visited_files.append(f)
            investigator.state.evidence_log.append({
                "file_path": f,
                "key_facts": ["gold evidence file"],
                "new_hypothesis": "",
                "suspicious_symbols": [],
                "suspicious_files": [],
            })
        except Exception as e:
            investigator.state.visited_files.append(f)
            investigator.state.files_content[f] = f"[读取失败: {e}]"

    answer = investigator.generate_answer()
    coverage = compute_coverage(item["gold_files"], answer)

    return {
        "qa_id": item["qa_id"],
        "condition": "gold",
        "question": item["question"],
        "gold_files": item["gold_files"],
        "entry_file": item["entry_file"],
        "visited_files": investigator.state.visited_files,
        "num_visited": len(investigator.state.visited_files),
        "num_steps": 1,
        "coverage": coverage,
        "answer": answer,
        "trajectory": [],
        "frontier_remaining": [],
    }


def run_symbol_follow_condition(item: dict, max_steps: int) -> dict:
    """Symbol-Follow 条件：严格按符号追调用链。"""
    investigator = SymbolFollowInvestigator(max_steps=max_steps)
    result = investigator.run(item["question"], item["entry_file"])
    coverage = compute_coverage(item["gold_files"], result["answer"])

    return {
        "qa_id": item["qa_id"],
        "condition": "symbol_follow",
        "question": item["question"],
        "gold_files": item["gold_files"],
        "entry_file": item["entry_file"],
        "visited_files": result["visited_files"],
        "num_visited": len(result["visited_files"]),
        "num_steps": len(result["trajectory"]),
        "coverage": coverage,
        "answer": result["answer"],
        "trajectory": result["trajectory"],
        "frontier_remaining": [],
    }


def run_item(item: dict, condition: str, max_steps: int) -> dict:
    if condition == "gold":
        return run_gold_condition(item)
    if condition == "symbol_follow":
        return run_symbol_follow_condition(item, max_steps)
    return run_entry_condition(item, max_steps)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--range", default="0,5", help="start,end 或 easy|hard|all")
    parser.add_argument("--condition", choices=["entry", "gold", "symbol_follow"], required=True)
    parser.add_argument("--max-steps", type=int, default=5)
    parser.add_argument("-w", "--workers", type=int, default=2)
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args()

    items = load_items(args.benchmark, args.range)
    print(f"加载 {len(items)} 题，condition={args.condition}, max_steps={args.max_steps}, workers={args.workers}")

    results = []
    completed = 0

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(run_item, item, args.condition, args.max_steps): item
            for item in items
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            completed += 1
            print(f"  [{completed}/{len(items)}] {result['qa_id']}: "
                  f"coverage={result['coverage']*100:.0f}%({result['num_visited']}files)")

    results.sort(key=lambda x: x["qa_id"])

    avg_cov = sum(r["coverage"] for r in results) / len(results)
    full = sum(1 for r in results if r["coverage"] >= 1.0)

    print(f"\n{'='*60}")
    print("实验结果汇总")
    print(f"{'='*60}")
    print(f"总题数: {len(results)}")
    print(f"条件: {args.condition}")
    print(f"引用全: {full}/{len(results)} ({full/len(results)*100:.0f}%)")
    print(f"平均覆盖率: {avg_cov*100:.1f}%")

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
