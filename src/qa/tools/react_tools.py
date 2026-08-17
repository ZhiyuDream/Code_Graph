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


def tool_find_callers(function_name: str, repo_root: Path, limit: int = 25, out_meta: list | None = None) -> tuple[str, list[str]]:
    """查找谁调用了该函数（grep 全仓库，定位到具体调用方函数）。

    out_meta 不为 None 时，把结构化调用点列表（name/file/line）extend 进去。
    调用点超过 limit 时**显式声明截断**（此前静默截断为 10 处还显示"找到 10 处"，
    Agent 会误以为那就是全部调用方——证据完整性问题）。
    """
    if not function_name:
        return "错误：find_callers 缺少 function_name 参数", []
    # 多查 1 个用于检测溢出
    callers = grep_callers(function_name, repo_root, limit=limit + 1)
    if not callers:
        return f"没有找到调用 {function_name} 的地方", []
    overflow = len(callers) > limit
    if overflow:
        callers = callers[:limit]

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
    head = f"找到 {len(callers)} 处对 {function_name} 的调用"
    if overflow:
        head += f"（超过上限，仅显示前 {limit} 处；这不是全部调用方，可用 search_symbol 查完整提及）"
    lines = [head + ":"]
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
    out_meta: list | None = None,
) -> tuple[str, list[str]]:
    """列出该函数体内调用了哪些函数（直接分析函数实现）。

    out_meta 不为 None 时，把结构化 callee 列表（name/file）extend 进去。
    """
    if not function_name or not file_path:
        return "错误：find_callees 需要 function_name 和 file_path 参数", []
    file_path = file_path.split(":")[0]
    callees = grep_callees(function_name, file_path, start_line, end_line, repo_root, limit=limit + 1)
    if not callees:
        return f"函数 {function_name} 体内没有发现函数调用", []
    overflow = len(callees) > limit
    if overflow:
        callees = callees[:limit]
    if out_meta is not None:
        out_meta.extend(callees)
    names = [c["name"] for c in callees]
    obs = f"函数 {function_name} 调用了: {', '.join(names)}"
    if overflow:
        obs += f"（超过 {limit} 个，仅显示前 {limit} 个；这不是全部，可用 read_lines 读完整函数体确认）"
    return obs, []


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


def tool_search_codebase(
    query: str,
    repo_root: Path,
    retriever=None,
    chunks_by_id: dict | None = None,
    limit: int = 8,
) -> tuple[str, list[str], list[dict]]:
    """区域级搜索：把自然语言意图翻译成结构化的区域报告。

    返回 (observation, files, hits)。hits 是结构化候选 [{name, file_path, start_line, end_line, signature, score}]，
    供调用方注入召回池/frontier。
    组合现有组件：名称索引搜索 + 目录命中密度 + embedding 重查。
    """
    if not query or not query.strip():
        return "错误：search_codebase 缺少 query 参数", [], []

    import re
    terms = [t.lower() for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", query) if len(t) >= 3]
    sections = []
    files = []
    hits: list[dict] = []

    # 1. 名称索引搜索 + 目录密度
    if chunks_by_id and terms:
        dir_counts: dict[str, int] = {}
        name_hits = []
        for fid, ch in chunks_by_id.items():
            meta = ch.get("meta") or {}
            name = (meta.get("name") or "").lower()
            fp = meta.get("file_path", "")
            if not name or not fp:
                continue
            if any(t in name for t in terms):
                parts = fp.split("/")
                d = "/".join(parts[:3]) if len(parts) > 3 else "/".join(parts[:-1]) or parts[0]
                dir_counts[d] = dir_counts.get(d, 0) + 1
                name_hits.append(meta)
        if dir_counts:
            top_dirs = sorted(dir_counts.items(), key=lambda kv: -kv[1])[:5]
            sections.append("命中目录：\n" + "\n".join(f"- {d}（{n} 个函数名命中）" for d, n in top_dirs))
        # 名称命中按"精确度"排序：短名优先（更可能是核心符号）
        name_hits.sort(key=lambda m: len(m.get("name") or ""))
        for m in name_hits[:limit]:
            hits.append({
                "fid": f"{m['file_path']}:{m['name']}:{m.get('start_line', 0)}",
                "name": m.get("name", ""), "file_path": m.get("file_path", ""),
                "start_line": m.get("start_line", 0), "end_line": m.get("end_line", 0),
                "signature": m.get("signature", ""), "score": 1.0,
            })
            files.append(m.get("file_path", ""))

    # 2. embedding 重查（语义补充）
    if retriever is not None:
        try:
            import numpy as _np
            q_emb = retriever.encode_queries([query])[0]
            for r in retriever.retrieve(_np.asarray([q_emb], dtype=_np.float32), top_k=limit):
                md = r["metadata"]
                fid = f"{md['file_path']}:{md['name']}:{md['start_line']}"
                if any(h["fid"] == fid for h in hits):
                    continue
                hits.append({
                    "fid": fid, "name": md.get("name", ""), "file_path": md.get("file_path", ""),
                    "start_line": md.get("start_line", 0), "end_line": md.get("end_line", 0),
                    "signature": md.get("signature", ""), "score": r.get("score", 0.0),
                })
                files.append(md.get("file_path", ""))
        except Exception:
            pass

    if not hits:
        return f"search_codebase('{query}') 没有找到相关区域", [], []

    rep = "\n".join(
        f"- {h['name']} @ {h['file_path']}:{h['start_line']}-{h['end_line']}"
        + (f" — {h['signature'][:60]}" if h.get("signature") else "")
        for h in hits[:limit]
    )
    sections.append(f"代表函数（{len(hits)} 个）：\n{rep}")
    obs = f"search_codebase('{query}') 结果：\n\n" + "\n\n".join(sections)
    return obs, sorted(set(f for f in files if f)), hits[: limit * 2]


def tool_scan_directory(
    directory: str,
    repo_root: Path,
    max_files: int = 12,
    max_funcs_per_file: int = 15,
    keywords: list | None = None,
) -> tuple[str, list[str]]:
    """目录粗筛：列出目录下代码文件及各自的函数名清单（只看名字/签名，不读实现）。

    用于"这个目录大概率有相关文件，先扫一眼"的人类式探索：
    数据来自 Neo4j tree-sitter 索引，成本远低于逐个 read_function。
    keywords 非空时按关键词命中数排序再截断（036 案例：common/ 51 个文件按字母序截断，
    gold 文件 ngram-map.cpp 被切掉）。
    """
    if not directory:
        return "错误：scan_directory 缺少 directory 参数", []
    directory = directory.rstrip("/")
    abs_dir = (repo_root / directory).resolve()
    try:
        abs_dir.relative_to(repo_root.resolve())
    except ValueError:
        return f"错误：目录 {directory} 不在仓库内", []
    if not abs_dir.is_dir():
        return f"目录 {directory} 不存在", []
    code_files = sorted(
        p.name for p in abs_dir.iterdir() if p.is_file() and p.suffix in CODE_EXTS
    )
    if not code_files:
        return f"目录 {directory} 下没有代码文件", []
    # 子目录也列出来，方便继续下钻
    subdirs = sorted(p.name + "/" for p in abs_dir.iterdir() if p.is_dir() and not p.name.startswith("."))

    # 一次性查该目录所有函数（Neo4j 索引，不读文件内容）
    funcs_by_file: dict[str, list] = {}
    try:
        from src.core.neo4j_client import run_cypher
        rows = run_cypher(
            "MATCH (f:Function) WHERE f.file_path STARTS WITH $prefix "
            "RETURN f.file_path AS fp, f.name AS name, f.start_line AS start "
            "ORDER BY f.file_path, f.start_line",
            {"prefix": directory + "/"},
        )
        for r in rows:
            fp = r["fp"]
            # 只要直接子文件，子目录的函数不归进来
            if "/" in fp[len(directory) + 1:]:
                continue
            funcs_by_file.setdefault(fp, []).append(r["name"])
    except Exception:
        pass

    # 关键词相关性排序：文件名/函数名命中关键词多的排前面（截断也先截无关的）
    kws = [k.lower() for k in (keywords or []) if isinstance(k, str) and len(k) >= 3]
    if kws:
        def _rel(name: str) -> int:
            fp = f"{directory}/{name}"
            hay = name.lower() + " " + " ".join(funcs_by_file.get(fp, [])).lower()
            return sum(1 for k in kws if k in hay)
        code_files = sorted(code_files, key=lambda n: (-_rel(n), n))

    lines = [f"目录 {directory} 粗筛（{len(code_files)} 个代码文件，只看函数名，未读实现）:"]
    if subdirs:
        lines.append("子目录: " + ", ".join(subdirs[:10]))
    shown_files = code_files[:max_files]
    for name in shown_files:
        fp = f"{directory}/{name}"
        funcs = funcs_by_file.get(fp, [])
        if funcs:
            listing = ", ".join(funcs[:max_funcs_per_file])
            more = f" ... 还有 {len(funcs) - max_funcs_per_file} 个" if len(funcs) > max_funcs_per_file else ""
            lines.append(f"■ {name}（{len(funcs)} 个函数）: {listing}{more}")
        else:
            lines.append(f"■ {name}（索引中无函数，可能是纯声明/宏文件）")
    if len(code_files) > max_files:
        rest = code_files[max_files:]
        lines.append(f"... 还有 {len(rest)} 个文件未列出（{', '.join(rest[:8])} 等），可用 list_files 查看或指定子目录再扫")
    lines.append("提示：对可疑文件用 list_functions 看完整函数清单，对可疑函数用 read_function 读实现。")
    return "\n".join(lines), []


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
