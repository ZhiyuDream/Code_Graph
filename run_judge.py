#!/usr/bin/env python3
"""Judge evaluation for Round6 - comparing Graph-Agent vs RAG"""
import json, sys, time
from pathlib import Path
from openai import OpenAI

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

# Load environment from .env
from dotenv import load_dotenv
load_dotenv(_ROOT / ".env")

# Import LLM_MODEL from config
from config import LLM_MODEL

AGENT_FILE = _ROOT / "results/graph_agent_20260403_232050.json"
RAG_FILE = _ROOT / "results/classic_rag_20260402_191755.json"
OUTPUT_FILE = _ROOT / "results/judge_20260403_232050.json"

def safe_str(s):
    if s is None:
        return ""
    return str(s)[:8000]

def judge_one(client, question, reference, ag_answer, rag_answer, model):
    prompt = f"""你是一个严格的编程问答质量评审。请对以下两个回答进行对比评分。

问题: {question}

参考答案: {reference}

Graph-Agent 答案:
{ag_answer}

RAG 答案:
{rag_answer}

评分标准:
- 0.0: Graph-Agent 答案错误或完全不相关，RAG 答案正确或有参考价值
- 0.25: Graph-Agent 答案部分正确但明显不如 RAG
- 0.5: 两者质量相近，难分伯仲
- 0.75: Graph-Agent 答案部分优于 RAG
- 1.0: Graph-Agent 答案明显优于 RAG

请只输出一个 JSON 对象，不要有其他文字：
{{"ag_score": <分数>, "reason": "<简短原因>"}}
"""
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0
        )
        content = resp.choices[0].message.content.strip()
        if content.startswith("```json"):
            content = content[7:]
        if content.endswith("```"):
            content = content[:-3]
        return json.loads(content.strip())
    except Exception as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        return {"ag_score": 0.5, "reason": f"error: {e}"}

def main():
    with open(AGENT_FILE) as f:
        ag_data = json.load(f)
    with open(RAG_FILE) as f:
        rag_data = json.load(f)

    ag_by_idx = {item["index"]: item for item in ag_data}
    rag_by_idx = {item["index"]: item for item in rag_data}

    all_indices = sorted(set(ag_by_idx.keys()) & set(rag_by_idx.keys()))
    print(f"Total cases to judge: {len(all_indices)}")

    client = OpenAI()

    results = []
    for i, idx in enumerate(all_indices):
        ag_item = ag_by_idx[idx]
        rag_item = rag_by_idx[idx]

        question = safe_str(ag_item.get("具体问题", ""))
        reference = safe_str(ag_item.get("参考答案", ""))
        ag_answer = safe_str(ag_item.get("生成答案", ""))
        rag_answer = safe_str(rag_item.get("生成答案", ""))

        print(f"[{i+1}/{len(all_indices)}] Case {idx}...", end=" ", flush=True)
        result = judge_one(client, question, reference, ag_answer, rag_answer, LLM_MODEL)
        print(f"AG={result['ag_score']}")

        results.append({
            "index": idx,
            "question": question[:200],
            "ag_score": result["ag_score"],
            "reason": result["reason"],
            "ag_answer": ag_answer[:500],
            "rag_answer": rag_answer[:500],
        })

        if (i + 1) % 20 == 0:
            time.sleep(1)

    # Save results
    with open(OUTPUT_FILE, "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    # Compute statistics
    scores = [r["ag_score"] for r in results]
    avg = sum(scores) / len(scores)

    ag_wins = sum(1 for s in scores if s > 0.5 + 0.15)
    rag_wins = sum(1 for s in scores if s < 0.5 - 0.15)
    ties = len(scores) - ag_wins - rag_wins

    print(f"\n=== Judge Results ===")
    print(f"Graph-Agent avg: {avg:.4f}")
    print(f"AG wins: {ag_wins}, RAG wins: {rag_wins}, Ties: {ties}")
    print(f"Results saved to: {OUTPUT_FILE}")

if __name__ == "__main__":
    main()