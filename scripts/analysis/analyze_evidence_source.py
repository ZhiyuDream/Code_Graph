#!/usr/bin/env python3
"""证据来源归因：每个被引用的 gold 文件最初是如何被发现的。

分类：
  - 召回(concept-symbol)：函数在初始召回池中（concept 聚类 + embedding + symbol 重排）
  - 调用链：由 find_callers / find_callees 发现（含 V26 系统自动读调用点）
  - 符号搜索：由 search_symbol 发现
  - 目录探索：由 list_files / list_functions 引导发现
  - 其他：无法归因

判定逻辑：找到该 gold 文件第一次被 read（read_function/read_lines）的步，
看该文件此前是否出现在召回池 / 各扩展动作的返回里。

用法：
  .venv/bin/python scripts/analysis/analyze_evidence_source.py \
      --result results/qa_react_v26_failing.json \
      --eval results/eval_react_v26_failing.json
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def attribute_question(result: dict, cited_gold: list[str]) -> dict:
    """返回 {gold_file: 来源}。"""
    recall_files = {f["file_path"] for f in result.get("initial_functions", [])}

    # 按步扫描：维护"已由各渠道发现的文件"时间线
    first_read = {}       # file -> step_idx（第一次被读的步）
    first_read_action = {}  # file -> 第一次被读时的动作
    channel_files = []    # [(step_idx, channel, set(files))]，read 步之前的发现记录

    for idx, s in enumerate(result.get("steps", [])):
        action = s["action"]
        files = set(s.get("files_accessed", []))
        if action in ("find_callers", "find_callees"):
            channel_files.append((idx, "调用链", files))
            # V26 系统自动读调用点：observation 中含 "[系统自动读取调用点]"，
            # 其后 "函数 name (file:start-end):" 的文件视为真正读过
            if "[系统自动读取调用点]" in s.get("observation", ""):
                import re as _re
                for m in _re.finditer(r"函数 \S+ \((\S+?):\d+-\d+\)", s["observation"]):
                    if m.group(1) not in first_read:
                        first_read[m.group(1)] = idx
                        first_read_action[m.group(1)] = action
        elif action == "search_symbol":
            channel_files.append((idx, "符号搜索", files))
        elif action in ("list_files", "list_functions"):
            fp = s["action_input"].get("file_path") or s["action_input"].get("directory") or ""
            channel_files.append((idx, "目录探索", {fp} if fp else set()))
        elif action in ("read_function", "read_lines"):
            for f in files:
                if f not in first_read:
                    first_read[f] = idx
                    first_read_action[f] = action

    out = {}
    for gold in cited_gold:
        if gold not in first_read:
            out[gold] = "未读却被引（可疑）"
            continue
        if first_read_action.get(gold) in ("find_callers", "find_callees"):
            out[gold] = "调用链"
            continue
        read_idx = first_read[gold]
        if gold in recall_files:
            out[gold] = "召回(concept-symbol)"
            continue
        # 找 read 之前最后一次由某渠道发现该文件的记录
        channel = None
        for idx, ch, files in channel_files:
            if idx < read_idx and gold in files:
                channel = ch
        out[gold] = channel or "其他"
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--result", type=Path, required=True)
    ap.add_argument("--eval", type=Path, required=True)
    args = ap.parse_args()

    results = {r["qa_id"]: r for r in json.load(open(args.result))}
    evals = {e["qa_id"]: e for e in json.load(open(args.eval))}

    total = Counter()
    per_q = defaultdict(dict)
    for qid, ev in evals.items():
        cited = ev["eval_citation"]["cited_files"]
        if not cited or qid not in results:
            continue
        attr = attribute_question(results[qid], cited)
        per_q[qid] = attr
        total.update(attr.values())

    n = sum(total.values())
    print(f"\n=== 证据来源归因（{n} 个被引用的 gold 文件）===")
    for ch, cnt in total.most_common():
        print(f"  {ch}: {cnt} ({cnt/n:.0%})")

    print(f"\n=== 逐题明细 ===")
    for qid in sorted(per_q):
        items = ", ".join(f"{Path(f).name}→{ch}" for f, ch in sorted(per_q[qid].items()))
        print(f"  {qid}: {items}")

    # 按题统计：至少有一个 gold 由调用链发现的题数
    q_with_chain = sum(1 for attr in per_q.values() if "调用链" in attr.values())
    q_with_recall = sum(1 for attr in per_q.values() if "召回(concept-symbol)" in attr.values())
    print(f"\n含调用链证据的题: {q_with_chain}/{len(per_q)}")
    print(f"含召回证据的题: {q_with_recall}/{len(per_q)}")


if __name__ == "__main__":
    main()
