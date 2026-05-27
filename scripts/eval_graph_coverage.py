import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT / "src"))   # 核心库
sys.path.insert(0, str(_ROOT))             # 根目录（config.py）

#!/usr/bin/env python3
"""
图质量评估：Evidence 文件在图中的覆盖率、实体名称在图中的可匹配性。
用于区分「图里没有」与「检索没找到」。不依赖 qa_retrieval_results.json。

用法：
  python eval_graph_coverage.py [--csv PATH] [--limit N] [--output PATH]
  --output 默认为 Code_Graph/graph_coverage_report.md
"""
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CSV = ROOT / "llama_cpp_QA.csv"
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "graph_coverage_report.md"


def _parse_evidence(evidence_str: str) -> set[str]:
    """解析 Evidence 列：支持 path, path 与 path:行号; path2:行号 或 path:行号1,行号2；过滤纯数字（误拆出的行号）。与 investigate_graph_scope 一致。"""
    if not evidence_str or not isinstance(evidence_str, str):
        return set()
    out = set()
    for segment in evidence_str.split(","):
        segment = segment.strip().strip('"').replace("\\", "/")
        if not segment:
            continue
        for part in segment.split("; "):
            part = part.strip()
            if not part:
                continue
            if ":" in part:
                left, right = part.rsplit(":", 1)
                right = right.strip()
                if right and all(t.strip().isdigit() for t in right.split(",")):
                    part = left.strip()
            if not part or part.isdigit():
                continue
            if "/" in part or part.endswith((".cpp", ".h", ".c", ".hpp", ".cc", ".cxx")):
                out.add(part)
    return out


def _normalize_path_for_match(p: str) -> str:
    return (p or "").strip().replace("\\", "/").rstrip("/")


def _path_matches(e: str, r: str) -> bool:
    """Evidence 路径 e 与图中路径 r 是否视为同一文件。"""
    if not e or not r:
        return False
    if e == r:
        return True
    if r.endswith("/" + e) or r.endswith(e):
        return True
    if e.endswith("/" + r) or e.endswith(r):
        return True
    return False


def _normalize_entity(entity: str) -> str:
    """与 run_qa 一致：将实体名称转为图中可用的 file_path 或 name 片段。"""
    if not entity:
        return ""
    e = entity.strip()
    if e.endswith(".cpp") or e.endswith(".h") or "/" in e:
        return e.replace("\\", "/")
    return e


def _get_all_graph_file_paths(driver, database: str) -> set[str]:
    """Neo4j 中所有 Function 的 file_path 去重。"""
    with driver.session(database=database) as session:
        r = session.run(
            "MATCH (f:Function) RETURN DISTINCT f.file_path AS file_path",
        )
        return {rec["file_path"] for rec in r if rec.get("file_path")}


def _count_evidence_matched(evidence_set: set[str], graph_paths: set[str]) -> int:
    """Evidence 中有多少条路径能在 graph_paths 中匹配到至少一个。"""
    if not evidence_set:
        return 0
    n = 0
    for e in evidence_set:
        en = _normalize_path_for_match(e)
        if not en:
            continue
        for g in graph_paths:
            if _path_matches(en, g):
                n += 1
                break
    return n


def _entity_matches_graph(driver, entity: str, database: str) -> bool:
    """与 run_qa 类型 A 一致：图中是否存在至少一个 Function 的 file_path 或 name 包含实体。"""
    path_part = _normalize_entity(entity)
    if not path_part:
        return False
    with driver.session(database=database) as session:
        r = session.run(
            """
            MATCH (f:Function)
            WHERE f.file_path CONTAINS $path OR f.name CONTAINS $path
            RETURN f.name AS name
            LIMIT 1
            """,
            path=path_part,
        )
        return r.single() is not None


def main():
    parser = argparse.ArgumentParser(description="图质量评估：Evidence 覆盖率与实体命中率")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="QA CSV 路径")
    parser.add_argument("--limit", type=int, default=0, help="只评估前 N 行，0 表示全部")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="报告输出路径")
    args = parser.parse_args()

    import pandas as pd
    from config import NEO4J_DATABASE
    from neo4j_writer import get_driver

    if not args.csv.exists():
        print(f"CSV 不存在: {args.csv}")
        return 1

    df = pd.read_csv(args.csv, encoding="utf-8")
    if args.limit > 0:
        df = df.head(args.limit)
    rows = df.to_dict("records")
    n_rows = len(rows)

    driver = get_driver()
    try:
        driver.verify_connectivity()
    except Exception as e:
        print(f"Neo4j 连接失败: {e}")
        return 1

    print("正在加载图中所有 file_path...")
    graph_paths = _get_all_graph_file_paths(driver, NEO4J_DATABASE)
    print(f"图中共有 {len(graph_paths)} 个不同的 file_path。")

    # 逐题：Evidence 覆盖率、实体是否命中
    evidence_coverage_list = []  # 每题 (evidence_count, matched_count)
    entity_hit_list = []  # 每题 bool
    row_meta = []  # 每题 问题类型、一级分类

    for i, row in enumerate(rows):
        evidence_set = _parse_evidence(row.get("Evidence", "") or "")
        entity = (row.get("实体名称") or "").strip()
        q_type = (row.get("问题类型") or "").strip()
        cat1 = (row.get("一级分类") or "").strip()

        n_ev = len(evidence_set)
        n_matched = _count_evidence_matched(evidence_set, graph_paths) if evidence_set else 0
        evidence_coverage_list.append((n_ev, n_matched))
        row_meta.append((q_type, cat1))

        hit = _entity_matches_graph(driver, entity, NEO4J_DATABASE)
        entity_hit_list.append(hit)

        if (i + 1) % 100 == 0:
            print(f"  已处理 {i + 1}/{n_rows} 题...")

    driver.close()

    # 汇总
    total_ev = sum(n_ev for n_ev, _ in evidence_coverage_list)
    total_matched = sum(n_m for _, n_m in evidence_coverage_list)
    per_question_coverages = [
        n_m / n_ev if n_ev else 0.0
        for n_ev, n_m in evidence_coverage_list
    ]
    mean_coverage = sum(per_question_coverages) / len(per_question_coverages) if per_question_coverages else 0.0
    overall_ev_coverage = total_matched / total_ev if total_ev else 0.0
    entity_hit_rate = sum(entity_hit_list) / len(entity_hit_list) if entity_hit_list else 0.0

    # 按问题类型
    by_qtype = {}
    for (q_type, _), (n_ev, n_m), hit in zip(row_meta, evidence_coverage_list, entity_hit_list):
        t = q_type or "（未知）"
        if t not in by_qtype:
            by_qtype[t] = {"n": 0, "ev_total": 0, "ev_matched": 0, "entity_hits": 0}
        by_qtype[t]["n"] += 1
        by_qtype[t]["ev_total"] += n_ev
        by_qtype[t]["ev_matched"] += n_m
        if hit:
            by_qtype[t]["entity_hits"] += 1

    # 按一级分类
    by_cat1 = {}
    for (_, cat1), (n_ev, n_m), hit in zip(row_meta, evidence_coverage_list, entity_hit_list):
        c = cat1 or "（未知）"
        if c not in by_cat1:
            by_cat1[c] = {"n": 0, "ev_total": 0, "ev_matched": 0, "entity_hits": 0}
        by_cat1[c]["n"] += 1
        by_cat1[c]["ev_total"] += n_ev
        by_cat1[c]["ev_matched"] += n_m
        if hit:
            by_cat1[c]["entity_hits"] += 1

    # 输出 Markdown 报告
    lines = [
        "# 图质量评估报告",
        "",
        f"数据来源：`{args.csv.name}`，共 {n_rows} 题。",
        f"图中 Function 的 file_path 去重数：{len(graph_paths)}。",
        "",
        "## 1. Evidence 文件在图中的覆盖率",
        "",
        "- **按题平均**：每题 Evidence 路径中能在图中匹配到的比例，再对所有题求平均。",
        f"  - 平均覆盖率：{mean_coverage:.4f}",
        "- **按路径汇总**：全部题目的 Evidence 路径合并后，有多少比例在图中存在。",
        f"  - Evidence 路径总数：{total_ev}",
        f"  - 图中能匹配到的路径数：{total_matched}",
        f"  - 覆盖率：{overall_ev_coverage:.4f}",
        "",
        "## 2. 实体名称在图中的可匹配性",
        "",
        "- **实体命中率**：实体名称能在图中匹配到至少一个 Function（file_path 或 name 包含实体）的题目占比。",
        f"  - 命中题数：{sum(entity_hit_list)} / {len(entity_hit_list)}",
        f"  - 命中率：{entity_hit_rate:.4f}（{100 * entity_hit_rate:.1f}%）",
        "",
        "## 3. 按问题类型",
        "",
        "| 问题类型 | 题数 | Evidence 路径总数 | 图中匹配数 | Evidence 覆盖率 | 实体命中数 | 实体命中率 |",
        "|----------|------|------------------|------------|-----------------|------------|------------|",
    ]
    for t in sorted(by_qtype.keys()):
        d = by_qtype[t]
        cov = d["ev_matched"] / d["ev_total"] if d["ev_total"] else 0.0
        ent_rate = d["entity_hits"] / d["n"] if d["n"] else 0.0
        lines.append(
            f"| {t} | {d['n']} | {d['ev_total']} | {d['ev_matched']} | {cov:.4f} | {d['entity_hits']} | {ent_rate:.4f} |"
        )

    lines.extend([
        "",
        "## 4. 按一级分类",
        "",
        "| 一级分类 | 题数 | Evidence 路径总数 | 图中匹配数 | Evidence 覆盖率 | 实体命中数 | 实体命中率 |",
        "|----------|------|------------------|------------|-----------------|------------|------------|",
    ])
    for c in sorted(by_cat1.keys()):
        d = by_cat1[c]
        cov = d["ev_matched"] / d["ev_total"] if d["ev_total"] else 0.0
        ent_rate = d["entity_hits"] / d["n"] if d["n"] else 0.0
        lines.append(
            f"| {c} | {d['n']} | {d['ev_total']} | {d['ev_matched']} | {cov:.4f} | {d['entity_hits']} | {ent_rate:.4f} |"
        )

    lines.extend([
        "",
        "---",
        "",
        "说明：Evidence 覆盖率 = 图中存在至少一个 Function 的 file_path 能与该 Evidence 路径匹配的路径数 / 该题 Evidence 路径数。",
        "实体命中率 = 该组内实体能在图中匹配到至少一个 Function 的题目数 / 该组题数。",
        "与 run_qa 使用相同的路径匹配与实体归一化逻辑。",
        "",
    ])

    out_text = "\n".join(lines)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(out_text, encoding="utf-8")
    print(out_text)
    print(f"\n已写入：{args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
