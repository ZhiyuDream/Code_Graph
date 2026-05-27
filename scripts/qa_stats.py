import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT / "src"))   # 核心库
sys.path.insert(0, str(_ROOT))             # 根目录（config.py）

#!/usr/bin/env python3
"""从 qa_retrieval_results.json 生成统计指标，输出到 qa_retrieval_stats.md。需同目录或上游的 llama_cpp_QA.csv 以按问题类型统计。"""
import json
from pathlib import Path

PATH_JSON = Path(__file__).resolve().parent / "qa_retrieval_results.json"
PATH_CSV = Path(__file__).resolve().parent.parent / "llama_cpp_QA.csv"
PATH_OUT = Path(__file__).resolve().parent / "qa_retrieval_stats.md"


def main():
    with open(PATH_JSON, "r", encoding="utf-8") as f:
        data = json.load(f)

    n = len(data)
    has_error = [r for r in data if r.get("错误")]
    valid = [r for r in data if not r.get("错误")]

    # 从 CSV 读入问题类型、一级分类、二级分类（按行号/index 对齐）
    index_to_type = {}
    index_to_cat1 = {}
    index_to_cat2 = {}
    if PATH_CSV.exists():
        try:
            import pandas as pd
            df_csv = pd.read_csv(PATH_CSV, encoding="utf-8")
            for idx, row in df_csv.iterrows():
                index_to_type[int(idx)] = row.get("问题类型", "")
                index_to_cat1[int(idx)] = row.get("一级分类", "")
                index_to_cat2[int(idx)] = row.get("二级分类", "")
        except Exception:
            pass
    for r in valid:
        i = r.get("index")
        r["_问题类型"] = index_to_type.get(i, "")
        r["_一级分类"] = index_to_cat1.get(i, "")
        r["_二级分类"] = index_to_cat2.get(i, "")

    # 路由类型分布
    by_route = {}
    for r in valid:
        t = r.get("路由类型", "?")
        by_route[t] = by_route.get(t, 0) + 1

    # 评价分数（仅含有效数值）
    scores = [r["评价分数"] for r in valid if "评价分数" in r and r["评价分数"] is not None]
    n_eval = len(scores)

    def mean(xs):
        return sum(xs) / len(xs) if xs else None

    def pct(p, xs):
        if not xs:
            return None
        xs = sorted(xs)
        i = max(0, int(len(xs) * p / 100) - 1)
        return xs[i]

    # 证据匹配
    recall_list = []
    prec_list = []
    hit_list = []
    ev_list = []
    ret_list = []
    for r in valid:
        em = r.get("证据匹配") or {}
        rec = em.get("证据召回率")
        prec = em.get("证据精确率")
        hit = em.get("命中Evidence文件数")
        ev = em.get("Evidence文件数")
        ret = em.get("检索涉及文件数")
        if rec is not None:
            recall_list.append(rec)
        if prec is not None:
            prec_list.append(prec)
        if hit is not None:
            hit_list.append(hit)
        if ev is not None:
            ev_list.append(ev)
        if ret is not None:
            ret_list.append(ret)

    lines = [
        "# QA 检索与生成结果统计",
        "",
        f"数据来源：`{PATH_JSON.name}`",
        "",
        "## 1. 样本量",
        f"- 总题数：{n}",
        f"- 含错误/异常：{len(has_error)}",
        f"- 有效结果：{len(valid)}",
        "",
        "## 2. 路由类型分布",
        "| 路由类型 | 题数 | 占比 |",
        "|----------|------|------|",
    ]
    for t in sorted(by_route.keys()):
        cnt = by_route[t]
        pct_str = f"{100 * cnt / len(valid):.1f}%" if valid else "—"
        lines.append(f"| {t} | {cnt} | {pct_str} |")

    lines.extend([
        "",
        "## 3. LLM 评价分数（0–1）",
    ])
    if n_eval:
        n_ge50 = sum(1 for s in scores if s >= 0.5)
        n_ge60 = sum(1 for s in scores if s >= 0.6)
        lines.extend([
            f"- 有分数题数：{n_eval}",
            f"- 平均分：{mean(scores):.4f}",
            f"- 中位数（P50）：{pct(50, scores):.4f}",
            f"- P25：{pct(25, scores):.4f}，P75：{pct(75, scores):.4f}",
            f"- 最小值：{min(scores):.4f}，最大值：{max(scores):.4f}",
            f"- **总体正确率（评价分数≥0.5）**：{n_ge50}/{n_eval} = {100 * n_ge50 / n_eval:.1f}%",
            f"- **总体正确率（评价分数≥0.6）**：{n_ge60}/{n_eval} = {100 * n_ge60 / n_eval:.1f}%",
            "",
            "分数区间分布：",
        ])
        bins = [(0, 0.3), (0.3, 0.5), (0.5, 0.7), (0.7, 1.0)]
        for lo, hi in bins:
            cnt = sum(1 for s in scores if lo <= s < hi or (hi == 1.0 and s == 1.0))
            lines.append(f"- [{lo}, {hi})：{cnt} 题（{100 * cnt / n_eval:.1f}%）")
    else:
        lines.append("- 无评价分数（未使用 --eval）。")

    lines.extend([
        "",
        "## 4. 证据匹配（检索涉及文件 vs Evidence）",
    ])
    if recall_list:
        lines.extend([
            f"- 证据召回率（命中 Evidence 文件数 / Evidence 文件数）",
            f"  - 平均：{mean(recall_list):.4f}",
            f"  - 中位数：{pct(50, recall_list):.4f}",
            f"- 证据精确率（命中数 / 检索涉及文件数）",
            f"  - 平均：{mean(prec_list):.4f}",
            f"  - 中位数：{pct(50, prec_list):.4f}",
            f"- 命中 Evidence 文件数：总合 {sum(hit_list)}，平均每题 {mean(hit_list):.2f}",
            f"- Evidence 文件数：平均每题 {mean(ev_list):.2f}",
            f"- 检索涉及文件数：平均每题 {mean(ret_list):.2f}",
        ])
    else:
        lines.append("- 无证据匹配数据。")

    # 5. 按问题类型（CSV 列）统计正确率与证据指标
    if index_to_type and valid:
        lines.extend(["", "## 5. 按问题类型统计"])
        by_qtype = {}
        for r in valid:
            t = r.get("_问题类型", "") or "（未知）"
            if t not in by_qtype:
                by_qtype[t] = []
            by_qtype[t].append(r)
        lines.append("")
        lines.append("| 问题类型 | 题数 | 平均评价分 | 正确率(≥0.5) | 正确率(≥0.6) | 平均证据召回率 | 平均证据精确率 |")
        lines.append("|----------|------|------------|--------------|--------------|----------------|----------------|")
        for qtype in sorted(by_qtype.keys()):
            group = by_qtype[qtype]
            scores_g = [x["评价分数"] for x in group if "评价分数" in x and x["评价分数"] is not None]
            recall_g = [x.get("证据匹配", {}).get("证据召回率") for x in group if x.get("证据匹配", {}).get("证据召回率") is not None]
            prec_g = [x.get("证据匹配", {}).get("证据精确率") for x in group if x.get("证据匹配", {}).get("证据精确率") is not None]
            n_g = len(group)
            avg_score = mean(scores_g) if scores_g else None
            acc50 = 100 * sum(1 for s in scores_g if s >= 0.5) / len(scores_g) if scores_g else None
            acc60 = 100 * sum(1 for s in scores_g if s >= 0.6) / len(scores_g) if scores_g else None
            avg_rec = mean(recall_g) if recall_g else None
            avg_prec = mean(prec_g) if prec_g else None
            avg_score_str = f"{avg_score:.4f}" if avg_score is not None else "—"
            acc50_str = f"{acc50:.1f}%" if acc50 is not None else "—"
            acc60_str = f"{acc60:.1f}%" if acc60 is not None else "—"
            avg_rec_str = f"{avg_rec:.4f}" if avg_rec is not None else "—"
            avg_prec_str = f"{avg_prec:.4f}" if avg_prec is not None else "—"
            lines.append(
                f"| {qtype} | {n_g} | {avg_score_str} | "
                f"{acc50_str} | {acc60_str} | "
                f"{avg_rec_str} | {avg_prec_str} |"
            )

        lines.extend(["", "## 6. 按一级分类统计"])
        by_cat1 = {}
        for r in valid:
            c = r.get("_一级分类", "") or "（未知）"
            if c not in by_cat1:
                by_cat1[c] = []
            by_cat1[c].append(r)
        lines.append("")
        lines.append("| 一级分类 | 题数 | 平均评价分 | 正确率(≥0.5) | 正确率(≥0.6) | 平均证据召回率 | 平均证据精确率 |")
        lines.append("|----------|------|------------|--------------|--------------|----------------|----------------|")
        for c in sorted(by_cat1.keys()):
            group = by_cat1[c]
            scores_g = [x["评价分数"] for x in group if "评价分数" in x and x["评价分数"] is not None]
            recall_g = [x.get("证据匹配", {}).get("证据召回率") for x in group if x.get("证据匹配", {}).get("证据召回率") is not None]
            prec_g = [x.get("证据匹配", {}).get("证据精确率") for x in group if x.get("证据匹配", {}).get("证据精确率") is not None]
            n_g = len(group)
            avg_score = mean(scores_g) if scores_g else None
            acc50 = 100 * sum(1 for s in scores_g if s >= 0.5) / len(scores_g) if scores_g else None
            acc60 = 100 * sum(1 for s in scores_g if s >= 0.6) / len(scores_g) if scores_g else None
            avg_rec = mean(recall_g) if recall_g else None
            avg_prec = mean(prec_g) if prec_g else None
            avg_score_str = f"{avg_score:.4f}" if avg_score is not None else "—"
            acc50_str = f"{acc50:.1f}%" if acc50 is not None else "—"
            acc60_str = f"{acc60:.1f}%" if acc60 is not None else "—"
            avg_rec_str = f"{avg_rec:.4f}" if avg_rec is not None else "—"
            avg_prec_str = f"{avg_prec:.4f}" if avg_prec is not None else "—"
            lines.append(
                f"| {c} | {n_g} | {avg_score_str} | "
                f"{acc50_str} | {acc60_str} | "
                f"{avg_rec_str} | {avg_prec_str} |"
            )
    else:
        lines.extend(["", "## 5. 按问题类型统计", "", "（未找到 llama_cpp_QA.csv 或无有效数据，跳过按类型统计）"])

    # 7. 按路由类型统计（A/B/C 的证据召回、精确率与正确率，不依赖 CSV）
    if valid:
        lines.extend(["", "## 7. 按路由类型统计"])
        by_route_list = {}
        for r in valid:
            t = r.get("路由类型", "?")
            by_route_list.setdefault(t, []).append(r)
        lines.append("")
        lines.append("| 路由类型 | 题数 | 平均评价分 | 正确率(≥0.5) | 正确率(≥0.6) | 平均证据召回率 | 平均证据精确率 |")
        lines.append("|----------|------|------------|--------------|--------------|----------------|----------------|")
        for route in sorted(by_route_list.keys()):
            group = by_route_list[route]
            scores_g = [x["评价分数"] for x in group if "评价分数" in x and x["评价分数"] is not None]
            recall_g = [x.get("证据匹配", {}).get("证据召回率") for x in group if x.get("证据匹配", {}).get("证据召回率") is not None]
            prec_g = [x.get("证据匹配", {}).get("证据精确率") for x in group if x.get("证据匹配", {}).get("证据精确率") is not None]
            n_g = len(group)
            avg_score = mean(scores_g) if scores_g else None
            acc50 = 100 * sum(1 for s in scores_g if s >= 0.5) / len(scores_g) if scores_g else None
            acc60 = 100 * sum(1 for s in scores_g if s >= 0.6) / len(scores_g) if scores_g else None
            avg_rec = mean(recall_g) if recall_g else None
            avg_prec = mean(prec_g) if prec_g else None
            avg_score_str = f"{avg_score:.4f}" if avg_score is not None else "—"
            acc50_str = f"{acc50:.1f}%" if acc50 is not None else "—"
            acc60_str = f"{acc60:.1f}%" if acc60 is not None else "—"
            avg_rec_str = f"{avg_rec:.4f}" if avg_rec is not None else "—"
            avg_prec_str = f"{avg_prec:.4f}" if avg_prec is not None else "—"
            lines.append(
                f"| {route} | {n_g} | {avg_score_str} | "
                f"{acc50_str} | {acc60_str} | "
                f"{avg_rec_str} | {avg_prec_str} |"
            )

    lines.append("")
    out = "\n".join(lines)
    with open(PATH_OUT, "w", encoding="utf-8") as f:
        f.write(out)
    print(out)
    print(f"\n已写入：{PATH_OUT}")


if __name__ == "__main__":
    main()
