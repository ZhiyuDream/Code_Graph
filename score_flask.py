#!/usr/bin/env python3
"""
SWE-QA 评分脚本 - 对标官方 SWE-QA benchmark 评分标准。
5 个维度评分（每项 1-10）：
  correctness, completeness, relevance, clarity, reasoning
总分 50 分。
"""
import argparse
import json
import os
import sys
import concurrent.futures
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
load_dotenv(_ROOT / ".env")

from openai import OpenAI
from config import LLM_MODEL

client = OpenAI(
    api_key=os.environ.get("OPENAI_API_KEY", ""),
    base_url=os.environ.get("OPENAI_BASE_URL", "") or None,
)


def score_single(question: str, reference: str, candidate: str, model: str = LLM_MODEL) -> dict | None:
    """用 LLM 评判对单条答案评分"""
    prompt = f"""You are a professional evaluator. Please rate the candidate answer against the reference answer based on five criteria.
Evaluation Criteria and Scoring Guidelines (each scored 1 to 10):
    1. Correctness:
        10 — Completely correct; core points and details are accurate with no ambiguity.
        8-9 — Mostly correct; only minor details are slightly inaccurate or loosely expressed.
        6-7 — Partially correct; some errors or omissions, but main points are generally accurate.
        4-5 — Several errors or ambiguities that affect understanding of the core information.
        2-3 — Many errors; misleading or fails to convey key information.
        1 — Serious errors; completely wrong or misleading.
    2. Completeness:
        10 — Covers all key points from the reference answer without omission.
        8-9 — Covers most key points; only minor non-critical information missing.
        6-7 — Missing several key points; content is somewhat incomplete.
        4-5 — Important information largely missing; content is one-sided.
        2-3 — Covers very little relevant information; seriously incomplete.
        1 — Covers almost no relevant information; completely incomplete.
    3. Relevance:
        10 — Content fully focused on the question topic; no irrelevant information.
        8-9 — Mostly focused; only minor irrelevant or peripheral information.
        6-7 — Generally on topic; some off-topic content but still relevant overall.
        4-5 — Topic not sufficiently focused; contains considerable off-topic content.
        2-3 — Content deviates from topic; includes excessive irrelevant information.
        1 — Majority of content irrelevant to the question.
    4. Clarity:
        10 — Fluent language; clear and precise expression; very easy to understand.
        8-9 — Mostly fluent; clear expression with minor unclear points.
        6-7 — Generally clear; some expressions slightly unclear or not concise.
        4-5 — Expression somewhat awkward; some ambiguity or lack of fluency.
        2-3 — Language obscure; sentences are not smooth; hinders understanding.
        1 — Expression confusing; very difficult to understand.
    5. Reasoning:
        10 — Reasoning is clear, logical, and well-structured; argumentation is excellent.
        8-9 — Reasoning is clear and logical; well-structured with solid argumentation.
        6-7 — Reasoning generally reasonable; mostly clear logic; minor jumps.
        4-5 — Reasoning is average; some logical jumps or organization issues.
        2-3 — Reasoning unclear; lacks logical order; difficult to follow.
        1 — No clear reasoning; logic is chaotic.

INPUT:
    Question:{question}
    Reference Answer:{reference}
    Candidate Answer:{candidate}

OUTPUT:
    Please output ONLY a JSON object with 5 integer fields in the range [1,10], corresponding
    to the evaluation scores:
        {{
        "correctness": <1-10>,
        "completeness": <1-10>,
        "relevance": <1-10>,
        "clarity": <1-10>,
        "reasoning": <1-10>
        }}

REQUIREMENT:
    No explanation, no extra text, no formatting other than valid JSON"""

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a helpful assistant"},
                {"role": "user", "content": prompt},
            ],
            stream=False,
            timeout=60,
        )
        score_str = resp.choices[0].message.content.strip()
        if score_str.startswith("```json"):
            score_str = score_str[7:]
        if score_str.endswith("```"):
            score_str = score_str[:-3]
        score_str = score_str.strip()
        scores = json.loads(score_str)
        for key in ["correctness", "completeness", "relevance", "clarity", "reasoning"]:
            if key not in scores or not (1 <= scores[key] <= 10):
                print(f"  [WARN] Invalid score: {key}={scores.get(key)}")
                return None
        return scores
    except Exception as e:
        print(f"  [ERROR] {e}")
        return None


def run_scoring(input_path: str, output_path: str | None = None, max_workers: int = 16):
    """对 Graph-Agent 的输出进行评分"""
    if output_path is None:
        output_path = input_path.replace(".jsonl", "_scored.jsonl")

    # 读取结果
    records = []
    with open(input_path, encoding="utf-8") as f:
        for line in f:
            records.append(json.loads(line))

    print(f"Scoring {len(records)} records with {max_workers} workers...")

    scored = []
    for i, rec in enumerate(records):
        question = rec.get("question", "")
        reference = rec.get("ground_truth", rec.get("reference", ""))
        candidate = rec.get("final_answer", rec.get("answer", ""))

        print(f"[{i+1}/{len(records)}] {question[:50]}...", end=" ", flush=True)
        scores = score_single(question, reference, candidate)
        if scores:
            total = sum(scores.values())
            result = {
                **rec,
                "correctness": scores["correctness"],
                "completeness": scores["completeness"],
                "relevance": scores["relevance"],
                "clarity": scores["clarity"],
                "reasoning": scores["reasoning"],
                "total_score": total,
            }
            scored.append(result)
            print(f"total={total}/50")
        else:
            print("FAILED")

        if (i + 1) % 20 == 0:
            time.sleep(1)

    # 保存
    with open(output_path, "w", encoding="utf-8") as f:
        for r in scored:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # 统计
    if scored:
        totals = [r["total_score"] for r in scored]
        avg = sum(totals) / len(totals)
        correctness_avg = sum(r["correctness"] for r in scored) / len(scored)
        completeness_avg = sum(r["completeness"] for r in scored) / len(scored)
        print(f"\n=== Scoring Results ===")
        print(f"Scored: {len(scored)}/{len(records)}")
        print(f"Average Total: {avg:.2f}/50")
        print(f"Avg Correctness: {correctness_avg:.2f}/10")
        print(f"Avg Completeness: {completeness_avg:.2f}/10")
        print(f"Results: {output_path}")

    return scored


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("input", help="Graph-Agent 输出文件(.jsonl)")
    parser.add_argument("--output", "-o", help="评分输出文件(.jsonl)")
    parser.add_argument("--workers", "-w", type=int, default=16)
    args = parser.parse_args()
    run_scoring(args.input, args.output, args.workers)
