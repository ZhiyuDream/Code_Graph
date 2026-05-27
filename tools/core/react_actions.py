"""
ReAct Action 执行器

将 ReAct 循环中的各种 action 执行逻辑独立封装，
便于扩展和维护。每种 action 对应一个独立函数，
execute_action() 做统一分发。
"""
from __future__ import annotations

from typing import Dict, List

from tools.search import (
    expand_call_chain,
    expand_same_file,
    expand_same_class,
)
from tools.search.grep_search_v2 import grep_codebase
from tools.search.semantic_search import search_functions_by_text
from tools.search.query_rewriter import get_grep_keywords


def _normalize_search_results(raw_results: List[Dict], source_tag: str) -> List[Dict]:
    """
    将各种搜索结果统一规范化为 ReAct collected["functions"] 的格式。

    统一字段: name, file, text, score, source, start_line, end_line
    """
    normalized = []
    seen_keys = set()
    
    for r in raw_results:
        if not r:
            continue
        # 不同搜索工具返回的字段名略有差异，做兼容处理
        name = r.get("name", "")
        file_path = r.get("file", "")
        
        # 处理 grep 结果：可能没有 name，用 file + start_line 组合作为唯一标识
        lines_data = r.get("lines", [])
        if lines_data and isinstance(lines_data, list) and len(lines_data) > 0:
            start_line = lines_data[0].get("line", lines_data[0].get("start_line", 0))
            end_line = lines_data[-1].get("line", lines_data[-1].get("end_line", start_line))
            # 从 lines 拼接 text
            text_parts = [line.get("content", line.get("text", "")) for line in lines_data]
            text = "\n".join(text_parts)
        else:
            start_line = r.get("start_line", r.get("line", 0))
            end_line = r.get("end_line", r.get("line", start_line))
            text = r.get("text", r.get("content", ""))
        
        if not name:
            # grep 结果用 file:line 作为 name
            name = f"{file_path.split('/')[-1]}:{start_line}"
        
        # 去重键：避免同一个文件的多个匹配被当作不同函数
        dedup_key = f"{file_path}:{start_line}"
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)
        
        func = {
            "name": name,
            "file": file_path,
            "text": text,
            "score": r.get("score", 0.5),
            "source": source_tag,
            "start_line": start_line,
            "end_line": end_line,
        }
        normalized.append(func)
    return normalized


def action_expand_callers(target: str) -> List[Dict]:
    """扩展目标函数的调用者（上游）"""
    result = expand_call_chain(target, "callers")
    return _normalize_search_results(result.get("functions", []), f"caller_of_{target}")


def action_expand_callees(target: str) -> List[Dict]:
    """扩展目标函数的被调用者（下游）"""
    result = expand_call_chain(target, "callees")
    return _normalize_search_results(result.get("functions", []), f"callee_of_{target}")


def action_expand_same_file(target: str) -> List[Dict]:
    """扩展同一文件中的其他函数"""
    result = expand_same_file(target)
    return _normalize_search_results(result.get("functions", []), f"same_file_of_{target}")


def action_expand_same_class(target: str) -> List[Dict]:
    """扩展同一类中的其他方法"""
    result = expand_same_class(target)
    return _normalize_search_results(result.get("functions", []), f"same_class_of_{target}")


def action_grep_search(query: str, repo_root: str = "", limit: int = 5) -> List[Dict]:
    """
    使用 grep 搜索代码库。
    如果 query 是自然语言，先通过 query_rewriter 提取关键词。
    """
    keywords = get_grep_keywords(query, use_llm=False)
    all_results = []
    seen_names = set()

    # 优先用提取的标识符做精确搜索
    for kw in keywords[:3]:
        raw = grep_codebase(kw, codebase_path=repo_root or None, limit=limit)
        for r in raw:
            name = r.get("name", "")
            if name and name in seen_names:
                continue
            if name:
                seen_names.add(name)
            all_results.append(r)
        if len(all_results) >= limit * 2:
            break

    return _normalize_search_results(all_results[:limit], f"grep:{query}")


def action_semantic_search(query: str, top_k: int = 5) -> List[Dict]:
    """使用 embedding 语义搜索"""
    raw = search_functions_by_text(query, top_k=top_k)
    return _normalize_search_results(raw, f"semantic:{query}")


# Action 名称 → 执行函数的映射表
_ACTION_REGISTRY = {
    "expand_callers": action_expand_callers,
    "expand_callees": action_expand_callees,
    "expand_same_file": action_expand_same_file,
    "expand_same_class": action_expand_same_class,
    "grep_search": action_grep_search,
    "semantic_search": action_semantic_search,
}


def execute_action(action: str, target: str = "", query: str = "", repo_root: str = "") -> List[Dict]:
    """
    执行指定的 ReAct action，返回规范化后的函数列表。

    Args:
        action: action 名称
        target: 目标函数名（用于扩展类 action）
        query: 搜索查询（用于搜索类 action）
        repo_root: 代码库根目录（用于 grep_search）

    Returns:
        List[Dict]: 函数列表，每个函数包含 name, file, text, score, source 等字段
    """
    handler = _ACTION_REGISTRY.get(action)
    if handler is None:
        return []

    # 根据 action 类型传递不同参数
    if action in ("grep_search",):
        return handler(query=query or target, repo_root=repo_root)
    elif action in ("semantic_search",):
        return handler(query=query or target)
    else:
        # 扩展类 action 只需要 target
        return handler(target=target)


def get_registered_action_names() -> List[str]:
    """返回当前已注册的所有 action 名称"""
    return list(_ACTION_REGISTRY.keys())
