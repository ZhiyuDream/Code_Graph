#!/usr/bin/env python3
"""统一评估脚本：binary judge + LLM citation judge。

支持两种输入模式：
1. 旧格式（单文件）：--input results/v2.json
2. 新格式（分离）：--result results/benchmark.json --benchmark datasets/bench.json --range easy|hard

支持两种 judge provider：
- openai (默认)
- deepseek

用法示例：
    # OpenAI judge
    python evals/eval_v2.py --result results/qa.json --benchmark datasets/benchmark_hard.json \
        --range all -o results/eval.json -w 20

    # DeepSeek judge
    python evals/eval_v2.py --result results/qa.json --benchmark datasets/benchmark_hard.json \
        --range all --provider deepseek -o results/eval_deepseek.json -w 10
"""
import json
import sys
import os
import re
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from config import OPENAI_API_KEY, OPENAI_BASE_URL, DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL
from openai import OpenAI


def load_prompt(name: str) -> str:
    """从 prompts/ 目录加载 prompt 模板。"""
    path = _ROOT / "prompts" / f"{name}.txt"
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# 延迟初始化 client，支持 provider 选择
_client = None
_provider = "openai"
_model = "gpt-4.1-mini"


def init_client(provider: str, model: str | None = None):
    """初始化 judge client。"""
    global _client, _provider, _model
    _provider = provider

    if provider == "deepseek":
        _client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL or None)
        _model = model or "deepseek-v4-pro"
    else:
        _client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)
        _model = model or os.environ.get("JUDGE_MODEL", "gpt-4.1-mini")


def call_judge(prompt: str, json_mode: bool = False, max_tokens: int = 800) -> str:
    """调用 judge LLM。"""
    kwargs = {
        "model": _model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": max_tokens,
    }
    if json_mode and _provider == "openai":
        kwargs["response_format"] = {"type": "json_object"}

    resp = _client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content.strip()


# ── Core functions ──────────────────────────────────────────────────

BINARY_JUDGE_PROMPT = load_prompt("binary_judge")
CITATION_JUDGE_PROMPT = load_prompt("citation_judge")


def llm_binary_judge(question: str, reference: str, generated: str) -> tuple[bool, str]:
    prompt = BINARY_JUDGE_PROMPT.format(
        question=question[:500],
        reference=reference[:800],
        generated=generated[:1500]
    )
    try:
        text = call_judge(prompt, json_mode=False, max_tokens=200)
        first_line = text.split('\n')[0].upper()
        is_correct = "CORRECT" in first_line and "INCORRECT" not in first_line
        return is_correct, text
    except Exception as e:
        return False, f"评估错误: {e}"


def llm_citation_judge(question: str, reference: str, generated: str, gold_files: list[str]) -> dict:
    if not gold_files:
        return {
            "coverage_ratio": 1.0,
            "cited_files": [],
            "missing_files": [],
            "missing_reasons": {},
            "notes": "无 .cpp/.c gold 文件",
        }

    gold_text = "\n".join(f"- {f}" for f in gold_files)
    prompt = CITATION_JUDGE_PROMPT.format(
        question=question,
        gold_files=gold_text,
        reference_answer=reference[:2000],
        generated_answer=generated,
    )
    try:
        text = call_judge(prompt, json_mode=True, max_tokens=800)
        result = json.loads(text)
        return {
            "coverage_ratio": float(result.get("coverage_ratio", 0)),
            "cited_files": result.get("cited_files", []),
            "missing_files": result.get("missing_files", []),
            "missing_reasons": result.get("missing_reasons", {}),
            "notes": result.get("notes", ""),
        }
    except Exception as e:
        return {
            "coverage_ratio": 0,
            "cited_files": [],
            "missing_files": gold_files,
            "missing_reasons": {},
            "notes": f"评估错误: {e}",
        }


# ── Data loading ────────────────────────────────────────────────────

def load_split_format(result_path: Path, bench_path: Path, range_str: str) -> list[dict]:
    """加载分离格式的结果 + benchmark 数据。"""
    with open(result_path, "r", encoding="utf-8") as f:
        results = json.load(f)
    with open(bench_path, "r", encoding="utf-8") as f:
        bench = json.load(f)

    if isinstance(bench, dict) and "items" in bench:
        bench_items = bench["items"]
    elif isinstance(bench, list):
        bench_items = bench
    else:
        raise ValueError("Unknown benchmark format")

    if range_str == "easy":
        start, end = 0, min(50, len(bench_items))
    elif range_str == "hard":
        start, end = min(50, len(bench_items)), len(bench_items)
    else:
        start, end = 0, len(bench_items)

    items = []
    for idx in range(start, end):
        bench_item = bench_items[idx]
        result_idx = idx - start if len(results) == (end - start) else idx
        if result_idx >= len(results):
            continue
        result = results[result_idx]

        # Deduplicate to file level, exclude .h/.hpp
        gold_files = sorted(set(
            ev["file"] for ev in bench_item.get("gold_evidence", [])
            if not ev["file"].endswith((".h", ".hpp"))
        ))

        items.append({
            "qa_id": bench_item.get("qa_id", f"q{idx}"),
            "question": bench_item.get("question", ""),
            "reference": bench_item.get("reference_answer", ""),
            "generated": result.get("answer", ""),
            "gold_files": gold_files,
            "category": bench_item.get("category", {}).get("level_2", "unknown")
                if isinstance(bench_item.get("category"), dict) else "unknown",
            "retrieved_functions": result.get("retrieved_functions", []),
        })
    return items


def load_single_format(input_path: Path) -> list[dict]:
    """加载旧格式单文件数据。"""
    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    items = []
    for item in data:
        # Try to extract gold files from evidence text
        evidence = item.get("evidence", "")
        gold_files = []
        for m in re.finditer(r'`([^`]+:\d+)`', evidence):
            fp = m.group(1).rsplit(':', 1)[0]
            if not fp.endswith((".h", ".hpp")):
                gold_files.append(fp)

        items.append({
            "qa_id": item.get("id", item.get("qa_id", "")),
            "question": item.get("question", ""),
            "reference": item.get("reference", item.get("reference_answer", "")),
            "generated": item.get("generated", item.get("answer", "")),
            "gold_files": gold_files,
            "category": item.get("dimension_2", "unknown"),
            "_raw": item,
        })
    return items


# ── Main ────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="统一评估：binary judge + LLM citation judge")
    parser.add_argument("--input", type=Path, help="旧格式单文件输入")
    parser.add_argument("--result", type=Path, help="结果 JSON 文件（分离格式）")
    parser.add_argument("--benchmark", type=Path, help="Benchmark 数据集（分离格式）")
    parser.add_argument("--range", choices=["easy", "hard", "all"], default="easy")
    parser.add_argument("--mode", choices=["binary", "citation", "all"], default="all",
                        help="评估模式: binary=仅二元判断, citation=仅引用覆盖, all=两者")
    parser.add_argument("--provider", choices=["openai", "deepseek"], default="openai",
                        help="Judge LLM provider")
    parser.add_argument("--model", type=str, default=None,
                        help="Judge model name (default: gpt-4.1-mini for openai, deepseek-v4-pro for deepseek)")
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("-w", "--workers", type=int, default=20)
    args = parser.parse_args()

    init_client(args.provider, args.model)

    # Load data
    if args.input:
        items = load_single_format(args.input)
    elif args.result and args.benchmark:
        items = load_split_format(args.result, args.benchmark, args.range)
    else:
        parser.error("请提供 --input 或 (--result + --benchmark)")

    print(f"加载 {len(items)} 题，provider: {args.provider}, model: {_model}, workers: {args.workers}, mode: {args.mode}")

    # Run evaluation
    completed = 0

    def eval_one(item: dict) -> dict:
        nonlocal completed
        # Binary judge
        if args.mode in ("binary", "all"):
            is_correct, reason = llm_binary_judge(
                item["question"], item["reference"], item["generated"]
            )
            item["eval_binary_correct"] = is_correct
            item["eval_binary_reason"] = reason

        # Citation judge
        if args.mode in ("citation", "all"):
            cit = llm_citation_judge(
                item["question"], item["reference"], item["generated"], item.get("gold_files", [])
            )
            item["eval_citation"] = cit

        completed += 1
        if completed % 5 == 0:
            print(f"  [{completed}/{len(items)}] 完成")
        return item

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(eval_one, item) for item in items]
        for future in as_completed(futures):
            results.append(future.result())

    results.sort(key=lambda x: x.get("qa_id", ""))

    # Summary
    print(f"\n{'='*60}")
    print("评估结果汇总")
    print(f"{'='*60}")

    if args.mode in ("binary", "all"):
        correct = sum(1 for r in results if r.get("eval_binary_correct"))
        print(f"Binary Judge: {correct}/{len(results)} = {correct/len(results)*100:.1f}%")

    if args.mode in ("citation", "all"):
        full = sum(1 for r in results if r.get("eval_citation", {}).get("coverage_ratio", 0) >= 1.0)
        partial = sum(1 for r in results if 0 < r.get("eval_citation", {}).get("coverage_ratio", 0) < 1.0)
        zero = sum(1 for r in results if r.get("eval_citation", {}).get("coverage_ratio", 0) == 0)
        avg_cov = sum(r.get("eval_citation", {}).get("coverage_ratio", 0) for r in results) / len(results)
        print(f"Citation Coverage: 全={full}, 部分={partial}, 零={zero}, 平均={avg_cov*100:.1f}%")

    # Per-question detail
    print(f"\n{'='*60}")
    print("逐题详情")
    print(f"{'='*60}")
    for r in results:
        qid = r.get("qa_id", "")
        parts = [qid]
        if "eval_binary_correct" in r:
            parts.append("✓" if r["eval_binary_correct"] else "✗")
        if "eval_citation" in r:
            cit = r["eval_citation"]
            cov = cit.get("coverage_ratio", 0)
            status = "全" if cov >= 1.0 else ("部分" if cov > 0 else "零")
            missing = ", ".join(cit.get("missing_files", []))
            parts.append(f"{cov*100:>3.0f}%[{status}]")
            if missing:
                parts.append(f"缺失:{missing}")
        print(" | ".join(parts))

    # Save
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
