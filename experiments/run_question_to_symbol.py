#!/usr/bin/env python3
"""
Question → Symbol 直接评测。

不给 Agent 任何入口文件，只根据 Question 让 LLM 提取可能相关的 symbol。
统计这些 symbol 是否出现在 gold files 中。

验证：从纯自然语言问题中直接提取符号有多难。

用法:
    python experiments/run_question_to_symbol.py \
        --benchmark datasets/benchmark_hard.json \
        --range 0,15 \
        -w 2 \
        -o results/question_to_symbol_0_15.json
"""
import json
import re
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.qa.investigation.base import LLMClient, load_prompt


def extract_symbols_from_question(question: str, llm: LLMClient) -> list[str]:
    """让 LLM 只根据问题提取可能相关的函数名/类名/全局变量名。"""
    prompt = load_prompt("extract_symbols_from_question").format(question=question)
    text = llm.call(prompt, max_tokens=500).strip()
    symbols = [line.strip() for line in text.split('\n') if line.strip()]
    # Clean up markdown and numbering
    cleaned = []
    for s in symbols:
        s = re.sub(r'^[-\d\.\*\+]+\s*', '', s).strip()
        s = s.strip('`')
        if s and len(s) >= 2:
            cleaned.append(s)
    return cleaned


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
        gold_files = sorted(set(ev["file"] for ev in item.get("gold_evidence", [])))
        selected.append({
            "qa_id": item.get("qa_id", f"q{idx}"),
            "question": item.get("question", ""),
            "gold_files": gold_files,
        })
    return selected


def run_item(item: dict, llm: LLMClient) -> dict:
    symbols = extract_symbols_from_question(item["question"], llm)

    # Load gold file contents
    from src.search.code_reader import read_full_file
    gold_content = ""
    for f in item["gold_files"]:
        try:
            gold_content += "\n" + read_full_file(f)
        except Exception:
            pass

    hit_symbols = [s for s in symbols if s in gold_content]

    return {
        "qa_id": item["qa_id"],
        "question": item["question"],
        "gold_files": item["gold_files"],
        "predicted_symbols": symbols,
        "hit_symbols": hit_symbols,
        "num_predicted": len(symbols),
        "num_hits": len(hit_symbols),
        "hit_rate": len(hit_symbols) / len(symbols) if symbols else 0,
    }


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--range", default="0,15")
    parser.add_argument("-w", "--workers", type=int, default=2)
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args()

    items = load_items(args.benchmark, args.range)
    print(f"加载 {len(items)} 题，workers={args.workers}")

    llm = LLMClient()
    results = []
    completed = 0

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(run_item, item, llm): item for item in items}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            completed += 1
            print(f"  [{completed}/{len(items)}] {result['qa_id']}: "
                  f"predicted={result['num_predicted']} hits={result['num_hits']} rate={result['hit_rate']*100:.0f}%")

    results.sort(key=lambda x: x["qa_id"])

    total_pred = sum(r["num_predicted"] for r in results)
    total_hits = sum(r["num_hits"] for r in results)
    avg_rate = total_hits / total_pred * 100 if total_pred else 0
    at_least_one_hit = sum(1 for r in results if r["num_hits"] > 0) / len(results) * 100

    print(f"\n{'='*60}")
    print("Question → Symbol 直接评测结果")
    print(f"{'='*60}")
    print(f"总题数: {len(results)}")
    print(f"总预测 symbol 数: {total_pred}")
    print(f"命中 gold file 的 symbol 数: {total_hits}")
    print(f"Symbol 命中率: {avg_rate:.1f}%")
    print(f"至少命中一个 symbol 的题目比例: {at_least_one_hit:.1f}%")

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
