import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT / "src"))   # 核心库
sys.path.insert(0, str(_ROOT))             # 根目录（config.py）

#!/usr/bin/env python3
"""
调查建图范围与 Evidence 的差异：路径格式、文件类型（.h vs .cpp）、目录分布。
帮助判断「图里本来就没有」的原因：compile_commands 范围、是否含 .h、解析失败等。

用法：python investigate_graph_scope.py [--csv PATH] [--output PATH]
"""
import argparse
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CSV = ROOT / "llama_cpp_QA.csv"
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "graph_scope_investigation.md"


def _parse_evidence(evidence_str: str) -> set[str]:
    """解析 Evidence 列：支持 path, path 与 path:行号; path2:行号 或 path:行号1,行号2 等格式；过滤纯数字（误拆出的行号）。"""
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


def _top_dir(p: str) -> str:
    """路径的第一个成分，如 src/llama.cpp -> src。"""
    p = (p or "").strip().lstrip("/")
    if not p:
        return "（空）"
    parts = p.replace("\\", "/").split("/")
    return parts[0] if parts else "（空）"


def _suffix(p: str) -> str:
    s = Path(p).suffix.lower()
    return s if s else "（无后缀）"


def analyze_paths(paths: set[str], label: str) -> dict:
    """统计路径的后缀分布与顶层目录分布。"""
    by_suffix = defaultdict(int)
    by_dir = defaultdict(int)
    for p in paths:
        if not p or not p.strip():
            continue
        by_suffix[_suffix(p)] += 1
        by_dir[_top_dir(p)] += 1
    return {
        "label": label,
        "total": len(paths),
        "by_suffix": dict(by_suffix),
        "by_dir": dict(by_dir),
    }


def get_evidence_paths_from_csv(csv_path: Path) -> set[str]:
    import pandas as pd
    df = pd.read_csv(csv_path, encoding="utf-8")
    out = set()
    for _, row in df.iterrows():
        out |= _parse_evidence(row.get("Evidence", "") or "")
    return out


def get_compile_commands_paths(build_dir: Path, repo_root: Path | None) -> set[str]:
    """从 compile_commands.json 读取所有 C/C++ 源文件路径；若 repo_root 存在则转为相对路径。"""
    import os
    path = build_dir / "compile_commands.json"
    if not path.exists():
        return set()
    try:
        with open(path, encoding="utf-8") as f:
            data = __import__("json").load(f)
    except Exception:
        return set()
    out = set()
    for entry in data:
        fpath = entry.get("file", "")
        if not fpath:
            continue
        if not os.path.isabs(fpath):
            cwd = entry.get("directory", "")
            fpath = os.path.normpath(os.path.join(cwd, fpath))
        if repo_root and fpath.startswith(str(repo_root)):
            fpath = os.path.relpath(fpath, repo_root)
        fpath = fpath.replace("\\", "/")
        out.add(fpath)
    return out


def get_graph_paths(driver, database: str) -> set[str]:
    with driver.session(database=database) as session:
        r = session.run(
            "MATCH (f:Function) RETURN DISTINCT f.file_path AS file_path"
        )
        return {rec["file_path"] for rec in r if rec.get("file_path")}


def main():
    parser = argparse.ArgumentParser(description="调查建图范围与 Evidence 路径差异")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="QA CSV")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="输出 Markdown 路径")
    args = parser.parse_args()

    lines = ["# 建图范围调查", ""]

    # 1. Evidence 路径
    if not args.csv.exists():
        lines.append(f"CSV 不存在: {args.csv}")
        args.output.write_text("\n".join(lines), encoding="utf-8")
        print("\n".join(lines))
        return 0
    evidence_paths = get_evidence_paths_from_csv(args.csv)
    ev_stats = analyze_paths(evidence_paths, "Evidence")
    lines.append("## 1. Evidence 中的路径（全题去重）")
    lines.append(f"- 去重后路径数：{ev_stats['total']}")
    lines.append("- 按后缀：")
    for suf, cnt in sorted(ev_stats["by_suffix"].items(), key=lambda x: -x[1]):
        lines.append(f"  - `{suf}`: {cnt}")
    lines.append("- 按顶层目录：")
    for d, cnt in sorted(ev_stats["by_dir"].items(), key=lambda x: -x[1])[:20]:
        lines.append(f"  - `{d}/`: {cnt}")
    if len(ev_stats["by_dir"]) > 20:
        lines.append(f"  - ... 共 {len(ev_stats['by_dir'])} 个顶层目录")
    lines.append("")

    # 2. compile_commands.json
    try:
        from config import get_compile_commands_path, get_repo_root
        build_dir = get_compile_commands_path()
        repo_root = get_repo_root()
    except Exception:
        build_dir = None
        repo_root = None

    if build_dir and (build_dir / "compile_commands.json").exists():
        cc_paths = get_compile_commands_paths(build_dir, repo_root)
        cc_stats = analyze_paths(cc_paths, "compile_commands")
        lines.append("## 2. compile_commands.json 中的路径")
        lines.append(f"- 去重后路径数：{cc_stats['total']}")
        lines.append("- 按后缀：")
        for suf, cnt in sorted(cc_stats["by_suffix"].items(), key=lambda x: -x[1]):
            lines.append(f"  - `{suf}`: {cnt}")
        lines.append("- 按顶层目录：")
        for d, cnt in sorted(cc_stats["by_dir"].items(), key=lambda x: -x[1])[:20]:
            lines.append(f"  - `{d}/`: {cnt}")
        if len(cc_stats["by_dir"]) > 20:
            lines.append(f"  - ... 共 {len(cc_stats['by_dir'])} 个顶层目录")
        lines.append("")
    else:
        cc_paths = set()
        cc_stats = None
        lines.append("## 2. compile_commands.json")
        lines.append("- 未找到或未配置（REPO_ROOT/COMPILE_COMMANDS_DIR）。跳过。")
        lines.append("")

    # 3. 图中路径
    graph_paths = set()
    g_stats = None
    try:
        from config import NEO4J_DATABASE
        from neo4j_writer import get_driver
        driver = get_driver()
        driver.verify_connectivity()
        graph_paths = get_graph_paths(driver, NEO4J_DATABASE)
        driver.close()
        g_stats = analyze_paths(graph_paths, "图")
        lines.append("## 3. 图中 Function 的 file_path（Neo4j）")
        lines.append(f"- 去重后路径数：{g_stats['total']}")
        lines.append("- 按后缀：")
        for suf, cnt in sorted(g_stats["by_suffix"].items(), key=lambda x: -x[1]):
            lines.append(f"  - `{suf}`: {cnt}")
        lines.append("- 按顶层目录：")
        for d, cnt in sorted(g_stats["by_dir"].items(), key=lambda x: -x[1])[:20]:
            lines.append(f"  - `{d}/`: {cnt}")
        if len(g_stats["by_dir"]) > 20:
            lines.append(f"  - ... 共 {len(g_stats['by_dir'])} 个顶层目录")
        lines.append("")
    except Exception as e:
        lines.append("## 3. 图中路径")
        lines.append(f"- Neo4j 不可用或查询失败: {e}")
        lines.append("")

    # 3.5 路径抽样（若图可用）
    if graph_paths and evidence_paths:
        # Evidence 中有但图中没有的路径（按后缀抽样，便于看 .h 是否大量未覆盖）
        ev_norm = {p.replace("\\", "/").strip() for p in evidence_paths if p}
        missing = ev_norm - graph_paths
        # 也做「图中路径是否与 Evidence 能匹配」：图中路径 g，若存在 e in Evidence 使得 e 与 g 后缀一致或互为子串，算可匹配
        lines.append("### 3.5 Evidence 中未出现在图中的路径（抽样）")
        lines.append("")
        by_suf_missing = defaultdict(list)
        for p in missing:
            by_suf_missing[_suffix(p)].append(p)
        for suf in [".h", ".cpp", ".c"]:
            L = by_suf_missing.get(suf, [])[:10]
            if L:
                lines.append(f"- `{suf}` 共 {len(by_suf_missing[suf])} 条，抽样：")
                for x in L:
                    lines.append(f"  - `{x}`")
        other = [p for suf, arr in by_suf_missing.items() if suf not in (".h", ".cpp", ".c") for p in arr]
        if other:
            lines.append(f"- 其他后缀共 {len(other)} 条，抽样：")
            for x in other[:5]:
                lines.append(f"  - `{x}`")
        lines.append("")
        lines.append("图中路径抽样（前 20）：")
        for p in sorted(graph_paths)[:20]:
            lines.append(f"- `{p}`")
        lines.append("")

    # 4. 对比与结论
    lines.append("## 4. 对比与可能原因")
    lines.append("")

    n_ev = len(evidence_paths)
    n_cc = len(cc_paths)
    n_g = len(graph_paths)

    ev_h = ev_stats["by_suffix"].get(".h", 0)
    ev_cpp = ev_stats["by_suffix"].get(".cpp", 0) + ev_stats["by_suffix"].get(".c", 0)
    g_h = g_stats["by_suffix"].get(".h", 0) if g_stats else 0
    g_cpp = (g_stats["by_suffix"].get(".cpp", 0) + g_stats["by_suffix"].get(".c", 0)) if g_stats else 0

    lines.append("### 4.1 Evidence 中大量 .h 与建图范围不一致")
    lines.append("")
    lines.append("- 建图时**只解析 compile_commands.json 里列出的文件**；而 compile_commands 通常只包含**参与编译的编译单元**（.c / .cpp / .cc / .cxx），**不包含仅被 #include 的 .h 头文件**。")
    lines.append('- 解析脚本（ast_parser / clangd_parser）也只处理 SOURCE_EXTENSIONS = {".c", ".cpp", ".cc", ".cxx"}，**不会把 .h 当作独立文件去解析**。')
    lines.append(f"- 若 Evidence 里列了大量 `.h`（本次 Evidence 去重后 `.h` 数量：{ev_h}，编译单元约 {ev_cpp}），则这些 `.h` 路径**不可能**出现在图的 file_path 中（图中仅有编译单元路径或 clang 报告的定义所在文件）。因此 **Evidence 中 .h 占比越高，图对 Evidence 的覆盖率上限就越低**。")
    lines.append("")

    lines.append("### 4.2 图中为何没有 .h 作为 file_path")
    lines.append("")
    lines.append("- **ast_parser**：遍历时每个函数的 `file_path` 被设为**当前解析的编译单元路径**（即 .cpp/.c），即使用户代码定义在 .h 里，ast_parser 当前实现也写的是 TU 路径。")
    lines.append("- **clangd_parser**：同样只对 compile_commands 中的**源文件**逐文件请求 documentSymbol，得到的 symbol 的 file_path 传的是**当前打开的文件路径**（即 .cpp），不是声明所在头文件。")
    lines.append("- 因此图中 Function 的 file_path **只有 .cpp/.c 等编译单元**，没有单独的 .h。若 Evidence 按「源码文件」列了 .cpp 和 .h，则 .h 在图中必然匹配不到。")
    lines.append("")

    lines.append("### 4.3 compile_commands 范围与解析失败")
    lines.append("")
    if cc_stats:
        lines.append(f"- compile_commands 中去重后文件数：{n_cc}；图中 file_path 数：{n_g}。")
        if n_cc > 0 and n_g < n_cc:
            lines.append(f"- **图中路径数少于 compile_commands**，说明有 **{n_cc - n_g}** 个文件在解析或导入时未产生节点（解析失败、无符号、或写入 Neo4j 时被过滤）。可逐文件检查解析日志或失败率。")
        elif n_cc > 0 and n_g == n_cc:
            lines.append("- 图中路径数与 compile_commands 一致，说明所有在 compile_commands 中的文件都成功进入图。")
        lines.append("")
    else:
        lines.append("- 未读取到 compile_commands，无法对比。建议在 REPO_ROOT/build 下生成 compile_commands.json 后重跑本脚本。")
        lines.append("")

    lines.append("### 4.4 建议")
    lines.append("")
    lines.append("1. **Evidence 与图的定义对齐**：若 Evidence 列出「相关文件」含 .h，可约定覆盖率只按 **.cpp/.c** 计算，或建图时增加「从已解析 TU 中提取声明所在文件」写入节点/边属性，使 .h 也能被统计。")
    lines.append("2. **扩大建图范围**：确认 CMake 配置是否包含全仓库（如 ggml、examples、tools 等）；若只 build 了部分目标，compile_commands 会少很多文件，需 Full 或 All 构建后再生成 compile_commands。")
    lines.append("3. **解析失败**：若 compile_commands 中文件数远大于图中路径数，需排查解析失败原因（依赖缺失、宏、clang 版本等），或对失败文件做白名单/重试。")
    lines.append("")

    out_text = "\n".join(lines)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(out_text, encoding="utf-8")
    print(out_text)
    print(f"\n已写入：{args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
