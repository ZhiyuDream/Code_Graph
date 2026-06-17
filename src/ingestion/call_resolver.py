"""
调用关系解析器：直接信任 clangd 的 callHierarchy 结果做简单映射。

clangd 20+ 的 callHierarchy/outgoingCalls 已经返回精确的 callee 信息
（uri + range），不需要参数个数消歧等启发式规则。

本模块只做简单的位置/名称匹配，将 RawCall 映射到已知函数。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
    """从函数列表构建查找索引。"""
    lookup = FunctionLookup()
    for f in functions:
        lookup.by_location[(f.file_path, f.name, f.start_line)] = f.id
        lookup.by_id[f.id] = f

        key = (f.file_path, f.name)
        if key not in lookup.by_name:
            lookup.by_name[key] = []
        lookup.by_name[key].append((f.id, f.start_line, f.end_line))

        if f.name not in lookup.by_name_global:
            lookup.by_name_global[f.name] = []
        lookup.by_name_global[f.name].append((f.id, f.file_path))

    for key in lookup.by_name:
        lookup.by_name[key].sort(key=lambda x: x[1])

    return lookup


def _find_caller_by_location(
    lookup: FunctionLookup,
    file_path: str,
    line: int,
) -> FunctionSymbol | None:
    """通过文件路径和行号查找包含该行的函数。"""
    # 精确匹配 start_line
    for (fp, name, start_line), func_id in lookup.by_location.items():
        if fp == file_path and start_line == line:
            return lookup.by_id[func_id]

    # 范围匹配
    for (fp, name), candidates in lookup.by_name.items():
        if fp == file_path:
            for func_id, start_line, end_line in candidates:
                if start_line <= line <= end_line:
                    return lookup.by_id[func_id]

    return None


def resolve_call(
    lookup: FunctionLookup,
    raw: RawCall,
    caller_id: str,
    repo_root: str | Path | None = None,
) -> tuple[str | None, str | None, list[str] | None]:
    """
    解析单个 RawCall。

    策略：
    1. 优先用 clangd 给的精确位置 (callee_file_path, callee_line) 匹配
    2. 同文件按名称匹配
    3. 全局按名称匹配
    4. 多候选时标记为 ambiguous（不再用参数个数消歧）

    Returns:
        (callee_id, status, candidates)
        - status: "resolved" | "global_match" | "ambiguous" | "unresolved"
    """
    callee_name = raw.callee_name

    # ---- 1. 精确位置匹配（clangd 20+ 直接给出）----
    if raw.callee_file_path and raw.callee_line is not None:
        # 先在同文件中查找
        key = (raw.callee_file_path, callee_name)
        candidates = lookup.by_name.get(key, [])
        for func_id, start_line, end_line in candidates:
            if start_line <= raw.callee_line <= end_line:
                logger.debug("Resolved by exact location: %s -> %s", caller_id, callee_name)
                return func_id, "resolved", None

        # 跨文件查找（clangd 给出的 callee_file_path 可能来自不同文件）
        global_candidates = lookup.by_name_global.get(callee_name, [])
        for func_id, fp in global_candidates:
            func = lookup.by_id.get(func_id)
            if func and func.start_line <= raw.callee_line <= func.end_line:
                logger.debug("Resolved by global location: %s -> %s", caller_id, callee_name)
                return func_id, "global_match", None

    # ---- 2. 同文件名称匹配 ----
    callee_fp = raw.callee_file_path or raw.file_path
    key = (callee_fp, callee_name)
    candidates = lookup.by_name.get(key, [])

    if candidates:
        # 唯一匹配
        if len(candidates) == 1:
            func_id = candidates[0][0]
            logger.debug("Resolved by unique name (same file): %s -> %s", caller_id, callee_name)
            return func_id, "resolved", None

        # 多候选（通常是重载函数）→ ambiguous，不再消歧
        candidate_ids = [c[0] for c in candidates]
        logger.debug("Ambiguous call (same file): %s -> %s (%d candidates)",
                     caller_id, callee_name, len(candidate_ids))
        return None, "ambiguous", candidate_ids

    # ---- 3. 全局名称匹配 ----
    global_candidates = lookup.by_name_global.get(callee_name, [])
    if not global_candidates:
        logger.debug("Unresolved call: %s -> %s (no candidates)", caller_id, callee_name)
        return None, "unresolved", None

    if len(global_candidates) == 1:
        func_id = global_candidates[0][0]
        logger.debug("Global match (unique): %s -> %s", caller_id, callee_name)
        return func_id, "global_match", None

    # 多全局候选 → ambiguous
    all_candidate_ids = [fid for fid, _ in global_candidates]
    logger.debug("Ambiguous global call: %s -> %s (%d candidates)",
                 caller_id, callee_name, len(all_candidate_ids))
    return None, "ambiguous", all_candidate_ids


def resolve_all_calls(
    functions: list[FunctionSymbol],
    raw_calls: list[RawCall],
    repo_root: str | Path | None = None,
) -> ResolvedCalls:
    """
    批量解析所有 RawCall。

    Args:
        functions: 全局函数列表
        raw_calls: 原始调用记录列表
        repo_root: 仓库根目录（保留参数兼容性，实际不再使用）

    Returns:
        ResolvedCalls（含 external_calls）
    """
    lookup = build_function_lookup(functions)
    resolved = ResolvedCalls()
    global_match_count = 0

    # 收集仓库内所有函数名（用于判断是否为外部调用）
    all_known_names: set[str] = {f.name for f in functions}

    for raw in raw_calls:
        if raw.caller_index < 0:
            # incomingCalls 模式：通过 (file_path, line) 查找 caller
            caller = _find_caller_by_location(lookup, raw.file_path, raw.line)
            if caller is None:
                logger.debug("Cannot resolve caller at %s:%d", raw.file_path, raw.line)
                continue
            caller_id = caller.id
        elif raw.caller_index >= len(functions):
            logger.warning("Invalid caller_index: %d (total functions: %d)",
                           raw.caller_index, len(functions))
            continue
        else:
            caller = functions[raw.caller_index]
            caller_id = caller.id

        if not caller_id:
            logger.warning("Function at index %d has no id", raw.caller_index)
            continue

        callee_id, status, candidates = resolve_call(lookup, raw, caller_id, repo_root=repo_root)

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
            # unresolved：区分外部调用 vs 真正找不到的
            if raw.callee_name and raw.callee_name not in all_known_names:
                resolved.external_calls.append((caller_id, raw.callee_name))
            else:
                resolved.unresolved.append((caller_id, raw.callee_name))

    logger.info(
        "Call resolution: %d resolved, %d global_match, %d ambiguous, %d unresolved, %d external",
        len(resolved.calls) - global_match_count, global_match_count,
        len(resolved.ambiguous), len(resolved.unresolved), len(resolved.external_calls),
    )
    return resolved
