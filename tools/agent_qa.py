#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
import time
import argparse
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

from neo4j_writer import get_driver
from config import NEO4J_DATABASE, OPENAI_API_KEY, OPENAI_BASE_URL, LLM_MODEL

MAX_STEPS = 6
TOOL_RESULT_MAX = 1200


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------

def _run(driver, cypher: str, params: dict = None) -> list[dict]:
    with driver.session(database=NEO4J_DATABASE) as s:
        r = s.run(cypher, params or {})
        return [dict(rec) for rec in r]


# ---- 层1：模块/目录级工具 ----

def tool_find_module_by_keyword(driver, keyword: str) -> str:
    """
    将关键词映射到相关目录和文件。
    用于问题没有明确函数名时，先定位到相关模块。
    """
    kw = keyword.lower().strip()
    results = {}

    dirs = _run(driver, """
        MATCH (d:Directory)
        WHERE toLower(d.name) CONTAINS $kw OR toLower(d.path) CONTAINS $kw
        RETURN d.path AS path, d.name AS name
        ORDER BY size(d.path) ASC
        LIMIT 5
    """, {"kw": kw})
    if dirs:
        results["匹配的目录"] = [f"{d['path']} ({d['name']})" for d in dirs]

    files = _run(driver, """
        MATCH (f:File)
        WHERE toLower(f.path) CONTAINS $kw
        RETURN f.path AS path, f.language AS lang
        ORDER BY f.path ASC
        LIMIT 8
    """, {"kw": kw})
    if files:
        results["匹配的文件"] = [f["path"] for f in files]

    funcs = _run(driver, """
        MATCH (f:File)-[:CONTAINS]->(fn:Function)
        WHERE toLower(f.path) CONTAINS $kw
        RETURN fn.name AS name, f.path AS file,
               fn.fan_in AS fan_in, fn.fan_out AS fan_out
        ORDER BY fn.fan_in DESC
        LIMIT 6
    """, {"kw": kw})
    if funcs:
        results["相关文件中的核心函数"] = [
            f"{fn['name']} ({fn['file']}) fan_in={fn['fan_in']}"
            for fn in funcs
        ]

    if not results:
        return f"未找到与 '{keyword}' 相关的目录或文件。"

    lines = []
    for section, items in results.items():
        lines.append(f"【{section}】")
        for item in items:
            lines.append(f"  {item}")
        lines.append("")
    return "\n".join(lines).strip()


def tool_get_directory_tree(driver, directory: str = "", depth: int = 2) -> str:
    """
    显示目录树结构。
    directory 为空时显示顶层目录；指定时显示子目录。
    """
    if directory:
        d_path = directory.rstrip("/")
        children = _run(driver, """
            MATCH (d:Directory {path: $path})-[:CONTAINS]->(child)
            RETURN labels(child)[0] AS type,
                   child.path AS path,
                   child.name AS name
            ORDER BY type, path
            LIMIT 30
        """, {"path": d_path})
        if not children:
            return f"目录 '{directory}' 在图中不存在或无子节点。"
        lines = [f"目录: {directory}", ""]
        subdirs = [c for c in children if c["type"] == "Directory"]
        files = [c for c in children if c["type"] == "File"]
        if subdirs:
            lines.append("子目录:")
            for sd in subdirs:
                lines.append(f"  {sd['path']}/")
        if files:
            lines.append("文件:")
            for fl in files:
                lines.append(f"  {fl['path']}")
        return "\n".join(lines)
    else:
        roots = _run(driver, """
            MATCH (repo:Repository)-[:CONTAINS]->(d:Directory)
            MATCH (d)-[:CONTAINS*0..1]->(child)
            WITH DISTINCT d, child
            ORDER BY d.path, child.path
            LIMIT 50
            RETURN DISTINCT d.path AS dir_path,
                   collect(DISTINCT child.path)[0..5] AS sample_children
            ORDER BY d.path
        """)
        if not roots:
            return "未找到目录结构。"
        lines = ["代码仓顶层目录结构:", ""]
        for r in roots:
            lines.append(f"{r['dir_path']}/")
            for ch in r["sample_children"][:5]:
                ch_label = "dir" if "/" in ch.replace(r["dir_path"], "").strip("/") else "file"
                lines.append(f"   [{ch_label}] {ch}")
            lines.append("")
        return "\n".join(lines).strip()


def tool_get_module_overview(driver, module_path: str) -> str:
    """
    给出某个目录/模块的概况：
    - 直接子目录
    - 直接包含的文件（按重要度排序）
    - 该模块中高 fan_in 的函数（跨模块调用多的入口函数）
    """
    path = module_path.rstrip("/")
    exists = _run(driver, """
        MATCH (d:Directory {path: $path})
        RETURN d.name AS name
    """, {"path": path})
    if not exists:
        return f"模块 '{path}' 在图中不存在。"

    subdirs = _run(driver, """
        MATCH (d:Directory {path: $path})-[:CONTAINS]->(sub:Directory)
        RETURN sub.path AS path
        ORDER BY sub.path
        LIMIT 10
    """, {"path": path})

    files = _run(driver, """
        MATCH (d:Directory {path: $path})-[:CONTAINS]->(f:File)-[:CONTAINS]->(fn:Function)
        WHERE fn.fan_in > 0
        RETURN f.path AS file,
               count(fn) AS func_count,
               max(fn.fan_in) AS top_fan_in
        ORDER BY top_fan_in DESC
        LIMIT 8
    """, {"path": path})

    entry_funcs = _run(driver, """
        MATCH (d:Directory {path: $path})-[:CONTAINS]->(f:File)-[:CONTAINS]->(fn:Function)
        WHERE fn.fan_in > 5
        RETURN fn.name AS name, f.path AS file, fn.fan_in AS fan_in
        ORDER BY fn.fan_in DESC
        LIMIT 8
    """, {"path": path})

    lines = [f"模块: {path}", ""]
    if subdirs:
        lines.append("子目录:")
        for sd in subdirs:
            lines.append(f"  {sd['path']}/")
        lines.append("")
    if files:
        lines.append("核心文件（按入口重要性）:")
        for fl in files:
            lines.append(f"  {fl['file']}  ({fl['func_count']}个函数, top_fan_in={fl['top_fan_in']})")
        lines.append("")
    if entry_funcs:
        lines.append("高 fan_in 入口函数（被多模块调用）:")
        for ef in entry_funcs:
            lines.append(f"  -> {ef['name']} @ {ef['file']}  fan_in={ef['fan_in']}")
    else:
        lines.append("（无高 fan_in 函数）")

    return "\n".join(lines).strip()


# ---- 层2：函数级工具 ----

def tool_search_functions(driver, name_pattern: str, limit: int = 8) -> str:
    """按名字模糊搜索函数（支持 substring）"""
    rows = _run(driver, """
        MATCH (f:Function)
        WHERE toLower(f.name) CONTAINS toLower($pat)
        RETURN f.name AS name, f.file_path AS file, f.fan_in AS fan_in, f.fan_out AS fan_out
        ORDER BY f.fan_in DESC LIMIT $lim
    """, {"pat": name_pattern, "lim": limit})
    if not rows:
        return f"未找到名字包含 '{name_pattern}' 的函数。"
    lines = [f"{r['name']} ({r['file']}) fan_in={r['fan_in']} fan_out={r['fan_out']}" for r in rows]
    return "\n".join(lines)


def tool_search_functions_by_content(driver, keyword: str, limit: int = 8) -> str:
    """
    通过函数名或文件路径中的关键词搜索函数。
    当精确函数名搜索失败时使用，例如搜 'blas' 可以找到 ggml/src/ggml-cpu/llamafile/sgemm.cpp 中的函数。
    同时匹配函数名和文件路径。
    """
    rows = _run(driver, """
        MATCH (f:Function)
        WHERE toLower(f.name) CONTAINS toLower($kw)
           OR toLower(f.file_path) CONTAINS toLower($kw)
        RETURN f.name AS name, f.file_path AS file,
               f.fan_in AS fan_in, f.fan_out AS fan_out
        ORDER BY f.fan_in DESC LIMIT $lim
    """, {"kw": keyword, "lim": limit})
    if not rows:
        return f"未找到名字或路径包含 '{keyword}' 的函数。"
    lines = [f"{r['name']} ({r['file']}) fan_in={r['fan_in']} fan_out={r['fan_out']}" for r in rows]
    return "\n".join(lines)


def tool_get_function_detail(driver, func_name: str) -> str:
    """获取函数的注解、fan_in/out、文件路径"""
    rows = _run(driver, """
        MATCH (f:Function {name: $name})
        RETURN f.name AS name, f.file_path AS file,
               f.fan_in AS fan_in, f.fan_out AS fan_out,
               f.annotation_json AS ann
        LIMIT 3
    """, {"name": func_name})
    if not rows:
        return f"未找到函数 '{func_name}'。"
    out = []
    for r in rows:
        ann = ""
        if r.get("ann"):
            try:
                d = json.loads(r["ann"]) if isinstance(r["ann"], str) else r["ann"]
                ann = d.get("summary", "")[:300]
            except Exception:
                ann = str(r["ann"])[:300]
        out.append(f"函数: {r['name']}\n文件: {r['file']}\nfan_in={r['fan_in']} fan_out={r['fan_out']}\n注解: {ann}")
    return "\n\n".join(out)


def tool_get_callers(driver, func_name: str, limit: int = 10) -> str:
    """谁调用了该函数（上游调用者）"""
    rows = _run(driver, """
        MATCH (caller:Function)-[:CALLS]->(f:Function {name: $name})
        RETURN caller.name AS name, caller.file_path AS file
        LIMIT $lim
    """, {"name": func_name, "lim": limit})
    if not rows:
        return f"没有找到调用 '{func_name}' 的函数（可能是入口函数或 fan_in=0）。"
    lines = [f"{r['name']} ({r['file']})" for r in rows]
    return f"调用 {func_name} 的函数（{len(lines)} 个）:\n" + "\n".join(lines)


def tool_get_callees(driver, func_name: str, limit: int = 10) -> str:
    """该函数调用了哪些函数（下游）"""
    rows = _run(driver, """
        MATCH (f:Function {name: $name})-[:CALLS]->(callee:Function)
        RETURN callee.name AS name, callee.file_path AS file
        LIMIT $lim
    """, {"name": func_name, "lim": limit})
    if not rows:
        return f"'{func_name}' 没有调用其他已解析函数（可能是叶子函数）。"
    lines = [f"{r['name']} ({r['file']})" for r in rows]
    return f"{func_name} 调用的函数（{len(lines)} 个）:\n" + "\n".join(lines)


def tool_get_file_functions(driver, file_path: str, limit: int = 15) -> str:
    """列出某文件中的函数（按 fan_in 降序）"""
    rows = _run(driver, """
        MATCH (f:File)
        WHERE f.path CONTAINS $fp
        MATCH (f)-[:CONTAINS]->(fn:Function)
        RETURN fn.name AS name, fn.fan_in AS fan_in, fn.fan_out AS fan_out
        ORDER BY fn.fan_in DESC LIMIT $lim
    """, {"fp": file_path, "lim": limit})
    if not rows:
        return f"未找到路径包含 '{file_path}' 的函数。"
    lines = [f"{r['name']} fan_in={r['fan_in']} fan_out={r['fan_out']}" for r in rows]
    return f"文件 {file_path} 中的函数 ({len(lines)} 个):\n" + "\n".join(lines)


def tool_read_file_lines(driver, file_path: str, start_line: int, end_line: int = None, context: int = 5) -> str:
    """
    读取指定文件的指定行范围内容，用于查看结构体定义、变量声明等具体代码。
    file_path: 文件路径（支持部分匹配，如 'llama-grammar.h'）
    start_line: 起始行号（从 1 开始）
    end_line: 结束行号（可选，默认 start_line + context*2）
    context: 当 end_line 未指定时，额外显示 start_line 前后多少行
    """
    base_name = Path(file_path).name
    rows = _run(driver, """
        MATCH (f:File)
        WHERE f.path CONTAINS $fp
        RETURN f.path AS path
        ORDER BY size(f.path) ASC
        LIMIT 10
    """, {"fp": file_path})
    if not rows:
        base_without_ext = str(Path(file_path).name).rsplit(".", 1)[0]
        rows = _run(driver, """
            MATCH (f:File)
            WHERE f.path CONTAINS $base
            RETURN f.path AS path
            ORDER BY size(f.path) ASC
            LIMIT 5
        """, {"base": base_without_ext})

    possible_roots = [
        Path("/data/yulin/RUC/llama.cpp"),
        Path("/data/yulin/RUC"),
        Path.cwd(),
    ]

    matched_path = rows[0]["path"] if rows else None
    full_path = None

    if file_path.endswith((".h", ".hpp")):
        for root in possible_roots:
            candidate = root / file_path
            if candidate.exists():
                full_path = candidate
                break

    if full_path is None and matched_path:
        for root in possible_roots:
            candidate = root / matched_path
            if candidate.exists():
                full_path = candidate
                break

    if full_path is None and matched_path and matched_path.endswith((".h", ".hpp")):
        for root in possible_roots:
            alt = root / (matched_path.rsplit(".", 1)[0] + ".cpp")
            if alt.exists():
                full_path = alt
                break

    if full_path is None:
        tried = f" (尝试过: {matched_path})" if matched_path else ""
        return f"未找到文件: {file_path}{tried}"

    if end_line is None:
        end_line = start_line + context * 2

    try:
        with open(full_path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        total_lines = len(lines)
        actual_start = max(0, start_line - 1)
        actual_end = min(total_lines, end_line)
        snippet_lines = lines[actual_start:actual_end]
        if not snippet_lines:
            return f"文件 {full_path} 共 {total_lines} 行，请求范围 [{start_line}, {end_line}] 超出"
        snippet = "".join(snippet_lines)
        header = f"文件: {full_path} (共 {total_lines} 行)\n行号 {actual_start+1}-{actual_end}:\n"
        return header + snippet
    except Exception as e:
        return f"读取文件失败: {e}"


def tool_search_variables(driver, name_pattern: str, limit: int = 10) -> str:
    """按名称搜索全局变量/常量/枚举值"""
    rows = _run(driver, """
        MATCH (v:Variable)
        WHERE v.name CONTAINS $pattern
        RETURN v.name AS name, v.file_path AS file_path
        ORDER BY size(v.file_path) LIMIT $lim
    """, {"pattern": name_pattern, "lim": limit})
    if not rows:
        return f"未找到名称包含 '{name_pattern}' 的变量。"
    lines = [f"{r['name']} ({r['file_path']})" for r in rows]
    return f"变量 '{name_pattern}' 相关结果（{len(lines)} 个）:\n" + "\n".join(lines)


def tool_search_attributes(driver, name_pattern: str, limit: int = 10) -> str:
    """按名称搜索 Class 成员（struct field / class member）"""
    rows = _run(driver, """
        MATCH (a:Attribute)
        WHERE a.name CONTAINS $pattern OR a.member_of_class CONTAINS $pattern
        RETURN a.name AS name, a.file_path AS file_path, a.member_of_class AS member_of_class
        ORDER BY size(a.file_path) LIMIT $lim
    """, {"pattern": name_pattern, "lim": limit})
    if not rows:
        return f"未找到名称包含 '{name_pattern}' 的 Class 成员。"
    lines = [f"{r['name']} [{r['member_of_class']}] ({r['file_path']})" for r in rows]
    return f"Class 成员 '{name_pattern}' 相关结果（{len(lines)} 个）:\n" + "\n".join(lines)


# ---- 层3：Issue/PR 工具 ----

def _issue_relevance_score(issue_row: dict, keyword: str) -> float:
    """计算单条 Issue 的 relevance_score（Jaccard 风格）"""
    STOPWORDS = {"a", "an", "the", "in", "on", "at", "to", "for", "of", "and", "or", "but", "is", "are", "was", "were", "be", "with", "by", "from"}
    kw_words = {w.lower() for w in re.findall(r'\b\w+\b', keyword) if w.lower() not in STOPWORDS and len(w) > 1}
    if not kw_words:
        return 0.5

    title_words = {w.lower() for w in re.findall(r'\b\w+\b', issue_row.get('title', '') or '')}
    body_text = (issue_row.get('body') or '')[:2000]
    body_words = {w.lower() for w in re.findall(r'\b\w+\b', body_text) if w.lower() not in STOPWORDS and len(w) > 1}

    if kw_words & title_words:
        title_match = len(kw_words & title_words) / len(kw_words | title_words)
    else:
        title_match = 0.0

    body_match = sum(1 for w in kw_words if w in body_words) / len(kw_words)
    return title_match * 0.6 + body_match * 0.4


def tool_search_issues(driver, keyword: str, limit: int = 5) -> str:
    """按关键词搜索 Issue 节点，按 final_score = relevance × ranking_score 排序"""
    if re.match(r'^\d+$', keyword.strip()):
        return tool_get_issue_detail(driver, keyword.strip())

    rows = _run(driver, """
        MATCH (i:Issue)
        WHERE toLower(i.title) CONTAINS toLower($kw)
              OR toLower(coalesce(i.body, '')) CONTAINS toLower($kw)
        RETURN i.number AS num, i.title AS title, i.body AS body,
               i.ranking_score AS ranking_score, i.tier AS tier,
               i.labels AS labels, i.comments AS comments,
               i.created_at AS created_at
        LIMIT 100
    """, {"kw": keyword})

    if not rows:
        return f"未找到包含关键词 '{keyword}' 的 Issue。"

    scored = []
    for r in rows:
        relevance = _issue_relevance_score(r, keyword)
        ranking = float(r.get('ranking_score') or 0.5)
        final_score = relevance * ranking
        scored.append((final_score, r))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:limit]

    lines = []
    for score, r in top:
        tier = r.get('tier') or '?'
        rs = r.get('ranking_score')
        lines.append(f"Issue #{r['num']} [tier={tier} score={rs:.3f} final={score:.3f}]: {r['title']}")
    return "\n".join(lines)


def tool_get_issue_detail(driver, issue_num: str) -> str:
    """获取 Issue 详情及关联 PR"""
    rows = _run(driver, """
        MATCH (i:Issue {number: toInteger($num)})
        OPTIONAL MATCH (i)<-[:FIXES]-(pr:PullRequest)
        RETURN i.number AS num, i.title AS title,
               i.body AS body,
               collect(distinct pr.number + ': ' + pr.title) AS fix_prs
        LIMIT 1
    """, {"num": str(issue_num)})
    if not rows:
        return f"未找到 Issue #{issue_num}。"
    r = rows[0]
    body_snippet = (r.get("body") or "")[:400]
    prs = r.get("fix_prs") or []
    result = f"Issue #{r['num']}: {r['title']}\n描述: {body_snippet}"
    if prs:
        result += f"\n修复 PR: {'; '.join(str(p) for p in prs if p)}"
    return result


def _cosine_sim(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


# 全局 RAG 索引（延迟加载）
_RAG_INDEX = None


def _load_rag_index(idx_name: str | None = None):
    """加载 RAG 索引。idx_name 为 None 时使用环境变量 RAG_INDEX_NAME 或默认 classic_rag_index.json"""
    if idx_name is None:
        import os
        idx_name = os.environ.get("RAG_INDEX_NAME", "classic_rag_index.json")
    idx_path = _ROOT / "data" / idx_name
    if idx_path.exists():
        with open(idx_path, encoding="utf-8") as f:
            return json.load(f)
    return None


def tool_search_by_embedding(driver, query: str, limit: int = 6) -> str:
    """
    基于 embedding 的语义搜索。
    利用预计算的 RAG 索引，通过 cosine 相似度找到语义最相关的函数。
    返回每个函数的名称、文件、描述 annotation。

    当 graph 查询（CONTAINS 匹配）失败时，用这个工具作为 fallback——
    它通过语义相似性而非字符串匹配来找到相关函数。

    使用环境变量 RAG_INDEX_NAME 指定索引文件（默认 classic_rag_index.json）。
    """
    idx = _load_rag_index()
    if idx is None:
        return "语义搜索失败：RAG 索引不存在。"

    from openai import OpenAI
    from config import OPENAI_API_KEY, OPENAI_BASE_URL, EMBEDDING_MODEL
    client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)

    try:
        emb_resp = client.embeddings.create(model=EMBEDDING_MODEL, input=[query])
        query_emb = emb_resp.data[0].embedding
    except Exception as e:
        return f"语义搜索失败：embedding API 错误 ({e})"

    # Compute similarity with all function chunks
    func_chunks = [(i, c) for i, c in enumerate(idx["chunks"]) if c["type"] == "function"]
    scores = []
    for i, chunk in func_chunks:
        sim = _cosine_sim(query_emb, idx["embeddings"][i])
        scores.append((sim, chunk))
    scores.sort(key=lambda x: -x[0])

    if not scores:
        return f"语义搜索 '{query}' 未找到相关函数。"

    out = []
    for sim, chunk in scores[:limit]:
        text = chunk["text"]
        # Extract function name and description from text
        name = chunk["meta"].get("name", chunk["id"].split("::")[1] if "::" in chunk["id"] else "")
        file_p = chunk["meta"].get("file", "")
        # Extract description line
        desc = ""
        if "描述:" in text:
            desc = text.split("描述:", 1)[1].split("\n")[0].strip()
        out.append(f"[{sim:.3f}] {name} ({file_p})")
        if desc:
            out.append(f"  描述: {desc[:200]}")
        out.append("")

    return "\n".join(out).strip()


# ---------------------------------------------------------------------------
# 工具注册表
# ---------------------------------------------------------------------------

TOOLS = [
    # 层1：模块/目录级
    {
        "type": "function",
        "function": {
            "name": "find_module_by_keyword",
            "description": "将关键词映射到相关目录和文件，用于问题没有明确函数名时先定位相关模块。例如关键词 'server'、'moe'、'quantize'、'vulkan'。返回匹配的目录、文件、以及文件中的核心函数（按 fan_in 排序）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "搜索关键词（如模块名、技术名、文件名）"},
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_directory_tree",
            "description": "显示目录树结构。不带参数时显示顶层目录；带 directory 参数时显示该目录的直接子目录和文件。用于了解代码结构和模块划分。",
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {"type": "string", "description": "目录路径（如 'ggml'、'src'、'tools'）。为空则显示顶层目录。"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_module_overview",
            "description": "获取某个目录/模块的概况：包含哪些子目录、核心文件（按函数重要性排序）、以及高 fan_in 的入口函数（被多模块调用的函数）。用于理解某个模块的整体结构和职责。",
            "parameters": {
                "type": "object",
                "properties": {
                    "module_path": {"type": "string", "description": "模块路径（如 'ggml'、'src'、'tools/server'）"},
                },
                "required": ["module_path"],
            },
        },
    },
    # 层2：函数级
    {
        "type": "function",
        "function": {
            "name": "search_functions",
            "description": "按名字模糊搜索图中的函数节点，返回名字、文件路径、fan_in/fan_out。适合当知道函数名关键词时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name_pattern": {"type": "string", "description": "函数名关键词（支持 substring）"},
                    "limit": {"type": "integer", "description": "返回条数，默认 8", "default": 8},
                },
                "required": ["name_pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_functions_by_content",
            "description": "通过函数名或文件路径中的关键词搜索函数。当精确函数名搜索失败时使用，可发现文件路径中包含关键词的函数（如搜 'blas' 找到 ggml/src/ggml-cpu/llamafile/sgemm.cpp 中的矩阵函数）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "搜索关键词（可匹配函数名或文件路径）"},
                    "limit": {"type": "integer", "description": "返回条数，默认 8", "default": 8},
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_function_detail",
            "description": "获取指定函数的详细注解（功能描述、工作流角色）以及 fan_in/fan_out 和文件路径。",
            "parameters": {
                "type": "object",
                "properties": {
                    "func_name": {"type": "string", "description": "函数的完整名称"},
                },
                "required": ["func_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_callers",
            "description": "查询谁调用了指定函数（上游调用者列表）。用于影响分析：修改该函数会影响哪些地方。",
            "parameters": {
                "type": "object",
                "properties": {
                    "func_name": {"type": "string"},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["func_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_callees",
            "description": "查询指定函数调用了哪些函数（下游依赖）。用于理解函数的内部流程和数据流。",
            "parameters": {
                "type": "object",
                "properties": {
                    "func_name": {"type": "string"},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["func_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_file_functions",
            "description": "列出某个文件中的所有函数（按重要性排序）。用于理解模块结构和职责。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "文件路径关键词（支持 substring）"},
                    "limit": {"type": "integer", "default": 15},
                },
                "required": ["file_path"],
            },
        },
    },
    # 层2.5：变量级
    {
        "type": "function",
        "function": {
            "name": "search_variables",
            "description": "按名称搜索全局变量/常量/枚举值节点。用于查询某个变量在代码库中的定义位置和基本信息。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name_pattern": {"type": "string", "description": "变量名关键词"},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["name_pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_attributes",
            "description": "按名称搜索 Class 成员（struct field / class member variable）。用于查询某个结构体字段或类成员变量的定义、所属类、文件位置。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name_pattern": {"type": "string", "description": "成员名关键词"},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["name_pattern"],
            },
        },
    },
    # 辅助：读文件内容
    {
        "type": "function",
        "function": {
            "name": "read_file_lines",
            "description": "读取指定文件的指定行范围代码内容，用于查看结构体字段定义、变量声明等具体实现细节。当已知文件路径和行号时使用此工具获取实际代码。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "文件路径（支持部分匹配，如 'llama-grammar.h'）"},
                    "start_line": {"type": "integer", "description": "起始行号（从 1 开始）"},
                    "end_line": {"type": "integer", "description": "结束行号（可选，默认为 start_line + 10）"},
                },
                "required": ["file_path", "start_line"],
            },
        },
    },
    # 层3：Issue/PR
    {
        "type": "function",
        "function": {
            "name": "search_issues",
            "description": "按关键词搜索 GitHub Issue 节点。用于 Bug 排查、Feature 需求、性能问题等从真实 Issue 出发的问题。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "搜索关键词"},
                    "limit": {"type": "integer", "default": 5},
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_issue_detail",
            "description": "获取指定 Issue 编号的详细信息，包括描述和关联的修复 PR。",
            "parameters": {
                "type": "object",
                "properties": {
                    "issue_num": {"type": "string", "description": "Issue 编号，如 '18258'"},
                },
                "required": ["issue_num"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "semantic_search",
            "description": "根据查询语句语义搜索相关函数注解。用于模糊的功能性问题，如'负责量化的函数有哪些'。使用 embedding 相似度匹配而非字符串包含。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "查询语句"},
                    "limit": {"type": "integer", "default": 6},
                },
                "required": ["query"],
            },
        },
    },
]

TOOL_MAP = {
    "find_module_by_keyword": tool_find_module_by_keyword,
    "get_directory_tree": tool_get_directory_tree,
    "get_module_overview": tool_get_module_overview,
    "search_functions": tool_search_functions,
    "search_functions_by_content": tool_search_functions_by_content,
    "get_function_detail": tool_get_function_detail,
    "get_callers": tool_get_callers,
    "get_callees": tool_get_callees,
    "get_file_functions": tool_get_file_functions,
    "search_variables": tool_search_variables,
    "search_attributes": tool_search_attributes,
    "read_file_lines": tool_read_file_lines,
    "search_issues": tool_search_issues,
    "get_issue_detail": tool_get_issue_detail,
    "semantic_search": tool_search_by_embedding,
}

SYSTEM_PROMPT = """你是 llama.cpp 代码库的专家助手，可以访问代码知识图谱工具。

回答策略：
1. 先分析问题类型，决定需要哪些信息
2. 调用合适的工具获取信息（可多次调用，每次聚焦一个目标）
3. 信息足够后，用中文生成清晰、结构化的答案

工具使用指南：

【最重要：Issue/Bug 类问题 — 必须找到 Issue 详情】
- 如果问题是"我遇到了这个问题：XXX"、"llama.cpp 中出现了这个问题：XXX"、
  "我在使用 llama.cpp 时遇到了类似的问题：XXX"、"llama.cpp 推理时遇到性能问题：XXX"
  → 这类问题必须找到对应 Issue 才能回答
- 步骤：
  1. 用 search_issues(关键词) 搜索，关键词只取描述中最核心的 1-2 个词（如 "ROCm illegal memory access" 就取 "ROCm illegal memory"）
  2. 找到 Issue 编号后，立刻用 get_issue_detail(issue_num) 获取详情（标题、描述、关联 PR）
  3. 可以多次 search_issues，每次用不同的关键词组合尝试
  4. 绝对不能在没有获取 Issue 详情的情况下生成答案
- 示例：
  "我遇到了这个问题：ROCm illegal memory access with -sm row"
  → search_issues("ROCm illegal memory") → 得到 Issue #16799
  → get_issue_detail("16799") → 获取详情

【模块/目录类问题 — 用层1工具定位】
- 问题涉及某个技术领域但没有具体函数名（如 MoE、vulkan、quantization）
  → 用 find_module_by_keyword 先定位到相关目录/文件
- 问题涉及项目结构、模块划分（如 ggml 是什么、server 模块在哪）
  → 用 get_directory_tree 或 get_module_overview
- 问题涉及某个目录/模块的整体功能
  → 用 get_module_overview

【函数类问题 — 用层2工具定位】
- 问题涉及具体函数名 → 用 search_functions 或 get_function_detail
- 问题涉及某文件中的函数 → 用 get_file_functions
- 问题涉及模糊概念（如"有哪些量化函数"、"实现内存拷贝的函数"）→ 用 semantic_search（基于 embedding 语义匹配）

【变量/常量/枚举值问题 — 用层2.5工具】
- 问题涉及某变量/常量的定义、取值或使用 → 用 search_variables
- 问题涉及某 struct field / class 成员变量的定义 → search_attributes 定位到文件和行号后，必须立即用 read_file_lines(file_path, start_line, end_line) 读取具体代码来回答细节（类型、设计意图、使用方式等）

【调用关系类问题 — 用层2/层3工具】
- 问题涉及影响分析（改这个会影响谁）→ 用 get_callers
- 问题涉及内部流程（这个函数怎么工作的）→ 用 get_callees

【Feature需求类问题】
- 先用 search_issues 搜关键词，再用 get_issue_detail 确认详情

注意：
- 不要编造未在工具结果中出现的信息
- 引用查到的函数名、Issue 编号等具体证据
- Issue 类问题：搜到 Issue 编号后必须立即调用 get_issue_detail
- 如果工具返回了文件路径和行号，必须用 read_file_lines 读取实际代码内容
"""


# ---------------------------------------------------------------------------
# Agent 循环
# ---------------------------------------------------------------------------

def run_agent(driver, question: str) -> tuple[str, list[dict], int, dict]:
    """运行一次 agent 循环，返回 (最终答案, 工具调用轨迹, 步数, token统计)"""
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)

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
            tools=TOOLS,
            tool_choice="auto",
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

            fn = TOOL_MAP.get(name)
            if fn:
                try:
                    result = fn(driver, **args)
                except Exception as e:
                    result = f"工具调用出错: {e}"
            else:
                result = f"未知工具: {name}"

            if len(result) > TOOL_RESULT_MAX:
                result = result[:TOOL_RESULT_MAX] + "\n...(已截断)"

            trajectory.append({"tool": name, "args": args, "result_snippet": result[:200]})
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })

            # Fallback: 当 graph 搜索返回空时，自动用 semantic_search 重试
            if "未找到" in result and name != "semantic_search" and name != "search_by_embedding":
                kw = args.get("keyword") or args.get("name_pattern") or args.get("query") or ""
                if kw and len(kw) >= 3:
                    # 提取关键词中的实义词
                    words = [w for w in kw.replace("-", " ").split() if len(w) >= 3]
                    if words:
                        fallback_kw = " ".join(words[:2])
                        try:
                            fallback_result = tool_search_by_embedding(driver, fallback_kw, limit=5)
                            if "未找到" not in fallback_result and fallback_result != "语义搜索失败：RAG 索引不存在。":
                                # 以 user message 注入 fallback 结果（不是真正的 tool response）
                                messages.append({
                                    "role": "user",
                                    "content": f"[自动补充：图搜索未找到相关结果，语义搜索 '{fallback_kw}' 的结果如下]\n{fallback_result}",
                                })
                                trajectory.append({
                                    "tool": "semantic_search (auto-fallback)",
                                    "args": {"query": fallback_kw, "limit": 5},
                                    "result_snippet": fallback_result[:200],
                                })
                        except Exception:
                            pass  # fallback 失败则忽略

        steps += 1

    resp = client.chat.completions.create(
        model=LLM_MODEL,
        messages=messages + [{"role": "user",
                               "content": "请根据以上工具查询结果，现在给出最终答案。"}],
        tools=TOOLS,
        tool_choice="auto",
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


# ---------------------------------------------------------------------------
# 证据匹配
# ---------------------------------------------------------------------------

def parse_evidence(evidence_str: str) -> set[str]:
    """从 Evidence 列解析出 Issue 编号、文件路径、模块名"""
    if not evidence_str or not isinstance(evidence_str, str):
        return set()
    out = set()
    for m in re.finditer(r'Issue #(\d+)', evidence_str):
        out.add(f"Issue #{m.group(1)}")
    for m in re.finditer(r'PR #(\d+)', evidence_str):
        out.add(f"PR #{m.group(1)}")
    for m in re.finditer(r'(/[\w\-\./]+\.(?:cpp|c|h|hpp))(?::\d+)?', evidence_str):
        out.add(m.group(1))
    for seg in evidence_str.replace(";", ",").replace(":", ",").split(","):
        seg = seg.strip()
        if "/" in seg and any(seg.endswith(ext) for ext in (".cpp", ".c", ".h", ".hpp")):
            out.add(seg)
    for m in re.finditer(r'([\w\-\.]+)\s*模块', evidence_str):
        out.add(m.group(1))
    return out


def calc_evidence_hit(gold_evidence: set[str], trajectory: list[dict],
                       generated_answer: str = "") -> dict:
    """计算轨迹中工具调用涉及的 Evidence 命中情况"""
    if not gold_evidence:
        return {"evidence_count": 0, "hit_count": 0, "recall": None}

    mentioned = set()
    all_text = generated_answer
    for step in trajectory:
        args_str = json.dumps(step.get("args", {}))
        result_str = step.get("result_snippet", "")
        all_text += " " + args_str + " " + result_str

    for m in re.finditer(r'Issue #(\d+)', all_text):
        mentioned.add(f"Issue #{m.group(1)}")
    for m in re.finditer(r'PR #(\d+)', all_text):
        mentioned.add(f"PR #{m.group(1)}")
    for word in re.findall(r'[\w/\-\.]+\.(?:cpp|c|h|hpp)', all_text):
        mentioned.add(word)
    for m in re.finditer(r'\b(llama|ggml|common|examples|server|vulkan|cuda|metal)\b', all_text, re.IGNORECASE):
        mentioned.add(m.group(1).lower())

    hits = set()
    for g in gold_evidence:
        for m in mentioned:
            if g == m or m.endswith("/" + g) or g.endswith("/" + m) or g in m or m in g:
                hits.add(g)
                break

    recall = len(hits) / len(gold_evidence) if gold_evidence else None
    return {
        "evidence_count": len(gold_evidence),
        "hit_count": len(hits),
        "recall": round(recall, 3) if recall is not None else None,
        "hits": sorted(hits),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", type=Path,
                        default=Path(__file__).resolve().parent / "llama_cpp_QA_v3.csv")
    parser.add_argument("--output", type=Path,
                        default=Path(__file__).resolve().parent / "experiments" / "agent_qa_results.json")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    if not OPENAI_API_KEY:
        print("ERROR: OPENAI_API_KEY 未设置", file=sys.stderr)
        sys.exit(1)

    import pandas as pd
    df = pd.read_csv(args.csv, encoding="utf-8")
    if args.limit > 0:
        df = df.head(args.limit)

    driver = get_driver()
    driver.verify_connectivity()

    results = []
    total = len(df)

    q_col = "问题" if "问题" in df.columns else "具体问题"
    ev_col = "Evidence"
    subcat_col = "子类"
    cat_col = "类别"

    for i, (idx, row) in enumerate(df.iterrows()):
        question = str(row.get(q_col, ""))
        reference = str(row.get("答案", ""))
        evidence_raw = str(row.get(ev_col, ""))
        subcat = str(row.get(subcat_col, ""))
        category = str(row.get(cat_col, ""))
        entity = str(row.get("实体", ""))

        print(f"[{i+1}/{total}] {category}/{subcat}: {question[:60]}...", flush=True)

        t0 = time.time()
        try:
            answer, trajectory, steps, token_usage = run_agent(driver, question)
            latency = round(time.time() - t0, 2)
            gold = parse_evidence(evidence_raw)
            ev_hit = calc_evidence_hit(gold, trajectory, answer)
            results.append({
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
                "token_usage": token_usage,
                "错误": None,
            })
            print(f"  steps={steps} latency={latency}s ev_recall={ev_hit['recall']}", flush=True)
        except Exception as e:
            print(f"  ERROR: {e}", flush=True)
            import traceback
            traceback.print_exc()
            results.append({
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
                "延迟_s": 0,
                "token_usage": {},
                "错误": str(e),
            })

        if (i + 1) % 5 == 0:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  [checkpoint] 已保存 {i+1} 条", flush=True)

    driver.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n完成：{len(results)} 条 → {args.output}")


if __name__ == "__main__":
    main()
