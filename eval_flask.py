#!/usr/bin/env python3
"""
SWE-QA Flask 评测脚本。
使用 Graph-Agent (RAG+Graph 混合) 在 Flask 数据集上生成答案，
输出格式与 SWE-QA benchmark 的评分脚本兼容。
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
load_dotenv(_ROOT / ".env")

from tools.agent_qa import run_agent
from neo4j_writer import get_driver


def run_flask_eval(limit: int = 0, output: str | None = None):
    """在 Flask SWE-QA 数据集上运行 Graph-Agent 评测"""
    dataset_path = _ROOT.parent / "SWE-QA-Bench" / "SWE-QA-Bench" / "datasets" / "questions" / "flask.jsonl"

    if not dataset_path.exists():
        print(f"ERROR: Dataset not found at {dataset_path}")
        return

    # 加载问题
    questions = []
    with open(dataset_path) as f:
        for line in f:
            questions.append(json.loads(line.strip()))

    if limit > 0:
        questions = questions[:limit]

    print(f"Running Graph-Agent on {len(questions)} Flask questions...")

    # 设置 Flask RAG 索引
    os.environ["RAG_INDEX_NAME"] = "flask_rag_index.json"

    driver = get_driver()
    results = []

    for i, item in enumerate(questions):
        question = item["question"]
        ground_truth = item.get("ground_truth", "")

        print(f"[{i+1}/{len(questions)}] {question[:60]}...", end=" ", flush=True)
        t0 = time.time()
        try:
            answer, traj, steps, tokens = run_agent(driver, question)
            latency = time.time() - t0
            results.append({
                "question": question,
                "final_answer": answer,
                "ground_truth": ground_truth,
                "steps": steps,
                "latency_s": round(latency, 2),
                "tokens": tokens,
            })
            print(f"OK steps={steps} ({latency:.1f}s)")
        except Exception as e:
            print(f"ERROR: {e}")
            results.append({
                "question": question,
                "final_answer": f"ERROR: {e}",
                "ground_truth": ground_truth,
                "error": str(e),
            })

    driver.close()

    # 保存结果（兼容 SWE-QA 格式）
    if output is None:
        output = f"results/flask_graph_agent_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
    output_path = _ROOT / output
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"\nResults saved to: {output_path}")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="限制题目数量")
    parser.add_argument("--output", type=str, default=None, help="输出文件路径(.jsonl)")
    args = parser.parse_args()
    run_flask_eval(limit=args.limit, output=args.output)
