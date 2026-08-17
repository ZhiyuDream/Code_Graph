#!/usr/bin/env python3
"""统一评估脚本：binary judge + LLM citation judge。

支持两种输入模式：
1. 旧格式（单文件）：--input results/v2.json
2. 新格式（分离）：--result results/benchmark.json --benchmark datasets/bench.json --range easy|hard

Judge LLM 通过 --model 指定，统一走 src/core/llm_client.py，默认 gpt-4.1-mini。

用法示例：
    # 默认 judge：gpt-4.1-mini
    python evals/eval_v2.py --result results/qa.json --benchmark datasets/benchmark_hard.json \
        --range all -o results/eval.json -w 20

    # 指定 glm-5.2 作为 judge（.env 中 OPENAI_BASE_URL 需指向兼容中转站）
    python evals/eval_v2.py --result results/qa.json --benchmark datasets/benchmark_hard.json \
        --range all --model glm-5.2 -o results/eval_glm52.json -w 20
"""
import json
import sys
import os
import re
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.core.llm_client import call_llm, call_llm_json


def load_prompt(name: str) -> str:
    """从 prompts/ 目录加载 prompt 模板。"""
    path = _ROOT / "prompts" / f"{name}.txt"
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# 当前使用的 judge 模型，由命令行 --model 指定；默认从环境 LLM_MODEL 读取，未设置则回退 gpt-4.1-mini。
_judge_model: str | None = None


def init_judge(model: str | None = None):
    """初始化 judge 模型名称。"""
    global _judge_model
    _judge_model = model


def call_judge(prompt: str, json_mode: bool = False, max_tokens: int = 800) -> str:
    """调用 judge LLM，统一走 call_llm / call_llm_json。"""
    if json_mode:
        result = call_llm_json(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            model=_judge_model,
        )
        if result is None:
            return "{}"
        return json.dumps(result, ensure_ascii=False)
    return call_llm(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        model=_judge_model,
    )


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
        generated_answer=generated,
    )
    try:
        text = call_judge(prompt, json_mode=True, max_tokens=4000)  # reasoning judge 需要更大预算,否则截断成空判
        result = json.loads(text)
        return {
            "coverage_ratio": float(result.get("coverage_ratio", 0)),
            "cited_files": result.get("cited_files", []),
            "missing_files": result.get("missing_files", []),
            "missing_reasons": result.get("missing_reasons", {}),
            "evidence_quotes": result.get("evidence_quotes", {}),
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


def deterministic_citation_judge(generated: str, gold_files: list[str], read_files: set) -> dict:
    """确定性 citation 核对（替代 LLM judge，防幻觉虚报）。

    判定标准（与用户确认）：gold 文件出现在答案文本（含末尾引用清单，
    全路径或 basename）即算"引用"；但前提是该文件在调查中被实际读过
    （read_function/read_lines/系统自动读取），否则视为"编造型引用"不计。

    basename 匹配加路径/命名字符边界：`common.cpp` 不应命中 `server-common.cpp`
    （012 案例：子串误配把没读过的文件算成"引用过"）。
    """
    import os
    import re
    cited, missing, reasons = [], [], {}
    for f in gold_files:
        base = os.path.basename(f)
        in_answer = (f in generated) or bool(
            re.search(r"(?<![A-Za-z0-9_\-.])" + re.escape(base) + r"(?![A-Za-z0-9_])", generated)
        )
        was_read = f in read_files
        if in_answer and was_read:
            cited.append(f)
        else:
            missing.append(f)
            if in_answer and not was_read:
                reasons[f] = "答案引用但从未读过（编造型引用，不计）"
            elif was_read and not in_answer:
                reasons[f] = "读过但答案未引用"
            else:
                reasons[f] = "未读且未引用"
    ratio = len(cited) / len(gold_files) if gold_files else 1.0
    return {
        "coverage_ratio": ratio,
        "cited_files": cited,
        "missing_files": missing,
        "missing_reasons": reasons,
        "notes": "确定性核对（读过+答案引用，含引用清单）",
    }


def extract_read_files(result: dict) -> set:
    """从结果 JSON 提取实际读过的文件集合。

    ReAct 结果：steps 中 read_function/read_lines 的 files_accessed，
    以及 find_callers/search_symbol 中系统自动读取的文件。
    Concept-Symbol 结果：retrieved_functions（这些函数被实际读入上下文）。
    """
    read = set()
    for s in result.get("steps", []):
        if s.get("action") in ("read_function", "read_lines"):
            read.update(s.get("files_accessed", []))
        elif s.get("action") in ("find_callers", "search_symbol") and "[系统自动" in s.get("observation", ""):
            read.update(s.get("files_accessed", []))
    for fid in result.get("retrieved_functions", []):
        if isinstance(fid, str) and "/" in fid:
            read.add(fid.split(":")[0])
    return read


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

    # 按 qa_id 匹配（子集评估时位置对不上，必须按 id）
    results_by_id = {r.get("qa_id", r.get("id", "")): r for r in results}

    items = []
    for idx in range(start, end):
        bench_item = bench_items[idx]
        qa_id = bench_item.get("qa_id", f"q{idx}")
        result = results_by_id.get(qa_id)
        if result is None:
            continue

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
            "_read_files": sorted(extract_read_files(result)),
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
    parser.add_argument("--citation-mode", choices=["llm", "det", "both"], default="llm",
                        help="citation 判定方式: llm=LLM judge(可能幻觉虚报), det=确定性核对(读过+答案引用), both=两者都算")
    parser.add_argument("--model", type=str, default=None,
                        help="Judge model name (默认: 优先 LLM_MODEL 环境变量，否则 gpt-4.1-mini)")
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("-w", "--workers", type=int, default=20)
    args = parser.parse_args()

    judge_model = args.model or os.environ.get("LLM_MODEL") or "gpt-4.1-mini"
    init_judge(judge_model)

    # Load data
    if args.input:
        items = load_single_format(args.input)
    elif args.result and args.benchmark:
        items = load_split_format(args.result, args.benchmark, args.range)
    else:
        parser.error("请提供 --input 或 (--result + --benchmark)")

    print(f"加载 {len(items)} 题，judge model: {_judge_model}, workers: {args.workers}, mode: {args.mode}")

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
            if args.citation_mode in ("llm", "both"):
                cit = llm_citation_judge(
                    item["question"], item["reference"], item["generated"], item.get("gold_files", [])
                )
                item["eval_citation"] = cit
            if args.citation_mode in ("det", "both"):
                det = deterministic_citation_judge(
                    item["generated"], item.get("gold_files", []), set(item.get("_read_files", []))
                )
                if args.citation_mode == "both":
                    item["eval_citation_det"] = det
                else:
                    item["eval_citation"] = det

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
