#!/usr/bin/env python3
"""
Static vs Dynamic Investigation 对比实验。

核心问题：在相同入口文件下，允许 Agent 根据新证据动态改变调查路径，
是否能找到更多最终证据？

用法:
    # 小批量测试（5 题）
    python experiments/run_static_dynamic.py \
        --benchmark datasets/benchmark_hard.json \
        --range 0,5 \
        -o results/static_dynamic_0_5.json \
        -w 2

    # 全量并行（50 题）
    python experiments/run_static_dynamic.py \
        --benchmark datasets/benchmark_hard.json \
        --range 0,50 \
        -o results/static_dynamic_full.json \
        -w 10
"""
import json
import re
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.qa.investigation import StaticInvestigator, DynamicInvestigator


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
        # Entry file: prefer definition evidence, otherwise first gold file.
        definitions = [ev["file"] for ev in item.get("gold_evidence", []) if ev.get("type") == "definition"]
        if definitions:
            entry_file = definitions[0]
        elif item.get("gold_evidence"):
            entry_file = item["gold_evidence"][0]["file"]
        else:
            entry_file = ""

        # Gold files for coverage: exclude header files (only implementation/call-site files matter)
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


def run_single_mode(item: dict, mode: str, max_steps: int) -> dict:
    """用指定模式跑一道题。"""
    if mode == "static":
        investigator = StaticInvestigator(max_steps=max_steps)
    else:
        investigator = DynamicInvestigator(max_steps=max_steps)

    result = investigator.run(item["question"], item["entry_file"])
    coverage = compute_coverage(item["gold_files"], result["answer"])

    return {
        "qa_id": item["qa_id"],
        "mode": mode,
        "question": item["question"],
        "gold_files": item["gold_files"],
        "entry_file": item["entry_file"],
        "visited_files": result["visited_files"],
        "num_visited": len(result["visited_files"]),
        "num_steps": len(result["steps"]),
        "coverage": coverage,
        "answer": result["answer"],
        "steps": result["steps"],
        "frontier_remaining": result["frontier_files"],
    }


def run_item_both_modes(item: dict, max_steps: int) -> dict:
    """对单道题跑 Static 和 Dynamic 两种模式。"""
    static_result = run_single_mode(item, "static", max_steps)
    dynamic_result = run_single_mode(item, "dynamic", max_steps)

    return {
        "qa_id": item["qa_id"],
        "question": item["question"],
        "gold_files": item["gold_files"],
        "entry_file": item["entry_file"],
        "static": static_result,
        "dynamic": dynamic_result,
    }


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--range", default="0,5", help="start,end 或 easy|hard|all")
    parser.add_argument("--max-steps", type=int, default=10)
    parser.add_argument("-w", "--workers", type=int, default=2)
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args()

    items = load_items(args.benchmark, args.range)
    print(f"加载 {len(items)} 题，max_steps={args.max_steps}, workers={args.workers}")
    print(f"模式: Static vs Dynamic")

    results = []
    completed = 0

    # Each worker runs one full item (both static + dynamic)
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(run_item_both_modes, item, args.max_steps): item
            for item in items
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            completed += 1
            static_cov = result["static"]["coverage"] * 100
            dynamic_cov = result["dynamic"]["coverage"] * 100
            print(f"  [{completed}/{len(items)}] {result['qa_id']}: "
                  f"Static={static_cov:.0f}%({result['static']['num_visited']}files) "
                  f"Dynamic={dynamic_cov:.0f}%({result['dynamic']['num_visited']}files)")

    results.sort(key=lambda x: x["qa_id"])

    # Summary
    static_full = sum(1 for r in results if r["static"]["coverage"] >= 1.0)
    dynamic_full = sum(1 for r in results if r["dynamic"]["coverage"] >= 1.0)
    static_avg_cov = sum(r["static"]["coverage"] for r in results) / len(results)
    dynamic_avg_cov = sum(r["dynamic"]["coverage"] for r in results) / len(results)
    static_avg_files = sum(r["static"]["num_visited"] for r in results) / len(results)
    dynamic_avg_files = sum(r["dynamic"]["num_visited"] for r in results) / len(results)

    print(f"\n{'='*60}")
    print("实验结果汇总")
    print(f"{'='*60}")
    print(f"总题数: {len(results)}")
    print(f"Static 引用全: {static_full}/{len(results)} ({static_full/len(results)*100:.0f}%)")
    print(f"Dynamic 引用全: {dynamic_full}/{len(results)} ({dynamic_full/len(results)*100:.0f}%)")
    print(f"Static 平均覆盖率: {static_avg_cov*100:.1f}%")
    print(f"Dynamic 平均覆盖率: {dynamic_avg_cov*100:.1f}%")
    print(f"Static 平均访问文件数: {static_avg_files:.1f}")
    print(f"Dynamic 平均访问文件数: {dynamic_avg_files:.1f}")

    # Per-question detail
    print(f"\n{'='*60}")
    print("逐题详情")
    print(f"{'='*60}")
    for r in results:
        s = r["static"]
        d = r["dynamic"]
        winner = "Dynamic" if d["coverage"] > s["coverage"] else ("Static" if s["coverage"] > d["coverage"] else "Tie")
        print(f"{r['qa_id']}: Static={s['coverage']*100:.0f}%({s['num_visited']}f) "
              f"Dynamic={d['coverage']*100:.0f}%({d['num_visited']}f) → {winner}")

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
