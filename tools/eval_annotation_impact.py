import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT / "src"))   # 核心库
sys.path.insert(0, str(_ROOT))             # 根目录（config.py）

#!/usr/bin/env python3
"""
评估「函数注释（annotation）」对 QA 质量的影响。

思路：
  对同一道题，分别用「无注释」和「有注释」两种检索文本调用 LLM 生成答案，
  再用 LLM 评分（0-1）对比差异。

用法：
  python eval_annotation_impact.py [--csv PATH] [--limit N] [--output PATH] [--sample N]

  --sample N  从每种路由类型（A/B/C）各随机抽 N 题，默认 5（共 15 题）。
              设为 0 则用 --limit 控制总量。
  --limit N   当 --sample 0 时，取前 N 题（默认 30）。
  --output    输出 JSON 路径，默认 annotation_impact_results.json。

前置条件：
  1. Neo4j 中已有代码图（阶段 1）。
  2. 部分 Function 节点已有 annotation_json（跑过 annotate_functions.py）。
  3. .env 中配置了 OPENAI_API_KEY、LLM_MODEL。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# experiments/ 的父目录是 Code_Graph/，需加入 sys.path 以找到 run_qa、config 等模块
_CODE_GRAPH_DIR = Path(__file__).resolve().parent.parent
if str(_CODE_GRAPH_DIR) not in sys.path:
    sys.path.insert(0, str(_CODE_GRAPH_DIR))

ROOT = _CODE_GRAPH_DIR.parent
DEFAULT_QA_CSV = ROOT / "llama_cpp_QA.csv"
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "annotation_impact_results.json"


# ---------------------------------------------------------------------------
# 复用 run_qa 中的路由与检索逻辑
# ---------------------------------------------------------------------------
from run_qa import (
    _evidence_match,
    _get_file_paths_for_names,
    _normalize_entity,
    _parse_evidence,
    _query_type_a,
    _query_type_b,
    _query_type_c,
    _route,
)


def _get_annotations_for_names(driver, names: set[str], database: str) -> dict[str, dict]:
    """根据函数名集合查 Neo4j，返回 {name: annotation_dict}，仅含有注释的函数。"""
    if not names:
        return {}
    with driver.session(database=database) as session:
        r = session.run(
            """
            MATCH (f:Function)
            WHERE f.name IN $names AND f.annotation_json IS NOT NULL
            RETURN f.name AS name, f.annotation_json AS annotation_json
            """,
            names=list(names),
        )
        out = {}
        for rec in r:
            name = rec["name"]
            raw = rec.get("annotation_json")
            if raw:
                try:
                    out[name] = json.loads(raw) if isinstance(raw, str) else raw
                except Exception:
                    pass
        return out


def _enrich_retrieval_with_annotations(
    retrieval_text: str,
    names_involved: set[str],
    annotations: dict[str, dict],
) -> str:
    """在原始检索文本后追加函数注释信息。"""
    if not annotations:
        return retrieval_text

    ann_lines = ["\n\n--- 函数注释（由 annotate_functions 生成） ---"]
    for name in sorted(annotations.keys()):
        if name not in names_involved:
            continue
        ann = annotations[name]
        summary = ann.get("summary", "")
        role = ann.get("workflow_role", "")
        ctx = ann.get("invocation_context", [])
        ctx_str = ", ".join(ctx) if isinstance(ctx, list) else str(ctx)
        parts = [f"  [{name}]"]
        if summary:
            parts.append(f"    摘要: {summary}")
        if role:
            parts.append(f"    流程角色: {role}")
        if ctx_str:
            parts.append(f"    调用场景: {ctx_str}")
        ann_lines.append("\n".join(parts))

    if len(ann_lines) <= 1:
        return retrieval_text
    return retrieval_text + "\n".join(ann_lines)


# ---------------------------------------------------------------------------
# LLM 生成答案 & 评分（与 run_qa 类似，但独立以避免改动原文件）
# ---------------------------------------------------------------------------
_LLM_RETRIEVAL_MAX_CHARS = 4000


def _generate_answer(question: str, retrieval: str) -> tuple[str, int, int, float]:
    """返回 (answer, tokens_in, tokens_out, latency_ms)。"""
    from openai import OpenAI
    from config import LLM_MODEL, OPENAI_API_KEY, OPENAI_BASE_URL

    if not OPENAI_API_KEY:
        return "（未配置 OPENAI_API_KEY）", 0, 0, 0.0

    r = (retrieval or "（无检索结果）").strip()
    if len(r) > _LLM_RETRIEVAL_MAX_CHARS:
        r = r[:_LLM_RETRIEVAL_MAX_CHARS] + "\n...（已截断）"

    client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)
    prompt = f"""你是一个基于代码图回答仓库级问题的助手。下面给出「用户问题」和「从代码图中检索到的证据」（函数、文件路径、调用关系、函数注释等）。请仅根据检索证据用中文简洁回答问题；若证据不足，请明确说明并基于已有信息尽量推断。不要编造图中不存在的函数或调用关系。

【用户问题】
{question}

【代码图检索结果】
{r}

【你的回答】
"""
    t0 = time.time()
    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1500,
        )
        latency_ms = (time.time() - t0) * 1000
        tokens_in = resp.usage.prompt_tokens if resp.usage else 0
        tokens_out = resp.usage.completion_tokens if resp.usage else 0
        if resp.choices and resp.choices[0].message.content:
            return resp.choices[0].message.content.strip(), tokens_in, tokens_out, latency_ms
        return "（LLM 未返回内容）", tokens_in, tokens_out, latency_ms
    except Exception as e:
        return f"（LLM 调用失败: {e}）", 0, 0, (time.time() - t0) * 1000


def _evaluate_answer(question: str, reference: str, generated: str) -> tuple[int | None, str]:
    """二值评分：1 = 正确（关键信息覆盖充分），0 = 错误/不足。返回 (score, reason)。"""
    from openai import OpenAI
    from config import LLM_MODEL, OPENAI_API_KEY, OPENAI_BASE_URL

    if not OPENAI_API_KEY:
        return None, "（未配置 OPENAI_API_KEY）"

    ref = (reference or "").strip()[:2500]
    gen = (generated or "").strip()[:2500]

    client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)
    prompt = f"""请对比下面「参考答案」与「生成答案」，判断生成答案是否正确，只需回答 1 或 0，然后换行写一句简短理由。
- 1：生成答案覆盖了参考答案的关键信息，基本正确或高度互补；
- 0：生成答案关键信息缺失、严重错误，或与参考答案完全无关。
你必须先单独一行输出：判断: 1 或 判断: 0，然后换行写一句简短理由。不要输出其他格式。

【问题】
{question[:300]}

【参考答案】（截取部分）
{ref}

【生成答案】
{gen}

【你的输出】（第一行必须是「判断: 1」或「判断: 0」，第二行起为理由）
"""
    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
        )
        if not resp.choices or not resp.choices[0].message.content:
            return None, "（LLM 未返回评价）"
        text = resp.choices[0].message.content.strip()
        match = re.search(r"判断\s*[:：]\s*([01])\b", text)
        score = None
        if match:
            score = int(match.group(1))
        lines = text.split("\n")
        explanation = text
        for i, line in enumerate(lines):
            if re.search(r"判断\s*[:：]\s*", line):
                rest = "\n".join(lines[i + 1:]).strip()
                if rest:
                    explanation = rest
                break
        return score, explanation
    except Exception as e:
        return None, f"（评价失败: {e}）"


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def _process_one(
    idx: int,
    row_dict: dict,
    driver,
    database: str,
) -> dict:
    """对一道题分别跑无注释/有注释两个版本，返回对比结果。"""
    question = row_dict.get("具体问题", "")
    intent = row_dict.get("意图", "")
    entity = row_dict.get("实体名称", "")
    reference_answer = row_dict.get("答案", "")
    evidence_raw = row_dict.get("Evidence", "")
    route = _route(row_dict)

    # 1) 检索（与 run_qa 一致）
    if route == "A":
        retrieval, names_involved = _query_type_a(driver, entity, intent, question, database)
    elif route == "B":
        retrieval, names_involved = _query_type_b(driver, entity, question, database)
    else:
        retrieval, names_involved = _query_type_c(driver, entity, question, database)

    # 2) 查注释
    annotations = _get_annotations_for_names(driver, names_involved, database)
    n_annotated = len(annotations)

    # 3) 构造两种检索文本
    retrieval_plain = retrieval
    retrieval_enriched = _enrich_retrieval_with_annotations(retrieval, names_involved, annotations)

    # 4) 分别生成答案（含 token 数和延迟）
    answer_plain, tok_in_p, tok_out_p, lat_p = _generate_answer(question, retrieval_plain)
    answer_enriched, tok_in_e, tok_out_e, lat_e = _generate_answer(question, retrieval_enriched)

    # 5) 分别评分（二值）
    score_plain, expl_plain = _evaluate_answer(question, reference_answer, answer_plain)
    score_enriched, expl_enriched = _evaluate_answer(question, reference_answer, answer_enriched)

    # 6) 证据匹配
    retrieved_paths = _get_file_paths_for_names(driver, names_involved, database)
    evidence_set = _parse_evidence(evidence_raw)
    ev_match = _evidence_match(evidence_set, retrieved_paths)

    delta = None
    if score_plain is not None and score_enriched is not None:
        delta = score_enriched - score_plain

    return {
        "index": int(idx),
        "具体问题": question,
        "意图": intent,
        "实体名称": entity,
        "路由类型": route,
        "涉及函数数": len(names_involved),
        "有注释函数数": n_annotated,
        "检索结果_无注释": retrieval_plain[:800],
        "检索结果_有注释": retrieval_enriched[:1200],
        "生成答案_无注释": answer_plain,
        "生成答案_有注释": answer_enriched,
        "参考答案": reference_answer,
        "评分_无注释": score_plain,
        "评分说明_无注释": expl_plain,
        "评分_有注释": score_enriched,
        "评分说明_有注释": expl_enriched,
        "分数差(有-无)": delta,
        "证据匹配": ev_match,
        "tokens_in_无注释": tok_in_p,
        "tokens_out_无注释": tok_out_p,
        "latency_ms_无注释": round(lat_p, 1),
        "tokens_in_有注释": tok_in_e,
        "tokens_out_有注释": tok_out_e,
        "latency_ms_有注释": round(lat_e, 1),
    }


def _print_summary(results: list[dict]) -> str:
    """生成并返回对比摘要文本（二值评分版）。"""
    lines = ["=" * 70, "注释影响评估摘要（二值评分：1=正确 / 0=错误）", "=" * 70]

    valid = [r for r in results if r.get("评分_无注释") is not None and r.get("评分_有注释") is not None]
    if not valid:
        lines.append("无有效评分数据。")
        return "\n".join(lines)

    scores_plain = [r["评分_无注释"] for r in valid]
    scores_enriched = [r["评分_有注释"] for r in valid]
    deltas = [r["分数差(有-无)"] for r in valid if r.get("分数差(有-无)") is not None]

    acc_p = sum(scores_plain) / len(scores_plain)
    acc_e = sum(scores_enriched) / len(scores_enriched)
    avg_d = sum(deltas) / len(deltas) if deltas else 0

    n_improved = sum(1 for d in deltas if d > 0)
    n_same = sum(1 for d in deltas if d == 0)
    n_worse = sum(1 for d in deltas if d < 0)

    lines.append(f"评估题数: {len(valid)}")
    lines.append("")
    lines.append(f"{'指标':<20} {'无注释':>10} {'有注释':>10} {'差值':>10}")
    lines.append("-" * 55)
    lines.append(f"{'正确率':<20} {acc_p:>9.1%} {acc_e:>9.1%} {acc_e - acc_p:>+9.1%}")
    lines.append(f"{'正确题数':<20} {sum(scores_plain):>10} {sum(scores_enriched):>10} {sum(scores_enriched)-sum(scores_plain):>+10}")
    lines.append("")
    lines.append(f"提升: {n_improved} 题（0→1） | 持平: {n_same} 题 | 下降: {n_worse} 题（1→0）")

    # Token & latency 统计
    tok_in_p  = [r.get("tokens_in_无注释", 0) or 0 for r in valid]
    tok_out_p = [r.get("tokens_out_无注释", 0) or 0 for r in valid]
    lat_p     = [r.get("latency_ms_无注释", 0) or 0 for r in valid]
    tok_in_e  = [r.get("tokens_in_有注释", 0) or 0 for r in valid]
    tok_out_e = [r.get("tokens_out_有注释", 0) or 0 for r in valid]
    lat_e     = [r.get("latency_ms_有注释", 0) or 0 for r in valid]
    n = len(valid)
    lines.append("")
    lines.append(f"{'Token/延迟指标':<24} {'无注释':>12} {'有注释':>12} {'差值':>12}")
    lines.append("-" * 65)
    avg_tin_p  = sum(tok_in_p)  / n
    avg_tin_e  = sum(tok_in_e)  / n
    avg_tout_p = sum(tok_out_p) / n
    avg_tout_e = sum(tok_out_e) / n
    avg_lat_p  = sum(lat_p)     / n
    avg_lat_e  = sum(lat_e)     / n
    lines.append(f"{'输入 tokens 均值':<24} {avg_tin_p:>12.0f} {avg_tin_e:>12.0f} {avg_tin_e - avg_tin_p:>+12.0f}")
    lines.append(f"{'输出 tokens 均值':<24} {avg_tout_p:>12.0f} {avg_tout_e:>12.0f} {avg_tout_e - avg_tout_p:>+12.0f}")
    lines.append(f"{'延迟均值 (ms)':<24} {avg_lat_p:>12.0f} {avg_lat_e:>12.0f} {avg_lat_e - avg_lat_p:>+12.0f}")
    lines.append(f"{'总输入 tokens':<24} {sum(tok_in_p):>12} {sum(tok_in_e):>12} {sum(tok_in_e)-sum(tok_in_p):>+12}")
    lines.append(f"{'总输出 tokens':<24} {sum(tok_out_p):>12} {sum(tok_out_e):>12} {sum(tok_out_e)-sum(tok_out_p):>+12}")

    # 按路由类型分组
    by_route = {}
    for r in valid:
        t = r["路由类型"]
        by_route.setdefault(t, []).append(r)

    if len(by_route) > 1:
        lines.append("")
        lines.append(f"{'路由':<6} {'题数':>4} {'无注释正确率':>12} {'有注释正确率':>12} {'差值':>8} {'提升/持平/下降'}")
        lines.append("-" * 65)
        for route in sorted(by_route.keys()):
            group = by_route[route]
            sp = [r["评分_无注释"] for r in group]
            se = [r["评分_有注释"] for r in group]
            ds = [r["分数差(有-无)"] for r in group if r.get("分数差(有-无)") is not None]
            ap = sum(sp) / len(sp)
            ae = sum(se) / len(se)
            ni = sum(1 for d in ds if d > 0)
            ns_ = sum(1 for d in ds if d == 0)
            nw = sum(1 for d in ds if d < 0)
            lines.append(f"{route:<6} {len(group):>4} {ap:>11.1%} {ae:>11.1%} {ae-ap:>+8.1%} {ni}/{ns_}/{nw}")

    # 列出每题详情
    lines.append("")
    lines.append("逐题详情:")
    lines.append(f"{'#':<4} {'路由':<4} {'无':>3} {'有':>3} {'变化':>5} {'注释数':>6} {'输入tok(无/有)':>16} {'延迟ms(无/有)':>16}  问题(前40字)")
    lines.append("-" * 110)
    for r in valid:
        q = r["具体问题"][:40]
        sp = r["评分_无注释"]
        se = r["评分_有注释"]
        d = r.get("分数差(有-无)", 0) or 0
        na = r.get("有注释函数数", 0)
        tip = r.get("tokens_in_无注释", 0) or 0
        tie = r.get("tokens_in_有注释", 0) or 0
        lp  = r.get("latency_ms_无注释", 0) or 0
        le  = r.get("latency_ms_有注释", 0) or 0
        change = "+1" if d > 0 else ("-1" if d < 0 else " 0")
        lines.append(f"{r['index']:<4} {r['路由类型']:<4} {sp:>3} {se:>3} {change:>5} {na:>6} {tip:>7}/{tie:<7} {lp:>7.0f}/{le:<7.0f}  {q}")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="评估函数注释对 QA 质量的影响")
    parser.add_argument("--csv", type=Path, default=DEFAULT_QA_CSV, help="QA CSV 路径")
    parser.add_argument("--sample", type=int, default=5, help="每种路由类型各抽 N 题（默认 5，共约 15 题）；设为 0 则用 --limit")
    parser.add_argument("--limit", type=int, default=30, help="当 --sample 0 时取前 N 题")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="输出 JSON 路径")
    parser.add_argument("--workers", type=int, default=4, help="并行线程数（默认 4）")
    parser.add_argument("--prefer-annotated", action="store_true",
                        help="优先选涉及已有注释函数的题目（需连 Neo4j 预筛选）")
    args = parser.parse_args()

    if not args.csv.exists():
        print(f"错误：未找到 {args.csv}", file=sys.stderr)
        sys.exit(1)

    try:
        import pandas as pd
    except ImportError:
        print("请安装 pandas: pip install pandas", file=sys.stderr)
        sys.exit(1)

    from config import NEO4J_DATABASE
    from neo4j_writer import get_driver

    df = pd.read_csv(args.csv, encoding="utf-8")

    driver = get_driver()
    try:
        driver.verify_connectivity()
    except Exception as e:
        print(f"Neo4j 连接失败: {e}", file=sys.stderr)
        sys.exit(1)

    # 选题
    if args.sample > 0:
        rows_all = [(int(idx), row.to_dict()) for idx, row in df.iterrows()]
        import random
        random.seed(42)

        # --prefer-annotated: 预筛选涉及已有注释函数的题目
        if args.prefer_annotated:
            print("预筛选涉及已注释函数的题目...")
            annotated_names = set()
            with driver.session(database=NEO4J_DATABASE) as session:
                r = session.run(
                    "MATCH (f:Function) WHERE f.annotation_json IS NOT NULL RETURN f.name AS name"
                )
                annotated_names = {rec["name"] for rec in r}
            print(f"  已注释函数名: {len(annotated_names)} 个")

            # 对每道题，检查实体名称是否能匹配到已注释函数
            preferred = []
            rest = []
            for idx, row_dict in rows_all:
                entity = _normalize_entity(row_dict.get("实体名称", ""))
                if not entity:
                    rest.append((idx, row_dict))
                    continue
                # 检查实体是否为已注释函数名，或已注释函数名包含实体
                matched = any(
                    entity in name or name in entity or entity == name
                    for name in annotated_names
                )
                if matched:
                    preferred.append((idx, row_dict))
                else:
                    rest.append((idx, row_dict))
            print(f"  实体直接匹配已注释函数的题目: {len(preferred)} 题")

            # 按路由类型分组，优先从 preferred 中抽
            by_route_pref: dict[str, list] = {}
            by_route_rest: dict[str, list] = {}
            for idx, row_dict in preferred:
                route = _route(row_dict)
                by_route_pref.setdefault(route, []).append((idx, row_dict))
            for idx, row_dict in rest:
                route = _route(row_dict)
                by_route_rest.setdefault(route, []).append((idx, row_dict))

            selected = []
            all_routes = sorted(set(list(by_route_pref.keys()) + list(by_route_rest.keys())))
            for route in all_routes:
                pool_pref = by_route_pref.get(route, [])
                pool_rest = by_route_rest.get(route, [])
                k = args.sample
                picked = []
                if len(pool_pref) >= k:
                    picked = random.sample(pool_pref, k)
                else:
                    picked = list(pool_pref)
                    need = k - len(picked)
                    if need > 0 and pool_rest:
                        picked.extend(random.sample(pool_rest, min(need, len(pool_rest))))
                selected.extend(picked)
            selected.sort(key=lambda x: x[0])
            rows = selected
            print(f"按路由类型各抽 {args.sample} 题（优先已注释），共 {len(rows)} 题")
        else:
            # 普通随机抽样
            by_route: dict[str, list] = {}
            for idx, row_dict in rows_all:
                route = _route(row_dict)
                by_route.setdefault(route, []).append((idx, row_dict))
            selected = []
            for route in sorted(by_route.keys()):
                pool = by_route[route]
                k = min(args.sample, len(pool))
                selected.extend(random.sample(pool, k))
            selected.sort(key=lambda x: x[0])
            rows = selected
            print(f"按路由类型各抽 {args.sample} 题，共 {len(rows)} 题")
    else:
        if args.limit > 0:
            df = df.head(args.limit)
        rows = [(int(idx), row.to_dict()) for idx, row in df.iterrows()]
        print(f"取前 {len(rows)} 题")

    # 先统计有多少函数已有注释
    with driver.session(database=NEO4J_DATABASE) as session:
        r = session.run("MATCH (f:Function) WHERE f.annotation_json IS NOT NULL RETURN count(f) AS cnt")
        n_annotated_total = r.single()["cnt"]
        r2 = session.run("MATCH (f:Function) RETURN count(f) AS cnt")
        n_func_total = r2.single()["cnt"]
    print(f"Neo4j 中函数总数: {n_func_total}，已有注释: {n_annotated_total} ({100*n_annotated_total/max(n_func_total,1):.1f}%)")

    if n_annotated_total == 0:
        print("警告：没有任何函数有注释，请先运行 annotate_functions.py", file=sys.stderr)
        print("继续运行（有注释版本将与无注释版本相同）...")

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_to_idx = {
            executor.submit(_process_one, idx, row_dict, driver, NEO4J_DATABASE): (i, idx, row_dict)
            for i, (idx, row_dict) in enumerate(rows)
        }
        completed = 0
        for future in as_completed(future_to_idx):
            i, idx, row_dict = future_to_idx[future]
            completed += 1
            print(f"[{completed}/{len(rows)}] 完成题目 {idx}: {row_dict.get('具体问题', '')[:50]}...")
            try:
                results.append(future.result())
            except Exception as e:
                print(f"  异常: {e}", file=sys.stderr)
                results.append({"index": idx, "具体问题": row_dict.get("具体问题", ""), "错误": str(e)})
    results.sort(key=lambda x: x.get("index", 0))

    driver.close()

    # 写 JSON
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n结果已写入: {args.output}")

    # 打印摘要
    summary = _print_summary(results)
    print(summary)

    # 也写一份摘要到 md
    summary_path = args.output.with_suffix(".md")
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(summary)
    print(f"摘要已写入: {summary_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
