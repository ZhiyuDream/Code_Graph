"""
调用关系解析器：利用 callee_line 精确匹配重载函数。

修复点（相比原 graph_builder.py）：
1. 利用 clangd 20 提供的 callee_line 做精确匹配
2. 函数重载时不盲目选第一个，标记 AMBIGUOUS
3. 输出诊断信息（ambiguous/unresolved 数量）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .models import FunctionSymbol, RawCall, ResolvedCalls

logger = logging.getLogger(__name__)


@dataclass
class FunctionLookup:
    """函数查找索引。"""
    # (file_path, name, start_line) -> func_id
    by_location: dict[tuple[str, str, int], str] = field(default_factory=dict)
    # (file_path, name) -> list of func_id，按 start_line 排序
    by_name: dict[tuple[str, str], list[tuple[str, int, int]]] = field(default_factory=dict)
    # func_id -> FunctionSymbol
    by_id: dict[str, FunctionSymbol] = field(default_factory=dict)
    # name -> list of (func_id, file_path) 全局索引
    by_name_global: dict[str, list[tuple[str, str]]] = field(default_factory=dict)


def build_function_lookup(functions: list[FunctionSymbol]) -> FunctionLookup:
    """
    从函数列表构建查找索引。

    Returns:
        FunctionLookup: 支持按位置、按名称、按全局名称查找
    """
    lookup = FunctionLookup()
    for f in functions:
        lookup.by_location[(f.file_path, f.name, f.start_line)] = f.id
        lookup.by_id[f.id] = f

        key = (f.file_path, f.name)
        if key not in lookup.by_name:
            lookup.by_name[key] = []
        lookup.by_name[key].append((f.id, f.start_line, f.end_line))

        # 全局名称索引
        if f.name not in lookup.by_name_global:
            lookup.by_name_global[f.name] = []
        lookup.by_name_global[f.name].append((f.id, f.file_path))

    # 按 start_line 排序
    for key in lookup.by_name:
        lookup.by_name[key].sort(key=lambda x: x[1])

    return lookup


def _get_caller_dir(file_path: str) -> str:
    """获取文件所在目录（不含文件名）。"""
    import os
    return os.path.dirname(file_path)


def resolve_call(
    lookup: FunctionLookup,
    raw: RawCall,
    caller_id: str,
) -> tuple[str | None, str | None, list[str] | None]:
    """
    解析单个 RawCall。

    Returns:
        (callee_id, status, candidates)
        - status: "resolved" | "global_match" | "ambiguous" | "unresolved"
        - candidates: 当 status="ambiguous" 时返回候选 id 列表
    """
    callee_fp = raw.callee_file_path or raw.file_path
    callee_name = raw.callee_name
    callee_line = raw.callee_line

    key = (callee_fp, callee_name)
    candidates = lookup.by_name.get(key, [])

    if not candidates:
        # 尝试用 file_path（caller 所在文件）查找
        key_fallback = (raw.file_path, callee_name)
        candidates = lookup.by_name.get(key_fallback, [])
        if candidates:
            callee_fp = raw.file_path

    # ---- 本地（同文件）匹配 ----
    if candidates:
        # 1. 精确匹配：利用 callee_line 在候选中找包含该行的函数
        if callee_line is not None:
            for func_id, start_line, end_line in candidates:
                if start_line <= callee_line <= end_line:
                    logger.debug("Resolved by line: %s -> %s (line=%d, range=%d-%d)",
                                 caller_id, callee_name, callee_line, start_line, end_line)
                    return func_id, "resolved", None

        # 2. name-only 唯一匹配
        if len(candidates) == 1:
            func_id = candidates[0][0]
            logger.debug("Resolved by unique name: %s -> %s", caller_id, callee_name)
            return func_id, "resolved", None

        # 3. 多候选：AMBIGUOUS
        candidate_ids = [c[0] for c in candidates]
        logger.debug("Ambiguous call: %s -> %s (%d candidates)",
                     caller_id, callee_name, len(candidate_ids))
        return None, "ambiguous", candidate_ids

    # ---- 全局名称匹配（P1 改进）----
    global_candidates = lookup.by_name_global.get(callee_name, [])
    if not global_candidates:
        logger.debug("Unresolved call: %s -> %s (no candidates)", caller_id, callee_name)
        return None, "unresolved", None

    # 全局唯一匹配
    if len(global_candidates) == 1:
        func_id = global_candidates[0][0]
        logger.debug("Global match (unique): %s -> %s", caller_id, callee_name)
        return func_id, "global_match", None

    # 多全局候选：优先同目录
    caller_dir = _get_caller_dir(raw.file_path)
    same_dir = [(fid, fp) for fid, fp in global_candidates
                if _get_caller_dir(fp) == caller_dir]
    if len(same_dir) == 1:
        func_id = same_dir[0][0]
        logger.debug("Global match (same dir): %s -> %s", caller_id, callee_name)
        return func_id, "global_match", None

    # 仍有多候选：AMBIGUOUS（返回所有全局候选）
    candidate_ids = [fid for fid, _ in global_candidates]
    logger.debug("Ambiguous global call: %s -> %s (%d candidates)",
                 caller_id, callee_name, len(candidate_ids))
    return None, "ambiguous", candidate_ids


def resolve_all_calls(
    functions: list[FunctionSymbol],
    raw_calls: list[RawCall],
) -> ResolvedCalls:
    """
    批量解析所有 RawCall。

    Args:
        functions: 全局函数列表
        raw_calls: 原始调用记录列表

    Returns:
        ResolvedCalls
    """
    lookup = build_function_lookup(functions)
    resolved = ResolvedCalls()
    global_match_count = 0

    for raw in raw_calls:
        if raw.caller_index < 0 or raw.caller_index >= len(functions):
            logger.warning("Invalid caller_index: %d (total functions: %d)",
                           raw.caller_index, len(functions))
            continue

        caller = functions[raw.caller_index]
        caller_id = caller.id

        if not caller_id:
            logger.warning("Function at index %d has no id", raw.caller_index)
            continue

        callee_id, status, candidates = resolve_call(lookup, raw, caller_id)

        # 跳过自调用（caller == callee）
        if status in ("resolved", "global_match") and callee_id == caller_id:
            logger.debug("Self-call skipped: %s", caller_id)
            continue

        if status in ("resolved", "global_match") and callee_id:
            resolved.calls.append((caller_id, callee_id))
            if status == "global_match":
                global_match_count += 1
        elif status == "ambiguous" and candidates:
            resolved.ambiguous.append((caller_id, raw.callee_name, candidates))
        else:
            resolved.unresolved.append((caller_id, raw.callee_name))

    logger.info(
        "Call resolution: %d resolved, %d global_match, %d ambiguous, %d unresolved",
        len(resolved.calls) - global_match_count, global_match_count,
        len(resolved.ambiguous), len(resolved.unresolved)
    )
    return resolved
