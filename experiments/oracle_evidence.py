#!/usr/bin/env python3
"""
Oracle Evidence 实验 — 直接给 LLM Gold 文件，验证"给对文件能不能答对"。

用法:
    python experiments/oracle_evidence.py \
        --benchmark datasets/benchmark_hard.json \
        --range 0,5 \
        -o results/oracle_hard_0_5.json

流程:
1. 读取 benchmark 中每道题的 gold evidence
2. 提取 .cpp/.c 文件，按 (file, line_start, line_end) 读取源码
3. 构造 prompt: Question + Gold 代码片段 → DeepSeek v4-pro 生成答案
4. 用 gpt-4.1-mini 评估引用覆盖率
"""
import json
import re
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, OPENAI_API_KEY, OPENAI_BASE_URL
from openai import OpenAI

from src.search.code_reader import read_file_lines

# Clients
oracle_client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url=DEEPSEEK_BASE_URL or "https://api.deepseek.com/v1",
)
eval_client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)

ORACLE_MODEL = "deepseek-v4-pro"
EVAL_MODEL = "gpt-4.1-mini"


ORACLE_PROMPT = """你是一位代码审计专家。请基于以下代码证据，回答用户的问题。

【问题】
{question}

【代码证据】
{evidence}

---

要求：
1. 直接回答问题
2. 在正文中引用代码证据时，使用格式：`file.cpp:start-end`
3. **答案末尾必须包含一个固定格式的"引用文件清单"章节，如下**：

## 引用文件清单
- `file1.cpp`
- `file2.cpp`
- `file3.cpp`

4. 清单中列出所有你实际作为证据使用的 .cpp/.c 文件路径
5. 如果不确定，明确说明"无法确认"
"""

CITATION_EVAL_PROMPT = """评估生成答案是否覆盖了给定的 gold evidence 文件。

【评估规则】
1. 只看 .cpp / .c 文件，忽略 .h / .hpp
2. 答案末尾有"引用文件清单"，请优先根据清单判断
3. 如果清单中的文件路径在 gold 文件列表中，就算覆盖
4. coverage_ratio = 清单中与 gold 重叠的 unique 文件数 / gold unique 文件数

【原始问题】
{question}

【Gold 文件】
{gold_files}

【生成答案】
{generated_answer}

---

返回 JSON：
{{
  "coverage_ratio": 0.0,
  "cited_files": ["file1.cpp"],
  "missing_files": ["file2.cpp"],
  "notes": "简短说明"
}}
"""


def read_gold_evidence(gold_evidence: list) -> tuple[str, list[str]]:
    """
    读取 gold evidence 中的代码片段。
    返回 (evidence_text, unique_cpp_files)。
    """
    cpp_entries = []
    for ev in gold_evidence:
        f = ev["file"]
        if f.endswith((".h", ".hpp")):
            continue
        cpp_entries.append(ev)

    # Deduplicate to unique file level for citation coverage evaluation
    # But read each (file, range) snippet for evidence context
    seen_files = set()
    unique_files = []
    parts = []
    for ev in cpp_entries:
        f = ev["file"]
        start = ev.get("line_start", 1)
        end = ev.get("line_end", start + 50)
        content = read_file_lines(f, start, end)

        parts.append(f"=== {f}:{start}-{end} ===\n{content}\n")
        if f not in seen_files:
            seen_files.add(f)
            unique_files.append(f)

    return "\n".join(parts), unique_files


def extract_cited_files_from_answer(answer: str) -> list[str]:
    """从答案末尾的固定格式'引用文件清单'中提取文件路径。"""
    cited = set()
    marker = "## 引用文件清单"
    idx = answer.rfind(marker)
    if idx >= 0:
        list_section = answer[idx + len(marker):]
        for line in list_section.split('\n'):
            line = line.strip()
            if line.startswith('- '):
                content = line[2:].strip()
                # Extract file path from `file.cpp` or `file.cpp:123`
                m = re.search(r'`?([\w/\-\.]+\.(?:cpp|c|h|hpp))(?::\d+)?`?', content)
                if m:
                    cited.add(m.group(1))
    return sorted(cited)


def oracle_answer(question: str, evidence_text: str) -> str:
    """调用 DeepSeek v4-pro 生成答案。"""
    prompt = ORACLE_PROMPT.format(question=question, evidence=evidence_text)
    try:
        resp = oracle_client.chat.completions.create(
            model=ORACLE_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=4000,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        return f"答案生成失败: {e}"


def eval_citation(question: str, gold_files: list[str], answer: str) -> dict:
    """评估引用覆盖率：优先从固定格式清单解析，否则 fallback 到 LLM。"""
    if not gold_files:
        return {"coverage_ratio": 1.0, "cited_files": [], "missing_files": [], "notes": "无 gold 文件"}

    # First try fixed-format list
    cited = extract_cited_files_from_answer(answer)
    if cited:
        gold_set = set(gold_files)
        cited_gold = gold_set & set(cited)
        coverage = len(cited_gold) / len(gold_set)
        missing = sorted(gold_set - set(cited))
        return {
            "coverage_ratio": coverage,
            "cited_files": sorted(cited_gold),
            "missing_files": missing,
            "notes": f"从固定格式清单解析，引用文件: {cited}",
        }

    # Fallback to LLM judge if no fixed-format list found
    prompt = CITATION_EVAL_PROMPT.format(
        question=question,
        gold_files="\n".join(f"- {f}" for f in gold_files),
        generated_answer=answer[:4000],
    )
    try:
        resp = eval_client.chat.completions.create(
            model=EVAL_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=600,
            response_format={"type": "json_object"},
        )
        result = json.loads(resp.choices[0].message.content.strip())
        return {
            "coverage_ratio": float(result.get("coverage_ratio", 0)),
            "cited_files": result.get("cited_files", []),
            "missing_files": result.get("missing_files", []),
            "notes": result.get("notes", ""),
        }
    except Exception as e:
        return {
            "coverage_ratio": 0,
            "cited_files": [],
            "missing_files": gold_files,
            "notes": f"评估错误: {e}",
        }


def run_single(item: dict) -> dict:
    """跑一道题。"""
    evidence_text, gold_files = read_gold_evidence(item.get("gold_evidence", []))

    if not gold_files:
        answer = "(无 .cpp/.c gold 证据)"
        citation = {"coverage_ratio": 1.0, "cited_files": [], "missing_files": [], "notes": "无 gold 文件"}
    else:
        answer = oracle_answer(item["question"], evidence_text)
        citation = eval_citation(item["question"], gold_files, answer)

    return {
        "qa_id": item.get("qa_id", ""),
        "question": item["question"],
        "gold_files": gold_files,
        "evidence_text_length": len(evidence_text),
        "answer": answer,
        "coverage_ratio": citation["coverage_ratio"],
        "cited_files": citation["cited_files"],
        "missing_files": citation["missing_files"],
        "eval_notes": citation["notes"],
    }


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--range", default="0,5", help="start,end 或 easy|hard|all")
    parser.add_argument("-w", "--workers", type=int, default=5)
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args()

    with open(args.benchmark, "r", encoding="utf-8") as f:
        bench = json.load(f)

    if isinstance(bench, dict) and "items" in bench:
        items = bench["items"]
    elif isinstance(bench, list):
        items = bench
    else:
        raise ValueError("Unknown benchmark format")

    # Parse range
    if args.range == "easy":
        start, end = 0, min(50, len(items))
    elif args.range == "hard":
        start, end = min(50, len(items)), len(items)
    elif "," in args.range:
        start, end = map(int, args.range.split(","))
    else:
        start, end = 0, len(items)

    items = items[start:end]
    print(f"Oracle Evidence 实验: {len(items)} 题, model={ORACLE_MODEL}, range={start},{end}")

    results = []
    completed = 0

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(run_single, item): item for item in items}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            completed += 1
            print(f"  [{completed}/{len(items)}] {result['qa_id']}: coverage={result['coverage_ratio']*100:.0f}%")

    results.sort(key=lambda x: x["qa_id"])

    # Summary
    full = sum(1 for r in results if r["coverage_ratio"] >= 1.0)
    partial = sum(1 for r in results if 0 < r["coverage_ratio"] < 1.0)
    zero = sum(1 for r in results if r["coverage_ratio"] == 0)
    avg_cov = sum(r["coverage_ratio"] for r in results) / len(results) if results else 0

    print(f"\n{'='*60}")
    print("Oracle Evidence 实验结果")
    print(f"{'='*60}")
    print(f"总题数: {len(results)}")
    print(f"引用全 (100%): {full}")
    print(f"引用部分 (1-99%): {partial}")
    print(f"引用零 (0%): {zero}")
    print(f"平均覆盖率: {avg_cov*100:.1f}%")

    print(f"\n{'='*60}")
    print("逐题详情")
    print(f"{'='*60}")
    for r in results:
        status = "全" if r["coverage_ratio"] >= 1.0 else ("部分" if r["coverage_ratio"] > 0 else "零")
        missing = ", ".join(r["missing_files"]) if r["missing_files"] else "-"
        print(f"{r['qa_id']}: {r['coverage_ratio']*100:>3.0f}% [{status}] | gold={len(r['gold_files'])} | 缺失: {missing}")
        if r["eval_notes"]:
            print(f"  备注: {r['eval_notes']}")

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
