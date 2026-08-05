"""ReAct 调查工具集：每个工具一个独立函数。

约定：
- 每个工具返回 (observation, new_files)：observation 是给 Agent 看的文本，
  new_files 是本次动作新涉及的文件（用于证据收集）。
- 工具不持有状态；缓存、访问记录等状态由 ReactAgent 维护。
"""
from __future__ import annotations

from pathlib import Path

from .grep_call_chain import (
    grep_files,
    grep_callers,
    grep_callees,
    read_function as _read_function_impl,
)

CODE_EXTS = (".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx")


def tool_read_function(function_name: str, file_path: str, repo_root: Path, out_meta: dict | None = None) -> tuple[str, list[str]]:
    """读取函数完整实现（不截断）。

    out_meta 不为 None 时，填入结构化结果（name/file/start_line/end_line/code）。
    """
    if not function_name:
        return "错误：read_function 缺少 function_name 参数", []
    if not file_path:
        return "错误：read_function 缺少 file_path 参数", []
    file_path = file_path.split(":")[0]  # 清理可能带的行号
    result = _read_function_impl(function_name, file_path, repo_root, max_chars=0)
    if "error" in result:
        return f"读取函数 {function_name} 失败: {result['error']}", []
    if out_meta is not None:
        out_meta.update(result)
    code = result["code"]
    if len(code) > 25000:
        # 超大函数：observation 只给头部+引导（完整代码仍通过 out_meta 进证据注册表）
        head = code[:6000]
        total_lines = code.count("\n") + 1
        obs = (
            f"函数 {function_name} ({file_path}:{result['start_line']}-{result['end_line']})"
            f" —— 该函数过长（{total_lines} 行），以下仅显示开头部分：\n"
            f"```cpp\n{head}\n```\n"
            f"... (省略中间部分)\n"
            f"提示：如需查看中间/结尾逻辑，用 read_lines(\"{file_path}\", start_line, end_line) "
            f"按区间继续读（函数范围 {result['start_line']}-{result['end_line']} 行）。"
        )
    else:
        obs = (
            f"函数 {function_name} ({file_path}:{result['start_line']}-{result['end_line']}):\n"
            f"```cpp\n{code}\n```"
        )
    return obs, [file_path]


def tool_read_lines(file_path: str, start_line: int, end_line: int, repo_root: Path) -> tuple[str, list[str]]:
    """读取文件指定行号范围（用于宏、全局变量、注释块等非函数区域）。"""
    if not file_path:
        return "错误：read_lines 缺少 file_path 参数", []
    abs_path = repo_root / file_path
    try:
        lines = abs_path.read_text(encoding="utf-8", errors="replace").split("\n")
    except Exception as e:
        return f"读取文件 {file_path} 失败: {e}", []
    s = max(0, start_line - 1)
    e = min(len(lines), end_line)
    snippet = "\n".join(lines[s:e])
    obs = f"文件 {file_path} 第 {start_line}-{end_line} 行:\n```cpp\n{snippet}\n```"
    return obs, [file_path]


def tool_list_functions(file_path: str, repo_root: Path) -> tuple[str, list[str]]:
    """列出文件中的所有函数（名称+行号范围+签名），数据来自 Neo4j tree-sitter 解析结果。"""
    if not file_path:
        return "错误：list_functions 缺少 file_path 参数", []
    file_path = file_path.split(":")[0]
    try:
        from src.core.neo4j_client import run_cypher
        rows = run_cypher(
            "MATCH (f:Function {file_path: $fp}) "
            "RETURN f.name AS name, f.start_line AS start, f.end_line AS end, "
            "f.signature AS signature ORDER BY f.start_line",
            {"fp": file_path},
        )
    except Exception as e:
        return f"查询函数列表失败: {e}", []
    if not rows:
        return f"文件 {file_path} 中没有函数（或文件不存在）", []
    lines = [f"文件 {file_path} 共 {len(rows)} 个函数:"]
    for r in rows:
        sig = (r.get("signature") or "").strip()
        sig = f" — {sig[:80]}" if sig else ""
        lines.append(f"- {r['name']} ({r['start']}-{r['end']}){sig}")
    return "\n".join(lines), []


def tool_list_files(directory: str, repo_root: Path) -> tuple[str, list[str]]:
    """列举目录下的代码文件和子目录（非递归）。"""
    if not directory:
        return "错误：list_files 缺少 directory 参数", []
    abs_dir = (repo_root / directory).resolve()
    try:
        abs_dir.relative_to(repo_root.resolve())
    except ValueError:
        return f"错误：目录 {directory} 不在仓库内", []
    if not abs_dir.is_dir():
        return f"目录 {directory} 不存在", []
    subdirs, files = [], []
    for p in sorted(abs_dir.iterdir()):
        if p.name.startswith("."):
            continue
        if p.is_dir():
            n_code = sum(1 for c in p.iterdir() if c.suffix in CODE_EXTS) if p.is_dir() else 0
            subdirs.append(f"- {p.name}/ ({n_code} 个代码文件)")
        elif p.suffix in CODE_EXTS:
            files.append(f"- {p.name}")
    if not subdirs and not files:
        return f"目录 {directory} 下没有代码文件", []
    parts = [f"目录 {directory} 内容:"]
    if subdirs:
        parts.append("子目录:\n" + "\n".join(subdirs))
    if files:
        parts.append("代码文件:\n" + "\n".join(files))
    return "\n".join(parts), []


def tool_find_callers(function_name: str, repo_root: Path, limit: int = 10, out_meta: list | None = None) -> tuple[str, list[str]]:
    """查找谁调用了该函数（grep 全仓库，定位到具体调用方函数）。

    out_meta 不为 None 时，把结构化调用点列表（name/file/line）extend 进去。
    """
    if not function_name:
        return "错误：find_callers 缺少 function_name 参数", []
    callers = grep_callers(function_name, repo_root, limit=limit)
    if not callers:
        return f"没有找到调用 {function_name} 的地方", []

    # grep 词法分析定不了归属函数的调用点（name 为空），用 Neo4j 函数行号表补全
    anon_files = {c["file"] for c in callers if not c.get("name")}
    if anon_files:
        try:
            from src.core.neo4j_client import run_cypher
            for fp in anon_files:
                rows = run_cypher(
                    "MATCH (f:Function {file_path: $fp}) "
                    "RETURN f.name AS name, f.start_line AS start, f.end_line AS end",
                    {"fp": fp},
                )
                for c in callers:
                    if c.get("name") or c["file"] != fp:
                        continue
                    for r in rows:
                        if r["start"] <= c["line"] <= r["end"]:
                            c["name"] = r["name"]
                            break
        except Exception:
            pass

    if out_meta is not None:
        out_meta.extend(callers)
    lines = [f"找到 {len(callers)} 处对 {function_name} 的调用:"]
    files = []
    for c in callers:
        loc = f"{c['file']}:{c['line']}"
        files.append(c["file"])
        caller = c["name"] or "(全局/未知作用域)"
        lines.append(f"- {caller} @ {loc}: {c['content'][:120]}")
    lines.append("\n注意：以上调用点目前只是线索，必须 read_function 读取调用方函数的实现后，才能在答案中作为证据引用。")
    return "\n".join(lines), sorted(set(files))


def tool_find_callees(
    function_name: str,
    file_path: str,
    start_line: int,
    end_line: int,
    repo_root: Path,
    limit: int = 15,
) -> tuple[str, list[str]]:
    """列出该函数体内调用了哪些函数（直接分析函数实现）。"""
    if not function_name or not file_path:
        return "错误：find_callees 需要 function_name 和 file_path 参数", []
    file_path = file_path.split(":")[0]
    callees = grep_callees(function_name, file_path, start_line, end_line, repo_root, limit=limit)
    if not callees:
        return f"函数 {function_name} 体内没有发现函数调用", []
    names = [c["name"] for c in callees]
    return f"函数 {function_name} 调用了: {', '.join(names)}", []


def tool_search_symbol(symbol_name: str, repo_root: Path, limit: int = 10) -> tuple[str, list[str]]:
    """搜索符号：先查 Neo4j 函数名索引（精确定义位置），再用 grep 补充（所有提及）。"""
    symbol = (symbol_name or "").strip()
    if not symbol:
        return "搜索符号为空，跳过", []
    sections = []
    files = []

    # 1. Neo4j 函数名索引：精确到函数定义
    try:
        from src.core.neo4j_client import run_cypher
        rows = run_cypher(
            "MATCH (f:Function) WHERE f.name CONTAINS $s "
            "RETURN f.name AS name, f.file_path AS fp, f.start_line AS start, f.end_line AS end "
            "ORDER BY f.start_line LIMIT $lim",
            {"s": symbol, "lim": limit},
        )
        if rows:
            lines = [f"Neo4j 索引中找到 {len(rows)} 个名称包含 '{symbol}' 的函数:"]
            for r in rows:
                lines.append(f"- {r['name']} @ {r['fp']}:{r['start']}-{r['end']}")
                files.append(r["fp"])
            sections.append("\n".join(lines))
    except Exception:
        pass

    # 2. grep 全文：所有提及该符号的文件
    import re
    grep_hits = grep_files(rf"\b{re.escape(symbol)}\b", repo_root, limit=limit)
    if grep_hits:
        sections.append(
            f"grep 找到 {len(grep_hits)} 个文件包含 '{symbol}':\n" + "\n".join(f"- {f}" for f in grep_hits)
        )
        files.extend(grep_hits)

    if not sections:
        return f"没有找到包含 '{symbol}' 的函数或文件", []
    # 保序去重
    seen, uniq = set(), []
    for f in files:
        if f not in seen:
            seen.add(f)
            uniq.append(f)
    return "\n\n".join(sections), uniq


def tool_expand_recall(recall_pool: list[dict], recall_shown: int, batch: int = 20) -> tuple[str, int]:
    """展开初始召回池的下一批候选函数。返回 (observation, 新的已展示数量)。"""
    if recall_shown >= len(recall_pool):
        return "召回池已耗尽（共 %d 个候选），没有更多候选函数了" % len(recall_pool), recall_shown
    nxt = recall_pool[recall_shown:recall_shown + batch]
    lines = [f"召回池第 {recall_shown + 1}-{recall_shown + len(nxt)} 个候选函数:"]
    for f in nxt:
        lines.append(
            f"- {f['name']} @ {f['file_path']}:{f['start_line']}-{f['end_line']} (score: {f['score']:.3f})"
        )
    return "\n".join(lines), recall_shown + len(nxt)
