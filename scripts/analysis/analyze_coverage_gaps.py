#!/usr/bin/env python3
"""覆盖率漏引分析：全引用/部分引用/零引用三档拆解。

对部分引用题：每个漏引文件分类为
  - B类「读过没引」（文件访问过但答案未引用 → 答案生成阶段丢证据）
  - A类「一跳可达」（未访问，但漏引文件中的 gold 符号是某个已读函数的
    caller/callee（Neo4j CALLS 1 跳）→ Agent 扩展不足）
  - C类「探索未到」（既未访问也不在一跳范围 → 召回/方向问题）

对零引用题：输出完整探索轨迹摘要（读了什么、搜了什么、方向如何跑偏）。

用法：
  .venv/bin/python scripts/analysis/analyze_coverage_gaps.py \
      --result results/qa_react_v23_full.json \
      --eval results/eval_react_v23_full.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))


def load_gold_symbols(benchmark_path: Path) -> dict:
    """qa_id -> {file: set(符号名)}"""
    items = json.load(open(benchmark_path))["items"]
    out = {}
    for it in items:
        files = {}
        for e in it.get("gold_evidence", []):
            files.setdefault(e["file"], set()).add(e.get("symbol", ""))
        out[it["qa_id"]] = files
    return out


def build_call_index():
    """从 Neo4j 构建函数级 CALLS 邻接（双向，name@file 为 key）。"""
    from src.core.neo4j_client import run_cypher
    rows = run_cypher(
        "MATCH (a:Function)-[:CALLS]->(b:Function) "
        "RETURN a.name AS caller, a.file_path AS cf, b.name AS callee, b.file_path AS bf"
    )
    adj = {}
    for r in rows:
        a, b = f"{r['caller']}@{r['cf']}", f"{r['callee']}@{r['bf']}"
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)  # 双向：Agent 可沿 callers/callees 两个方向扩展
    return adj


def bfs_reachable(adj, sources: set, targets: set, max_depth: int = 8):
    """从 sources 出发 BFS，返回 {target: depth}（不可达的不在结果里）。"""
    from collections import deque
    dist = {s: 0 for s in sources}
    q = deque(sources)
    hit = {}
    while q:
        cur = q.popleft()
        d = dist[cur]
        if d >= max_depth:
            continue
        for nxt in adj.get(cur, ()):
            if nxt in dist:
                continue
            dist[nxt] = d + 1
            if nxt in targets and nxt not in hit:
                hit[nxt] = d + 1
            q.append(nxt)
    return hit


def classify_missing(qa_id, missing_files, gold_symbols, result, adj):
    """对每个漏引文件分类，返回 {file: (类别, 说明)}

    类别：
      B类·读过没引：文件中的函数实际被 read_function/read_lines 读过，但答案未引用
      A类·调用链可达：未读过，但 gold 符号可从某个已读函数沿 CALLS 图（双向）BFS 到达
      C类·探索未到：未读过且调用链不可达

    注意：visited_files 包含 grep/search 命中的文件（不等于读过），
    B类判定必须基于实际 read 动作的文件。
    """
    read_files = set()
    for s in result.get("steps", []):
        if s["action"] in ("read_function", "read_lines"):
            read_files.update(s.get("files_accessed", []))
    read_keys = {k for k in result.get("visited_functions", {}) if "@" in k}
    out = {}
    remaining = []
    for f in missing_files:
        if f in read_files:
            out[f] = ("B类·读过没引", "文件中的函数已读过，答案未引用")
        else:
            remaining.append(f)
    if remaining and read_keys:
        targets = set()
        target_of = {}
        for f in remaining:
            for sym in gold_symbols.get(f, set()):
                if sym:
                    key = f"{sym}@{f}"
                    targets.add(key)
                    target_of.setdefault(f, key)
        hits = bfs_reachable(adj, read_keys, targets)
        for f in remaining:
            key = target_of.get(f)
            if key and key in hits:
                sym = key.split("@")[0]
                out[f] = ("A类·调用链可达", f"gold符号 {sym} 距已读函数 {hits[key]} 跳")
            else:
                syms = sorted(gold_symbols.get(f, set()))[:2]
                out[f] = ("C类·探索未到", f"gold符号 {syms} 从已读函数 8 跳内不可达")
    else:
        for f in remaining:
            syms = sorted(gold_symbols.get(f, set()))[:2]
            out[f] = ("C类·探索未到", f"gold符号 {syms}（无已读函数可扩展）")
    return out


def trajectory_summary(result, max_steps=25):
    """零引用题的探索轨迹摘要。"""
    lines = []
    for s in result.get("steps", [])[:max_steps]:
        ai = json.dumps(s["action_input"], ensure_ascii=False)[:70]
        tag = " [拒]" if s.get("rejected") else ""
        lines.append(f"  {s['step']:2d}. {s['action']}({ai}){tag}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--result", type=Path, required=True)
    ap.add_argument("--eval", type=Path, required=True)
    ap.add_argument("--benchmark", type=Path, default=_ROOT / "datasets" / "benchmark_hard.json")
    ap.add_argument("-o", "--output", type=Path, default=None)
    args = ap.parse_args()

    results = {r["qa_id"]: r for r in json.load(open(args.result))}
    evals = {e["qa_id"]: e for e in json.load(open(args.eval))}
    gold_symbols = load_gold_symbols(args.benchmark)
    print("构建 CALLS 索引...")
    adj = build_call_index()

    full, partial, zero = [], [], []
    for qid, ev in evals.items():
        ratio = ev["eval_citation"]["coverage_ratio"]
        if ratio >= 0.999:
            full.append(qid)
        elif ratio > 0:
            partial.append(qid)
        else:
            zero.append(qid)

    print(f"\n{'='*70}")
    print(f"全引用: {len(full)}  部分引用: {len(partial)}  零引用: {len(zero)}")
    print(f"{'='*70}")

    report = {"full": full, "partial": {}, "zero": {}}
    cls_counter = Counter()

    print(f"\n━━━ 部分引用题（{len(partial)}）：漏引分类 ━━━")
    for qid in sorted(partial):
        ev, res = evals[qid], results[qid]
        missing = ev["eval_citation"]["missing_files"]
        cls = classify_missing(qid, missing, gold_symbols.get(qid, {}), res, adj)
        print(f"\n■ {qid}（覆盖 {ev['eval_citation']['coverage_ratio']:.0%}）")
        report["partial"][qid] = {"coverage": ev["eval_citation"]["coverage_ratio"], "missing": {}}
        for f, (cat, why) in cls.items():
            cls_counter[cat] += 1
            print(f"  [{cat}] {f}\n      {why}")
            report["partial"][qid]["missing"][f] = {"class": cat, "reason": why}

    print(f"\n漏引分类汇总: {dict(cls_counter)}")

    print(f"\n━━━ 零引用题（{len(zero)}）：探索轨迹 ━━━")
    for qid in sorted(zero):
        ev, res = evals[qid], results.get(qid, {})
        print(f"\n■ {qid}")
        print(f"  问题: {ev['question'][:80]}")
        print(f"  gold 文件: {ev['gold_files']}")
        print(f"  实际访问: {res.get('visited_files', [])}")
        print(f"  judge 备注: {ev['eval_citation'].get('notes', '')[:150]}")
        print(f"  轨迹:")
        print(trajectory_summary(res))
        report["zero"][qid] = {
            "gold_files": ev["gold_files"],
            "visited_files": res.get("visited_files", []),
            "judge_notes": ev["eval_citation"].get("notes", ""),
        }

    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"\n报告已保存: {args.output}")


if __name__ == "__main__":
    main()
