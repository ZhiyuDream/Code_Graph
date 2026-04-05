import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT / "src"))   # 核心库
sys.path.insert(0, str(_ROOT))             # 根目录（config.py）

#!/usr/bin/env python3
"""
评测汇总脚本：读取 agent_qa / classic_rag 的结果 JSON，计算各项指标并输出对比报告。

指标：
  1. LLM-judge 分数 (0-1)：正确性 + 完整性
  2. Evidence Recall：检索中包含 gold evidence 的比例
  3. BERTScore (F1)：生成答案 vs 参考答案的语义相似度
  4. Latency (s)：每题平均耗时
  5. 按类别/子类细分

用法：
  # 先打分（LLM-judge + BERTScore）
  python eval_qa_v3.py score --input experiments/agent_qa_full.json

  # 对比两个系统
  python eval_qa_v3.py compare \
    --agent experiments/agent_qa_full.json \
    --rag   experiments/classic_rag_results.json

  # 只看摘要
  python eval_qa_v3.py summary --input experiments/agent_qa_full.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from config import OPENAI_API_KEY, OPENAI_BASE_URL, LLM_MODEL


# ---------------------------------------------------------------------------
# LLM-judge
# ---------------------------------------------------------------------------

JUDGE_PROMPT = """请对比「参考答案」与「生成答案」，判断生成答案是否可接受。

评分标准（0/1）：
- 1：生成答案覆盖了参考答案的主要信息，没有严重错误，可以接受
- 0：生成答案与参考答案严重不符，或有明显错误，或未回答问题，不可接受

必须首行输出：分数: 0 或 1
第二行起：一句话说明理由

【问题】
{question}

【参考答案】
{reference}

【生成答案】
{generated}
"""


def llm_judge(client, question: str, reference: str, generated: str) -> tuple[float | None, str]:
    import re
    prompt = JUDGE_PROMPT.format(
        question=question[:300],
        reference=reference[:1500],
        generated=generated[:1500],
    )
    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
            timeout=30,
        )
        text = (resp.choices[0].message.content or "").strip()
        m = re.search(r"分数\s*[:：]\s*(0|1)\b", text)
        score = float(m.group(1)) if m else None
        lines = text.split("\n")
        reason = ""
        for i, line in enumerate(lines):
            if re.search(r"分数\s*[:：]", line):
                reason = "\n".join(lines[i+1:]).strip()
                break
        return score, reason or text
    except Exception as e:
        return None, f"judge 失败: {e}"


# ---------------------------------------------------------------------------
# BERTScore（轻量版：用 sentence-transformers 余弦相似度代替完整 BERTScore）
# ---------------------------------------------------------------------------

def bert_score_lite(reference: str, generated: str, client, embed_model: str) -> float | None:
    """用 embedding 余弦相似度近似 BERTScore F1（无需 GPU）"""
    if not reference.strip() or not generated.strip():
        return None
    try:
        resp = client.embeddings.create(
            model=embed_model,
            input=[reference[:1000], generated[:1000]],
        )
        a = resp.data[0].embedding
        b = resp.data[1].embedding
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(y * y for y in b) ** 0.5
        return round(dot / (na * nb), 4) if na and nb else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 打分命令
# ---------------------------------------------------------------------------

def cmd_score(args):
    if not OPENAI_API_KEY:
        print("ERROR: OPENAI_API_KEY 未设置", file=sys.stderr)
        sys.exit(1)

    from openai import OpenAI
    from config import EMBEDDING_MODEL
    client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)

    data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    total = len(data)
    parallel = getattr(args, 'parallel', 1)
    print(f"共 {total} 条，开始打分（LLM-judge 0/1 + BERTScore），并行数={parallel}...")

    # 收集需要打分的项
    pending = []
    for i, item in enumerate(data):
        if "llm_judge_score" in item and item["llm_judge_score"] is not None:
            continue
        if not item.get("生成答案") or item.get("错误"):
            item["llm_judge_score"] = 0.0
            item["llm_judge_reason"] = "生成失败"
            item["bert_score"] = None
            continue
        pending.append((i, item))

    if not pending:
        print("所有条目已有分数，跳过")
        return

    # 并行打分
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def score_one(idx_item):
        idx, item = idx_item
        question = item.get("问题", "")
        reference = item.get("参考答案", "")
        generated = item.get("生成答案", "")
        score, reason = llm_judge(client, question, reference, generated)
        bs = bert_score_lite(reference, generated, client, EMBEDDING_MODEL)
        return idx, score, reason, bs

    completed = 0
    with ThreadPoolExecutor(max_workers=parallel) as executor:
        futures = {executor.submit(score_one, p): p for p in pending}
        for future in as_completed(futures):
            idx, score, reason, bs = future.result()
            data[idx]["llm_judge_score"] = score
            data[idx]["llm_judge_reason"] = reason
            data[idx]["bert_score"] = bs
            completed += 1
            print(f"[{completed}/{len(pending)}] idx={data[idx]['index']} judge={score} bs={bs}", flush=True)

            # 每 20 条保存
            if completed % 20 == 0:
                Path(args.input).write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                            encoding="utf-8")
                print(f"  [checkpoint] 已保存 {completed} 条", flush=True)

    Path(args.input).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"打分完成，结果已更新到 {args.input}")


# ---------------------------------------------------------------------------
# 摘要统计
# ---------------------------------------------------------------------------

def compute_summary(data: list[dict], label: str = "") -> dict:
    import statistics

    def safe_mean(vals):
        vals = [v for v in vals if v is not None]
        return round(statistics.mean(vals), 4) if vals else None

    judge_scores = [d.get("llm_judge_score") for d in data]
    bert_scores = [d.get("bert_score") for d in data]
    ev_recalls = [d.get("证据命中", {}).get("recall") for d in data]
    latencies = [d.get("延迟_s") for d in data]
    steps_list = [d.get("工具调用步数") for d in data]  # agent only

    summary = {
        "system": label,
        "n": len(data),
        "llm_judge_mean": safe_mean(judge_scores),
        "bert_score_mean": safe_mean(bert_scores),
        "ev_recall_mean": safe_mean(ev_recalls),
        "latency_mean_s": safe_mean(latencies),
        "steps_mean": safe_mean(steps_list) if any(s is not None for s in steps_list) else None,
        "errors": sum(1 for d in data if d.get("错误")),
    }

    # 按类别细分
    by_cat: dict[str, list] = {}
    for d in data:
        cat = d.get("类别", "?")
        by_cat.setdefault(cat, []).append(d)
    summary["by_category"] = {
        cat: {
            "n": len(items),
            "llm_judge_mean": safe_mean([d.get("llm_judge_score") for d in items]),
            "ev_recall_mean": safe_mean([d.get("证据命中", {}).get("recall") for d in items]),
        }
        for cat, items in by_cat.items()
    }

    # 按子类细分
    by_sub: dict[str, list] = {}
    for d in data:
        sub = d.get("子类", "?")
        by_sub.setdefault(sub, []).append(d)
    summary["by_subcat"] = {
        sub: {
            "n": len(items),
            "llm_judge_mean": safe_mean([d.get("llm_judge_score") for d in items]),
            "ev_recall_mean": safe_mean([d.get("证据命中", {}).get("recall") for d in items]),
        }
        for sub, items in sorted(by_sub.items())
    }

    return summary


def print_summary(s: dict):
    print(f"\n{'='*60}")
    print(f"系统: {s['system']}  (n={s['n']}, errors={s['errors']})")
    print(f"{'='*60}")
    print(f"  LLM-judge 均分 : {s['llm_judge_mean']}")
    print(f"  BERTScore 均分 : {s['bert_score_mean']}")
    print(f"  Evidence Recall: {s['ev_recall_mean']}")
    print(f"  平均延迟 (s)   : {s['latency_mean_s']}")
    if s.get("steps_mean") is not None:
        print(f"  平均工具步数   : {s['steps_mean']}")
    print()
    print("  按类别:")
    for cat, v in s.get("by_category", {}).items():
        print(f"    {cat:12s}  n={v['n']:3d}  judge={v['llm_judge_mean']}  ev_recall={v['ev_recall_mean']}")
    print()
    print("  按子类:")
    for sub, v in s.get("by_subcat", {}).items():
        print(f"    {sub:20s}  n={v['n']:2d}  judge={v['llm_judge_mean']}  ev_recall={v['ev_recall_mean']}")


def cmd_summary(args):
    data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    name = Path(args.input).stem
    s = compute_summary(data, label=name)
    print_summary(s)
    out = Path(args.input).with_suffix(".summary.json")
    out.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n摘要已保存: {out}")


# ---------------------------------------------------------------------------
# 对比命令
# ---------------------------------------------------------------------------

def cmd_compare(args):
    agent_data = json.loads(Path(args.agent).read_text(encoding="utf-8"))
    rag_data = json.loads(Path(args.rag).read_text(encoding="utf-8"))

    s_agent = compute_summary(agent_data, label="Graph-Agent")
    s_rag = compute_summary(rag_data, label="Classic-RAG")

    print_summary(s_agent)
    print_summary(s_rag)

    # 差值
    print(f"\n{'='*60}")
    print("对比 (Graph-Agent - Classic-RAG):")
    for k in ("llm_judge_mean", "bert_score_mean", "ev_recall_mean"):
        a = s_agent.get(k)
        b = s_rag.get(k)
        if a is not None and b is not None:
            print(f"  {k:20s}: {a:.4f} vs {b:.4f}  delta={a-b:+.4f}")

    out = Path(args.agent).parent / "comparison_report.json"
    out.write_text(json.dumps({"agent": s_agent, "rag": s_rag},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n对比报告已保存: {out}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")

    sc = sub.add_parser("score", help="对结果 JSON 打 LLM-judge 和 BERTScore 分")
    sc.add_argument("--input", required=True)
    sc.add_argument("--parallel", type=int, default=1,
                    help="并行打分数量，默认1（串行），建议20")

    ss = sub.add_parser("summary", help="输出统计摘要")
    ss.add_argument("--input", required=True)

    cp = sub.add_parser("compare", help="对比两个系统")
    cp.add_argument("--agent", required=True)
    cp.add_argument("--rag", required=True)

    args = parser.parse_args()
    if args.cmd == "score":
        cmd_score(args)
    elif args.cmd == "summary":
        cmd_summary(args)
    elif args.cmd == "compare":
        cmd_compare(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
