import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT / "src"))   # 核心库
sys.path.insert(0, str(_ROOT))             # 根目录（config.py）

#!/usr/bin/env python3
"""
注解消融实验：验证建图时为 Function 节点添加 LLM 注解对回答质量的影响。

两个模式（唯一区别是 get_function_detail 工具返回内容）：
  --no-ann  : 无注解模式 — 工具返回原始代码片段
  （默认）   : 有注解模式 — 工具返回原始代码片段 + annotation_json

用法：
  python agent_qa_ablation.py            # 有注解模式
  python agent_qa_ablation.py --no-ann   # 无注解模式
  python agent_qa_ablation.py --limit 3  # 只跑前3题
"""
from __future__ import annotations

import json
import sys
import time
import argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from neo4j_writer import get_driver
from config import NEO4J_DATABASE, OPENAI_API_KEY, OPENAI_BASE_URL, LLM_MODEL, REPO_ROOT

MAX_STEPS = 6
# 已取消截断：设置为极大值，允许完整读取源码和完整工具返回
TOOL_RESULT_MAX = 999999
CODE_MAX = 999999  # 单个函数原始代码最大字符数


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------

def _run(driver, cypher: str, params: dict = None) -> list[dict]:
    with driver.session(database=NEO4J_DATABASE) as s:
        r = s.run(cypher, params or {})
        return [dict(rec) for rec in r]


def _read_source(file_path: str, start_line: int, end_line: int) -> str:
    """从 REPO_ROOT 读取指定行范围的源码，返回字符串。"""
    if not REPO_ROOT:
        return "(REPO_ROOT 未配置，无法读取源码)"
    full_path = Path(REPO_ROOT) / file_path
    if not full_path.exists():
        return f"(文件不存在: {file_path})"
    try:
        lines = full_path.read_text(encoding="utf-8", errors="replace").splitlines()
        # start_line/end_line 是 1-indexed
        snippet = "\n".join(lines[start_line - 1: end_line])
        if len(snippet) > CODE_MAX:
            snippet = snippet[:CODE_MAX] + "\n...(代码已截断)"
        return snippet
    except Exception as e:
        return f"(读取源码失败: {e})"


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


def tool_get_function_detail(driver, func_name: str, *, with_annotation: bool) -> str:
    """获取函数的原始代码（及可选注解）、fan_in/out、文件路径"""
    rows = _run(driver, """
        MATCH (f:Function {name: $name})
        RETURN f.name AS name, f.file_path AS file,
               f.fan_in AS fan_in, f.fan_out AS fan_out,
               f.start_line AS sl, f.end_line AS el,
               f.annotation_json AS ann
        LIMIT 3
    """, {"name": func_name})
    if not rows:
        return f"未找到函数 '{func_name}'。"
    out = []
    for r in rows:
        code = _read_source(r["file"], r["sl"], r["el"])
        lines_count = (r["el"] or 0) - (r["sl"] or 0) + 1
        header = (f"函数: {r['name']}\n文件: {r['file']} "
                  f"(行 {r['sl']}-{r['el']}, {lines_count}行)\n"
                  f"fan_in={r['fan_in']} fan_out={r['fan_out']}\n")
        body = f"--- 源码 ---\n{code}"
        if with_annotation and r.get("ann"):
            try:
                d = json.loads(r["ann"]) if isinstance(r["ann"], str) else r["ann"]
                summary = d.get("summary", "")[:400]
                workflow = d.get("workflow_role", "")[:200]
                # V2 字段
                v2_fields = []
                if d.get("schema_version", 1) >= 2:
                    if d.get("is_wrapper"):
                        v2_fields.append(f"⚠️ 薄封装函数（委托下游）")
                    if d.get("failure_modes"):
                        fm = "; ".join(d["failure_modes"][:3])
                        v2_fields.append(f"已知崩溃点: {fm}")
                    depth = d.get("call_depth_hint")
                    if depth is not None:
                        v2_fields.append(f"调用深度提示: {depth}")
                    conf = d.get("confidence", "medium")
                    v2_fields.append(f"注解置信度: {conf}")
            except Exception:
                summary = str(r["ann"])[:400]
                workflow = ""
                v2_fields = []
            ann_str = f"\n--- 注解 ---\n功能摘要: {summary}"
            if workflow:
                ann_str += f"\n工作流角色: {workflow}"
            if v2_fields:
                ann_str += "\n" + "\n".join(v2_fields)
            body += ann_str
        out.append(header + body)
    return "\n\n".join(out)


def tool_get_call_chain(driver, func_name: str, depth: int = 2, limit: int = 15) -> str:
    """
    获取函数的完整调用链，支持多跳追溯（2-hop+）。
    返回从上游调用者到目标函数的调用路径。
    depth=1: 直接调用者
    depth=2: 2-hop调用链（如 A → B → target）
    """
    if depth < 1:
        depth = 1
    if depth > 3:
        depth = 3  # 最多3跳，避免性能问题

    rows = _run(driver, """
        MATCH path = (caller:Function)-[:CALLS*1..%d]->(f:Function {name: $name})
        WITH path, nodes(path) AS ns
        WHERE size(ns) >= 2 AND ns[0].name <> f.name
        WITH path, length(path) AS hop_depth,
             [n IN ns | n.name] AS chain_names,
             [n IN ns | n.file_path] AS chain_files
        ORDER BY hop_depth DESC, size([x IN chain_names WHERE x = chain_names[0]]) DESC
        LIMIT $lim
        RETURN chain_names[0] AS upstream,
               chain_files[0] AS upstream_file,
               hop_depth AS depth,
               [i IN range(1, size(chain_names)-1) | chain_names[i]] AS mid_chain,
               chain_names[-1] AS target
    """ % depth, {"name": func_name, "lim": limit})

    if not rows:
        return f"没有找到 '{func_name}' 的调用链（depth={depth}）。可能是入口函数或图谱中无调用关系。"

    out = [f"调用链追溯 '{func_name}'（depth=%d，共%d条）:\n" % (depth, len(rows))]
    for r in rows:
        upstream = r["upstream"]
        upstream_file = r["upstream_file"] or ""
        depth = r["depth"]
        mid = r["mid_chain"] or []

        if depth == 1:
            chain_str = upstream
        else:
            chain_str = " → ".join([upstream] + mid + [func_name])

        wrapper_note = ""
        if upstream_file:
            wrapper_note = f" ({upstream_file})"
        out.append(f"  [{depth}-hop] {chain_str}{wrapper_note}")

    return "\n".join(out)


def tool_get_callers(driver, func_name: str, limit: int = 10, expand_wrapper: bool = True) -> str:
    """
    谁调用了该函数（上游调用者）。

    如果 expand_wrapper=True，当直接调用者是 wrapper 函数时，
    会自动追溯其上游调用者（2-hop），帮助理解完整的调用链。
    """
    rows = _run(driver, """
        MATCH (caller:Function)-[:CALLS]->(f:Function {name: $name})
        OPTIONAL MATCH (caller)-[:CALLS]->(wrapper:Function)
        WHERE wrapper.annotation_json IS NOT NULL
        WITH caller, collect(DISTINCT wrapper.name) AS wrappers
        RETURN caller.name AS name, caller.file_path AS file,
               caller.annotation_json AS ann,
               wrappers AS possible_wrappers
        LIMIT $lim
    """, {"name": func_name, "lim": limit})

    if not rows:
        return f"没有找到调用 '{func_name}' 的函数（可能是入口函数或 fan_in=0）。"

    out = [f"调用 {func_name} 的函数（{len(rows)} 个）:\n"]
    has_wrapper_expansion = False

    for r in rows:
        name = r["name"]
        file = r["file"] or ""
        wrappers = r["possible_wrappers"] or []

        # 检查是否是 wrapper 函数
        is_wrapper = False
        if r.get("ann"):
            try:
                ann = json.loads(r["ann"]) if isinstance(r["ann"], str) else r["ann"]
                is_wrapper = ann.get("is_wrapper", False)
            except Exception:
                pass

        line = f"  - {name} ({file})"
        if is_wrapper:
            line += " [wrapper]"
            if expand_wrapper and wrappers:
                line += f" → 下游: {', '.join(wrappers[:3])}"
        out.append(line)

        # 如果是 wrapper 且启用了 expand_wrapper，尝试追溯 2-hop
        if is_wrapper and expand_wrapper:
            sub_rows = _run(driver, """
                MATCH (upstream:Function)-[:CALLS*1..2]->(w:Function {name: $name})
                WITH upstream, length(apoc.cypher.runFirstColumn(
                    'MATCH path = (upstream)-[:CALLS*1..2]->(w) RETURN path',
                    {name: $name})) AS hop
                WHERE hop >= 1
                RETURN upstream.name AS name, upstream.file_path AS file
                LIMIT 3
            """, {"name": name})
            for sub in sub_rows:
                sub_name = sub["name"]
                sub_file = sub["file"] or ""
                out.append(f"    └─ 上游: {sub_name} ({sub_file}) [2-hop via {name}]")
                has_wrapper_expansion = True

    result = "\n".join(out)
    if has_wrapper_expansion:
        result += "\n\n注: [wrapper] 标记的函数已展开其上游调用者（2-hop追溯）。"
    return result


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


def tool_get_file_functions(driver, file_path: str, limit: int = 15,
                            *, with_annotation: bool) -> str:
    """列出某文件中的函数（按 fan_in 降序）"""
    rows = _run(driver, """
        MATCH (f:Function)
        WHERE f.file_path CONTAINS $fp
        RETURN f.name AS name, f.fan_in AS fan_in, f.fan_out AS fan_out,
               f.start_line AS sl, f.end_line AS el, f.annotation_json AS ann
        ORDER BY f.fan_in DESC LIMIT $lim
    """, {"fp": file_path, "lim": limit})
    if not rows:
        return f"未找到路径包含 '{file_path}' 的函数。"
    lines = []
    for r in rows:
        line_info = f"行{r['sl']}-{r['el']}" if r.get("sl") else ""
        base = f"{r['name']} fan_in={r['fan_in']} fan_out={r['fan_out']} {line_info}"
        if with_annotation and r.get("ann"):
            try:
                d = json.loads(r["ann"]) if isinstance(r["ann"], str) else r["ann"]
                summary = d.get("summary", "")[:100]
                base += f"\n  注解: {summary}"
            except Exception:
                pass
        lines.append(base)
    return f"文件 {file_path} 中的函数 ({len(lines)} 个):\n" + "\n".join(lines)


def tool_search_issues(driver, keyword: str, limit: int = 5) -> str:
    """按关键词搜索 Issue 节点（标题+body），也支持直接输入 Issue 编号"""
    import re
    if re.match(r'^\d+$', keyword.strip()):
        return tool_get_issue_detail(driver, keyword.strip())
    rows = _run(driver, """
        MATCH (i:Issue)
        WHERE toLower(i.title) CONTAINS toLower($kw)
              OR toLower(coalesce(i.body, '')) CONTAINS toLower($kw)
        RETURN i.number AS num, i.title AS title
        ORDER BY i.number DESC LIMIT $lim
    """, {"kw": keyword, "lim": limit})
    if not rows:
        return f"未找到包含关键词 '{keyword}' 的 Issue。"
    lines = [f"Issue #{r['num']}: {r['title']}" for r in rows]
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


def tool_semantic_search(driver, query: str, limit: int = 6,
                         *, with_annotation: bool) -> str:
    """语义搜索相关函数（有注解：匹配 annotation_json；无注解：按函数名匹配）"""
    stop = {"的", "了", "是", "在", "有", "和", "为", "什么", "如何", "怎么", "哪些", "哪个"}
    words = [w for w in query.replace("？", " ").replace("，", " ").split()
             if w not in stop and len(w) > 1]
    kw = words[0] if words else query[:10]

    if with_annotation:
        rows = _run(driver, """
            MATCH (f:Function)
            WHERE f.annotation_json IS NOT NULL
                  AND (toLower(f.annotation_json) CONTAINS toLower($kw)
                       OR toLower(f.name) CONTAINS toLower($kw))
            RETURN f.name AS name, f.file_path AS file, f.fan_in AS fan_in,
                   f.annotation_json AS ann
            ORDER BY f.fan_in DESC LIMIT $lim
        """, {"kw": kw, "lim": limit})
    else:
        rows = _run(driver, """
            MATCH (f:Function)
            WHERE toLower(f.name) CONTAINS toLower($kw)
            RETURN f.name AS name, f.file_path AS file, f.fan_in AS fan_in,
                   NULL AS ann
            ORDER BY f.fan_in DESC LIMIT $lim
        """, {"kw": kw, "lim": limit})

    if not rows:
        return f"语义搜索 '{query}' 未找到相关函数。"
    out = []
    for r in rows:
        line = f"{r['name']} ({r['file']})"
        if with_annotation and r.get("ann"):
            try:
                d = json.loads(r["ann"]) if isinstance(r["ann"], str) else r["ann"]
                line += f"\n  {d.get('summary', '')[:150]}"
            except Exception:
                pass
        out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 工具注册表（根据 with_annotation 动态绑定）
# ---------------------------------------------------------------------------

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "search_functions",
            "description": "按名字模糊搜索图中的函数节点，返回名字、文件路径、fan_in/fan_out。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name_pattern": {"type": "string", "description": "函数名关键词（支持 substring）"},
                    "limit": {"type": "integer", "default": 8},
                },
                "required": ["name_pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_function_detail",
            "description": "获取指定函数的原始源码、fan_in/fan_out 和文件路径（有注解模式还包含功能摘要）。",
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
            "name": "get_call_chain",
            "description": "追溯函数的完整调用链，支持多跳（2-hop+）。当需要理解调用链、根因追溯、影响范围时使用。返回从上游到目标的调用路径。",
            "parameters": {
                "type": "object",
                "properties": {
                    "func_name": {"type": "string", "description": "目标函数名"},
                    "depth": {"type": "integer", "default": 2, "description": "追溯深度，默认2-hop，最多3-hop"},
                    "limit": {"type": "integer", "default": 15, "description": "最多返回多少条调用链"},
                },
                "required": ["func_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_callers",
            "description": "查询谁调用了指定函数（上游调用者列表）。用于影响分析。注：当调用者是wrapper函数时，会自动展开其下游。",
            "parameters": {
                "type": "object",
                "properties": {
                    "func_name": {"type": "string"},
                    "limit": {"type": "integer", "default": 10},
                    "expand_wrapper": {"type": "boolean", "default": True, "description": "是否对wrapper函数展开2-hop上游"},
                },
                "required": ["func_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_callees",
            "description": "查询指定函数调用了哪些函数（下游依赖）。用于理解内部流程。",
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
    {
        "type": "function",
        "function": {
            "name": "search_issues",
            "description": "按关键词搜索 GitHub Issue 节点。用于 Bug 排查、Feature 需求、性能问题。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string"},
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
            "description": "根据查询语句搜索相关函数。用于模糊的功能性问题。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": 6},
                },
                "required": ["query"],
            },
        },
    },
]

SYSTEM_PROMPT_WITH_ANN = """你是 llama.cpp 代码库的专家助手，可以访问代码知识图谱工具。
工具会返回函数的原始源码和 LLM 预生成的功能注解摘要。

回答策略：
1. 先分析问题类型，决定需要哪些信息
2. 调用合适的工具获取信息（可多次调用，每次聚焦一个目标）
3. 信息足够后，用中文生成清晰、结构化的答案

工具使用指南：
- 问题涉及具体 Issue 编号（如 "#18351"）→ 优先用 get_issue_detail
- 问题涉及函数名 → 用 search_functions 或 get_function_detail
- 问题涉及 bug/性能/feature → 用 search_issues（关键词尽量简短，1-2 个词）
- 问题涉及模块/文件 → 用 get_file_functions
- 问题涉及**调用链追溯**（如"谁调用了这个函数"、"调用链是什么"）→ 优先用 **get_call_chain**（支持2-hop+）
- 问题涉及**影响范围**（如"哪些函数受影响"、"改动影响哪些调用者"）→ 用 get_callers + **get_call_chain**（depth=2）
- 问题涉及**下游依赖**（如"这个函数调用了哪些"）→ 用 get_callees

注意：
- 不要编造未在工具结果中出现的信息
- 引用查到的函数名、Issue 编号等具体证据
- get_call_chain 最多支持3-hop，depth=2 时覆盖大多数场景
"""

SYSTEM_PROMPT_NO_ANN = """你是 llama.cpp 代码库的专家助手，可以访问代码知识图谱工具。
工具会返回函数的原始 C/C++ 源码（无预生成摘要），请自行阅读代码理解其功能。

回答策略：
1. 先分析问题类型，决定需要哪些信息
2. 调用合适的工具获取信息（可多次调用，每次聚焦一个目标）
3. 阅读返回的源码，理解函数功能后，用中文生成清晰、结构化的答案

工具使用指南：
- 问题涉及具体 Issue 编号（如 "#18351"）→ 优先用 get_issue_detail
- 问题涉及函数名 → 用 search_functions 或 get_function_detail（会返回源码）
- 问题涉及 bug/性能/feature → 用 search_issues（关键词尽量简短，1-2 个词）
- 问题涉及模块/文件 → 用 get_file_functions，再对关键函数调用 get_function_detail
- 问题涉及**调用链追溯**（如"谁调用了这个函数"、"调用链是什么"）→ 优先用 **get_call_chain**（支持2-hop+）
- 问题涉及**影响范围** → 用 get_callers + **get_call_chain**（depth=2）
- 问题涉及**下游依赖** → 用 get_callees

注意：
- 不要编造未在工具结果中出现的信息
- 引用查到的函数名、Issue 编号等具体证据
- get_call_chain 最多支持3-hop，depth=2 时覆盖大多数场景
"""


# ---------------------------------------------------------------------------
# Agent 循环
# ---------------------------------------------------------------------------

def run_agent(driver, question: str, with_annotation: bool) -> tuple[str, list[dict], int, dict]:
    """运行一次 agent 循环，返回 (最终答案, 工具调用轨迹, 步数, token_usage)"""
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)

    system_prompt = SYSTEM_PROMPT_WITH_ANN if with_annotation else SYSTEM_PROMPT_NO_ANN

    # 动态绑定工具函数（注入 with_annotation 参数）
    def _get_function_detail(driver, func_name):
        return tool_get_function_detail(driver, func_name, with_annotation=with_annotation)

    def _get_file_functions(driver, file_path, limit=15):
        return tool_get_file_functions(driver, file_path, limit, with_annotation=with_annotation)

    def _semantic_search(driver, query, limit=6):
        return tool_semantic_search(driver, query, limit, with_annotation=with_annotation)

    tool_map = {
        "search_functions": tool_search_functions,
        "get_function_detail": _get_function_detail,
        "get_call_chain": tool_get_call_chain,
        "get_callers": tool_get_callers,
        "get_callees": tool_get_callees,
        "get_file_functions": _get_file_functions,
        "search_issues": tool_search_issues,
        "get_issue_detail": tool_get_issue_detail,
        "semantic_search": _semantic_search,
    }

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": question},
    ]
    trajectory = []
    steps = 0
    token_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    for _ in range(MAX_STEPS):
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=messages,
            tools=TOOLS_SCHEMA,
            tool_choice="auto",
            max_tokens=800,
            timeout=60,
        )
        msg = resp.choices[0].message
        if resp.usage:
            token_usage["prompt_tokens"] += resp.usage.prompt_tokens
            token_usage["completion_tokens"] += resp.usage.completion_tokens
            token_usage["total_tokens"] += resp.usage.total_tokens

        if not msg.tool_calls:
            return msg.content.strip() if msg.content else "(无答案)", trajectory, steps, token_usage

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

            fn = tool_map.get(name)
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

        steps += 1

    # 超过 MAX_STEPS，强制生成最终答案
    resp = client.chat.completions.create(
        model=LLM_MODEL,
        messages=messages + [{"role": "user", "content": "请根据以上工具查询结果，现在给出最终答案。"}],
        max_tokens=800,
        timeout=60,
    )
    if resp.usage:
        token_usage["prompt_tokens"] += resp.usage.prompt_tokens
        token_usage["completion_tokens"] += resp.usage.completion_tokens
        token_usage["total_tokens"] += resp.usage.total_tokens
    final = resp.choices[0].message.content or "(无答案)"
    return final.strip(), trajectory, steps, token_usage


# ---------------------------------------------------------------------------
# 证据匹配（复用 agent_qa.py 逻辑）
# ---------------------------------------------------------------------------

def parse_evidence(evidence_str: str) -> set[str]:
    if not evidence_str or not isinstance(evidence_str, str):
        return set()
    import re
    out = set()
    for m in re.finditer(r'Issue #(\d+)', evidence_str):
        out.add(f"Issue #{m.group(1)}")
    for m in re.finditer(r'PR #(\d+)', evidence_str):
        out.add(f"PR #{m.group(1)}")
    for seg in evidence_str.replace(";", ",").split(","):
        seg = seg.strip()
        if "/" in seg and any(seg.endswith(ext) for ext in (".cpp", ".c", ".h", ".hpp")):
            out.add(seg)
    return out


def calc_evidence_hit(gold_evidence: set[str], trajectory: list[dict],
                      generated_answer: str = "") -> dict:
    if not gold_evidence:
        return {"evidence_count": 0, "hit_count": 0, "recall": None}
    import re
    all_text = generated_answer
    for step in trajectory:
        all_text += " " + json.dumps(step.get("args", {})) + " " + step.get("result_snippet", "")
    mentioned = set()
    for m in re.finditer(r'Issue #(\d+)', all_text):
        mentioned.add(f"Issue #{m.group(1)}")
    for m in re.finditer(r'PR #(\d+)', all_text):
        mentioned.add(f"PR #{m.group(1)}")
    for word in re.findall(r'[\w/\-\.]+\.(?:cpp|c|h|hpp)', all_text):
        mentioned.add(word)
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
    parser.add_argument("--no-ann", action="store_true",
                        help="无注解模式：工具只返回原始代码，不含 annotation_json")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--workers", type=int, default=1,
                        help="并行工作线程数（默认1）")
    args = parser.parse_args()

    with_annotation = not args.no_ann
    mode_label = "with_ann" if with_annotation else "no_ann"

    if args.output is None:
        args.output = Path(__file__).resolve().parent / "experiments" / f"ablation_{mode_label}.json"

    if not OPENAI_API_KEY:
        print("ERROR: OPENAI_API_KEY 未设置", file=sys.stderr)
        sys.exit(1)

    import pandas as pd
    df = pd.read_csv(args.csv, encoding="utf-8")
    if args.limit > 0:
        df = df.head(args.limit)

    # 兼容新旧 CSV 列名
    q_col = "具体问题" if "具体问题" in df.columns else "问题"
    # 优先 "参考答案"，fall back 到 "答案"（v4 数据集）
    ans_col = "参考答案" if "参考答案" in df.columns else ("答案" if "答案" in df.columns else "参考答案")
    ev_col = "Evidence"
    subcat_col = "二级分类" if "二级分类" in df.columns else "子类"
    cat_col = "一级分类" if "一级分类" in df.columns else "类别"
    intent_col = "意图" if "意图" in df.columns else None

    print(f"模式: {'有注解（代码+annotation）' if with_annotation else '无注解（仅代码原文）'}")
    print(f"输出: {args.output}")
    print(f"并行: {args.workers} workers, {len(df)} 题")

    def process_row(idx, row):
        """处理单行，每个worker用自己的driver"""
        question = str(row.get(q_col, ""))
        reference = str(row.get(ans_col, ""))
        evidence_raw = str(row.get(ev_col, ""))
        subcat = str(row.get(subcat_col, ""))
        category = str(row.get(cat_col, ""))
        intent = str(row.get(intent_col, "")) if intent_col else ""
        entity = str(row.get("实体名称", "")) if "实体名称" in df.columns else ""

        driver = get_driver()
        try:
            driver.verify_connectivity()
        except Exception as e:
            return {
                "index": int(idx), "模式": mode_label, "类别": category, "子类": subcat,
                "意图": intent, "实体名称": entity, "问题": question,
                "参考答案": reference, "生成答案": "", "Evidence": evidence_raw,
                "证据命中": {}, "工具调用步数": 0, "工具轨迹": [], "延迟_s": 0,
                "token_usage": {}, "错误": f"driver连接失败: {e}",
            }

        t0 = time.time()
        try:
            answer, trajectory, steps, token_usage = run_agent(driver, question, with_annotation)
            latency = round(time.time() - t0, 2)
            gold = parse_evidence(evidence_raw)
            ev_hit = calc_evidence_hit(gold, trajectory, answer)
            return {
                "index": int(idx), "模式": mode_label, "类别": category, "子类": subcat,
                "意图": intent, "实体名称": entity, "问题": question,
                "参考答案": reference, "生成答案": answer, "Evidence": evidence_raw,
                "证据命中": ev_hit, "工具调用步数": steps, "工具轨迹": trajectory,
                "延迟_s": latency, "token_usage": token_usage, "错误": None,
            }
        except Exception as e:
            return {
                "index": int(idx), "模式": mode_label, "类别": category, "子类": subcat,
                "意图": intent, "实体名称": entity, "问题": question,
                "参考答案": reference, "生成答案": "", "Evidence": evidence_raw,
                "证据命中": {}, "工具调用步数": 0, "工具轨迹": [], "延迟_s": round(time.time()-t0, 2),
                "token_usage": {}, "错误": str(e),
            }
        finally:
            driver.close()

    rows_data = [(idx, row) for idx, row in df.iterrows()]
    results = [None] * len(rows_data)

    if args.workers == 1:
        # 串行
        for i, (idx, row) in enumerate(rows_data):
            print(f"[{i+1}/{len(rows_data)}] ", end="", flush=True)
            result = process_row(idx, row)
            results[i] = result
            if result["错误"]:
                print(f"ERROR: {result['错误']}", flush=True)
            else:
                print(f"steps={result['工具调用步数']} latency={result['延迟_s']}s "
                      f"tokens={result['token_usage'].get('total_tokens', '?')}", flush=True)
            if (i + 1) % 5 == 0:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps([r for r in results if r is not None], ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"  [checkpoint] 已保存 {i+1} 条", flush=True)
    else:
        # 并行
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(process_row, idx, row): i for i, (idx, row) in enumerate(rows_data)}
            for future in as_completed(futures):
                i = futures[future]
                result = future.result()
                results[i] = result
                if result["错误"]:
                    print(f"[{i+1}/{len(rows_data)}] ERROR: {result['错误']}", flush=True)
                else:
                    print(f"[{i+1}/{len(rows_data)}] steps={result['工具调用步数']} latency={result['延迟_s']}s "
                          f"tokens={result['token_usage'].get('total_tokens', '?')}", flush=True)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n完成：{len(results)} 条 → {args.output}")


if __name__ == "__main__":
    main()
