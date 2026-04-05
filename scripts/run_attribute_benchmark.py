#!/usr/bin/env python3
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT.parent))
"""
Attribute Benchmark 并行评测脚本 - 支持双版本对比

用法：
  # 新版本 (with read_file_lines, search_attributes fix)
  python run_attribute_benchmark.py --csv /tmp/attribute_qs.csv \
    --output experiments/attribute_v4_new.json --workers 20 --name v4_new

  # 旧版本 (without read_file_lines) - 用于对比
  python run_attribute_benchmark.py --csv /tmp/attribute_qs.csv \
    --output experiments/attribute_v4_old.json --workers 20 --name v4_old --no-read-file

  # 对比报告
  python run_attribute_benchmark.py --compare \
    --new experiments/attribute_v4_new.json \
    --old experiments/attribute_v4_old.json
"""
import argparse
import json
import sys
import time
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from functools import wraps

from agent_qa import run_agent as _run_agent_orig, SYSTEM_PROMPT, MAX_STEPS, TOOLS, TOOL_MAP
from src.neo4j_writer import get_driver
from config import OPENAI_API_KEY, NEO4J_DATABASE, LLM_MODEL, OPENAI_BASE_URL


# ---------------------------------------------------------------------------
# 可选：禁用 read_file_lines 的 agent（用于对比实验）
# ---------------------------------------------------------------------------
def run_agent_no_readfile(driver, question: str):
    """旧版 agent：不含 read_file_lines 工具"""
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)

    # 移除 read_file_lines
    tools_filtered = [t for t in TOOLS if t["function"]["name"] != "read_file_lines"]
    tool_map_filtered = {k: v for k, v in TOOL_MAP.items() if k != "tool_read_file_lines"}

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    trajectory = []
    steps = 0
    total_tokens = 0
    prompt_tokens = 0
    completion_tokens = 0

    for _ in range(MAX_STEPS):
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=messages,
            tools=tools_filtered,
            tool_choice="auto",
            max_tokens=800,
            timeout=60,
        )
        usage = resp.usage
        if usage:
            prompt_tokens += usage.prompt_tokens or 0
            completion_tokens += usage.completion_tokens or 0
            total_tokens += usage.total_tokens or 0
        msg = resp.choices[0].message

        if not msg.tool_calls:
            token_stats = {"total": total_tokens, "prompt": prompt_tokens, "completion": completion_tokens}
            return msg.content.strip() if msg.content else "(无答案)", trajectory, steps, token_stats

        messages.append({"role": "assistant", "content": msg.content, "tool_calls": [
            {"id": tc.id, "type": "function",
             "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
            for tc in msg.tool_calls
        ]})

        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments)
            except Exception:
                args = {}

            fn = tool_map_filtered.get(name)
            if fn:
                try:
                    result = fn(driver, **args)
                except Exception as e:
                    result = f"工具调用出错: {e}"
            else:
                result = f"未知工具: {name}"

            if len(result) > 999999:
                result = result[:999999] + "\n...(已截断)"

            trajectory.append({"tool": name, "args": args, "result_snippet": result[:200]})
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })

        steps += 1

    resp = client.chat.completions.create(
        model=LLM_MODEL,
        messages=messages + [{"role": "user",
                             "content": "请根据以上工具查询结果，现在给出最终答案。"}],
        max_tokens=800,
        timeout=60,
    )
    usage = resp.usage
    if usage:
        prompt_tokens += usage.prompt_tokens or 0
        completion_tokens += usage.completion_tokens or 0
        total_tokens += usage.total_tokens or 0
    final = resp.choices[0].message.content or "(无答案)"
    token_stats = {"total": total_tokens, "prompt": prompt_tokens, "completion": completion_tokens}
    return final.strip(), trajectory, steps, token_stats


def run_agent_new(driver, question: str):
    """新版 agent：含 read_file_lines"""
    return _run_agent_orig(driver, question)


def run_one(row_dict, driver, q_col, ev_col, use_readfile):
    idx = row_dict.get("index", row_dict.get("_idx", 0))
    question = str(row_dict.get(q_col, ""))
    reference = str(row_dict.get("答案", ""))
    evidence_raw = str(row_dict.get(ev_col, ""))
    subcat = str(row_dict.get("子类", ""))
    category = str(row_dict.get("类别", ""))
    entity = str(row_dict.get("实体", ""))

    agent_fn = run_agent_new if use_readfile else run_agent_no_readfile
    agent_name = "new" if use_readfile else "old"

    print(f"[{idx}] ({agent_name}) {question[:50]}...", flush=True)
    t0 = time.time()
    try:
        answer, trajectory, steps, token_stats = agent_fn(driver, question)
        latency = round(time.time() - t0, 2)
        gold_files, gold_loc = parse_evidence(evidence_raw)
        ev_hit = calc_evidence_hit(gold_files, gold_loc, trajectory, answer)
        return {
            "index": int(idx),
            "类别": category,
            "子类": subcat,
            "实体名称": entity,
            "问题": question,
            "参考答案": reference,
            "生成答案": answer,
            "Evidence": evidence_raw,
            "证据命中": ev_hit,
            "工具调用步数": steps,
            "工具轨迹": trajectory,
            "延迟_s": latency,
            "token_stats": token_stats,
            "错误": None,
        }
    except Exception as e:
        latency = round(time.time() - t0, 2)
        print(f"  ERROR: {e}", flush=True)
        return {
            "index": int(idx),
            "类别": category,
            "子类": subcat,
            "实体名称": entity,
            "问题": question,
            "参考答案": reference,
            "生成答案": "",
            "Evidence": evidence_raw,
            "证据命中": {},
            "工具调用步数": 0,
            "工具轨迹": [],
            "延迟_s": latency,
            "token_stats": {"total": 0, "prompt": 0, "completion": 0},
            "错误": str(e),
        }


def parse_evidence(evidence_str: str) -> tuple[set, str]:
    """解析 Evidence JSON 字符串，返回 (文件路径集合, location原文)"""
    import json, re
    try:
        ev = json.loads(evidence_str)
    except Exception:
        return set(), evidence_str
    loc = ev.get("location", "")
    # 从 "ggml/src/ggml-quants.c:2671,2673,..." 提取文件路径
    m = re.match(r'(.+):[\d,]+', loc)
    if m:
        file_path = m.group(1)
    else:
        file_path = loc
    return {file_path}, loc


def calc_evidence_hit(gold_files: set, gold_loc: str, trajectory: list, answer: str) -> dict:
    """gold_files: 文件路径集合; gold_loc: location原文"""
    import re
    hit = False
    # 检查 answer 是否提到 gold 文件路径
    for fp in gold_files:
        if fp in answer or fp in gold_loc:
            hit = True
            break
    # 也检查 trajectory result_snippet 中是否出现文件路径
    if not hit:
        for entry in trajectory:
            snippet = entry.get("result_snippet", "")
            for fp in gold_files:
                if fp in snippet:
                    hit = True
                    break
            if hit:
                break
    recall = 1.0 if hit else 0.0
    return {"recall": recall, "hit": hit, "gold_files": list(gold_files)}


def report_summary(results, name="system"):
    ev_recalls = [r["证据命中"].get("recall", 0) for r in results if r["错误"] is None]
    avg_ev_recall = sum(ev_recalls) / len(ev_recalls) if ev_recalls else 0
    avg_latency = sum(r["延迟_s"] for r in results) / len(results)
    avg_steps = sum(r["工具调用步数"] for r in results) / len(results)
    total_toks = sum(r.get("token_stats", {}).get("total", 0) for r in results)
    total_err = sum(1 for r in results if r["错误"] is not None)
    judge_scores = [r.get("llm_judge_score", 0) for r in results if r.get("llm_judge_score") is not None]
    avg_judge = sum(judge_scores) / len(judge_scores) if judge_scores else 0
    bs_scores = [r.get("bert_score", 0) for r in results if r.get("bert_score") is not None]
    avg_bs = sum(bs_scores) / len(bs_scores) if bs_scores else 0
    return {
        "name": name,
        "n": len(results),
        "errors": total_err,
        "llm_judge": avg_judge,
        "bert_score": avg_bs,
        "ev_recall": avg_ev_recall,
        "avg_latency_s": avg_latency,
        "avg_steps": avg_steps,
        "total_tokens": total_toks,
        "avg_tokens": total_toks / len(results) if results else 0,
    }


def compare(new_path, old_path):
    new_data = json.load(open(new_path))
    old_data = json.load(open(old_path))

    new_sum = report_summary(new_data, "new (w/ read_file_lines)")
    old_sum = report_summary(old_data, "old (w/o read_file_lines)")

    print(f"\n{'='*70}")
    print(f"{'指标':<20} {'旧版 (无read_file)':<22} {'新版 (有read_file)':<22} {'变化':<15}")
    print(f"{'-'*70}")

    fields = [
        ("LLM-judge", "llm_judge", ".1%"),
        ("BERTScore", "bert_score", ".4f"),
        ("Evidence Recall", "ev_recall", ".2%"),
        ("平均延迟(s)", "avg_latency_s", ".2f"),
        ("平均工具步数", "avg_steps", ".1f"),
        ("总Token消耗", "total_tokens", "d"),
        ("平均Token/题", "avg_tokens", ".0f"),
    ]

    for label, key, fmt in fields:
        o = old_sum[key]
        n = new_sum[key]
        if key == "total_tokens":
            diff = f"{n - o:+d}"
        elif "judge" in key or "recall" in key:
            diff = f"{(n-o)*100:+.1f}%"
        else:
            diff = f"{n-o:+.2f}"
        if "recall" in key or "judge" in key:
            old_str = f"{o*100:.1f}%"
            new_str = f"{n*100:.1f}%"
        elif key == "total_tokens":
            old_str = f"{o:,}"
            new_str = f"{n:,}"
        else:
            old_str = f"{o:.2f}"
            new_str = f"{n:.2f}"
        print(f"{label:<20} {old_str:<22} {new_str:<22} {diff:<15}")
    print(f"{'='*70}")

    # Good case: new better than old
    old_by_idx = {r["index"]: r for r in old_data}
    new_by_idx = {r["index"]: r for r in new_data}
    indices = sorted(set(r["index"] for r in new_data))

    improved = []
    degraded = []
    for idx in indices:
        o = old_by_idx.get(idx, {})
        n = new_by_idx.get(idx, {})
        o_score = o.get("llm_judge_score", 0) if o else 0
        n_score = n.get("llm_judge_score", 0) if n else 0
        if n_score > o_score:
            improved.append((idx, o_score, n_score, new_by_idx[idx]["问题"][:50]))
        elif n_score < o_score:
            degraded.append((idx, o_score, n_score, new_by_idx[idx]["问题"][:50]))

    print(f"\n改进 ({len(improved)}):")
    for idx, o, n, q in improved:
        print(f"  idx={idx} {o:.0f}→{n:.0f} | {q}")

    print(f"\n退化 ({len(degraded)}):")
    for idx, o, n, q in degraded:
        print(f"  idx={idx} {o:.0f}→{n:.0f} | {q}")

    return new_sum, old_sum


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", type=Path, default=Path("/tmp/attribute_qs.csv"))
    parser.add_argument("--output", type=Path,
                        default=Path(__file__).resolve().parent / "experiments" / "attribute_v4_new.json")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--workers", type=int, default=20)
    parser.add_argument("--name", type=str, default="new")
    parser.add_argument("--no-read-file", action="store_true",
                        help="使用旧版agent（不含read_file_lines工具）")
    parser.add_argument("--compare", action="store_true")
    parser.add_argument("--new", type=Path)
    parser.add_argument("--old", type=Path)
    args = parser.parse_args()

    if args.compare:
        if not args.new or not args.old:
            print("ERROR: --compare 需要 --new 和 --old", file=sys.stderr)
            sys.exit(1)
        compare(args.new, args.old)
        return

    if not OPENAI_API_KEY:
        print("ERROR: OPENAI_API_KEY 未设置", file=sys.stderr)
        sys.exit(1)

    df = pd.read_csv(args.csv, encoding="utf-8")
    if args.limit > 0:
        df = df.head(args.limit)

    q_col = "问题" if "问题" in df.columns else "具体问题"
    ev_col = "Evidence"

    rows = []
    for orig_idx, row in df.iterrows():
        d = dict(row)
        d["_idx"] = int(orig_idx)
        rows.append(d)

    driver = get_driver()
    driver.verify_connectivity()

    results = []
    total = len(rows)
    lock = __import__("threading").Lock()
    use_readfile = not args.no_read_file

    t_start = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(run_one, r, driver, q_col, ev_col, use_readfile): r for r in rows}
        for i, future in enumerate(as_completed(futures)):
            result = future.result()
            with lock:
                results.append(result)
                ts = result.get("token_stats", {})
                print(f"  [{i+1}/{total}] steps={result['工具调用步数']} "
                      f"latency={result['延迟_s']}s tokens={ts.get('total',0)} "
                      f"ev_recall={result['证据命中'].get('recall','N/A')}", flush=True)

            if (i + 1) % 5 == 0:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                with open(args.output, "w", encoding="utf-8") as f:
                    json.dump(results, f, ensure_ascii=False, indent=2)

    driver.close()

    total_time = round(time.time() - t_start, 2)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    s = report_summary(results, args.name)
    print(f"\n{'='*60}")
    print(f"完成：{len(results)} 条 → {args.output}")
    print(f"总耗时: {total_time}s")
    print(f"LLM-judge: {s['llm_judge']:.1%}")
    print(f"BERTScore: {s['bert_score']:.4f}")
    print(f"Evidence Recall: {s['ev_recall']:.2%}")
    print(f"平均延迟: {s['avg_latency_s']:.2f}s")
    print(f"平均工具步数: {s['avg_steps']:.1f}")
    print(f"总Token: {s['total_tokens']} (平均 {s['avg_tokens']:.0f}/题)")
    print(f"错误数: {s['errors']}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()