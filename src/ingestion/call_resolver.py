"""
调用关系解析器：利用 callee_line + 参数个数匹配 精确解析重载函数。

修复点（相比原 graph_builder.py）：
1. 利用 clangd 20 提供的 callee_line 做精确匹配
2. 函数重载时不盲目选第一个，标记 AMBIGUOUS
3. P3: 利用 fromRanges 读调用点源码，提取参数个数辅助消歧
4. P3: 利用 callee_detail（签名）提取参数个数辅助消歧
5. 输出诊断信息（ambiguous/unresolved 数量）
"""
from __future__ import annotations

import logging
import re
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
    # name -> list of (func_id, param_count) 参数个数索引（用于重载消歧）
    by_param_count: dict[str, dict[int, list[str]]] = field(default_factory=dict)


def build_function_lookup(functions: list[FunctionSymbol]) -> FunctionLookup:
    """
    从函数列表构建查找索引。

    Returns:
        FunctionLookup: 支持按位置、按名称、按全局名称、按参数个数查找
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

        # 参数个数索引（用于重载消歧）
        if f.param_count is not None:
            if f.name not in lookup.by_param_count:
                lookup.by_param_count[f.name] = {}
            lookup.by_param_count[f.name].setdefault(f.param_count, []).append(f.id)

    # 按 start_line 排序
    for key in lookup.by_name:
        lookup.by_name[key].sort(key=lambda x: x[1])

    return lookup


def _get_caller_dir(file_path: str) -> str:
    """获取文件所在目录（不含文件名）。"""
    import os
    return os.path.dirname(file_path)


# ── P3: 参数个数提取（用于重载消歧）─────────────────────────────


def _count_args(args_str: str) -> int:
    """从参数字符串数顶层逗号个数，返回参数个数。"""
    args_str = args_str.strip()
    if not args_str:
        return 0
    depth = 0
    count = 1
    for c in args_str:
        if c in "(<{":
            depth += 1
        elif c in ")>}":
            depth -= 1
        elif c == "," and depth == 0:
            count += 1
    return count


def _extract_balanced_parens(line: str, start: int) -> tuple[str, bool]:
    """从 start 位置（应在 '(' 后）开始找匹配的 ')'。

    Returns:
        (args_str, complete) — args_str 是不含外层括号的参数字符串，
        complete=True 表示在本行找到了匹配的 ')'。
    """
    depth = 1
    i = start
    while i < len(line) and depth > 0:
        if line[i] == "(":
            depth += 1
        elif line[i] == ")":
            depth -= 1
        i += 1
    if depth == 0:
        return line[start:i - 1], True
    return line[start:], False


def _extract_multi_line_args(lines: list[str], start_line: int, start_col: int) -> str:
    """跨行提取参数列表（从 start_line 行的 start_col 开始，前面已有一个 '('）。"""
    parts = [lines[start_line][start_col:]]
    depth = 1
    line_idx = start_line + 1
    while depth > 0 and line_idx < len(lines):
        line = lines[line_idx]
        for ch in line:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    parts.append(line[:line.index(")")])
                    break
        else:
            parts.append(line)
        line_idx += 1
    return " ".join(parts)


def _extract_param_count_from_detail(detail: str) -> int | None:
    """从 clangd detail（函数签名）中提取参数个数。

    支持的格式:
      - "int (int, int)" → 2
      - "void ()" → 0
      - "void foo(const std::string&)" → 1
    """
    if not detail:
        return None
    detail = detail.strip()
    if "(" not in detail or ")" not in detail:
        return None
    depth = 0
    start_idx = None
    for i, ch in enumerate(detail):
        if ch == "(":
            if depth == 0:
                start_idx = i
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0 and start_idx is not None:
                params = detail[start_idx + 1 : i].strip()
                if not params or params == "void":
                    return 0
                return _count_args(params)
    return None


def _parse_args_from_source(
    file_path: str,
    from_ranges: list[dict],
    callee_name: str,
    repo_root: str | Path | None,
) -> int | None:
    """从调用点源码解析参数个数。

    Args:
        file_path: caller 文件路径（相对路径）
        from_ranges: clangd outgoingCalls 返回的 fromRanges（调用位置）
        callee_name: 被调用函数名
        repo_root: 仓库根目录

    Returns:
        参数个数，或 None（解析失败）
    """
    if not repo_root or not from_ranges or not callee_name:
        return None

    try:
        abs_path = Path(repo_root) / file_path
        if not abs_path.exists():
            return None
        lines = abs_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return None

    for fr in from_ranges:
        start = fr.get("start", {})
        line_idx = start.get("line", 0)  # 0-based
        char_idx = start.get("character", 0)

        if line_idx >= len(lines):
            continue

        source_line = lines[line_idx]

        # 在 char_idx 附近搜索 callee_name( ... )
        # 给 fromRanges 一个窗口（前后 150 字符）
        window_start = max(0, char_idx - 150)
        window_end = min(len(source_line), char_idx + 200)
        window = source_line[window_start:window_end]

        # 找 callee_name 后面跟着 ( 的模式
        pattern = re.escape(callee_name) + r"\s*\("
        matches = list(re.finditer(pattern, window))
        if not matches:
            continue

        # 选最接近 char_idx 的那个匹配
        best_match = None
        best_dist = float("inf")
        for m in matches:
            match_center = window_start + m.start() + len(m.group(0))
            dist = abs(match_center - char_idx)
            if dist < best_dist:
                best_dist = dist
                best_match = m

        if not best_match:
            continue

        # 从 ( 后开始找匹配的 )
        paren_start = window_start + best_match.end()
        args_str, complete = _extract_balanced_parens(source_line, paren_start)

        if not complete and line_idx + 1 < len(lines):
            args_str = _extract_multi_line_args(lines, line_idx, paren_start)

        return _count_args(args_str)

    return None


def _find_caller_by_location(
    lookup: FunctionLookup,
    file_path: str,
    line: int,
) -> FunctionSymbol | None:
    """通过文件路径和行号查找包含该行的函数。"""
    # 先用 (file_path, line) 做精确匹配
    for (fp, name, start_line), func_id in lookup.by_location.items():
        if fp == file_path and start_line == line:
            return lookup.by_id[func_id]

    # 再找包含该行的函数
    for (fp, name), candidates in lookup.by_name.items():
        if fp == file_path:
            for func_id, start_line, end_line in candidates:
                if start_line <= line <= end_line:
                    return lookup.by_id[func_id]

    return None


def _try_resolve_by_param_count(
    lookup: FunctionLookup,
    raw: RawCall,
    candidate_ids: list[str],
    repo_root: str | Path | None,
) -> str | None:
    """尝试用参数个数匹配消歧。返回唯一的匹配 func_id，或 None。"""
    # 1. 从源码调用点解析参数个数（最可靠）
    call_param_count: int | None = None
    if raw.from_ranges:
        call_param_count = _parse_args_from_source(
            raw.file_path, raw.from_ranges, raw.callee_name, repo_root
        )

    # 2. 源码解析失败，尝试从 callee_detail（签名）解析
    if call_param_count is None and raw.callee_detail:
        call_param_count = _extract_param_count_from_detail(raw.callee_detail)

    if call_param_count is None:
        return None

    # 3. 用参数个数过滤候选
    matched = []
    for fid in candidate_ids:
        func = lookup.by_id.get(fid)
        if func and func.param_count == call_param_count:
            matched.append(fid)

    if len(matched) == 1:
        return matched[0]

    # 4. 参数个数匹配后仍有多候选 → 无法消歧
    return None


def resolve_call(
    lookup: FunctionLookup,
    raw: RawCall,
    caller_id: str,
    repo_root: str | Path | None = None,
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

        # 3. P3: 多候选时尝试参数个数消歧
        candidate_ids = [c[0] for c in candidates]
        resolved_by_params = _try_resolve_by_param_count(
            lookup, raw, candidate_ids, repo_root
        )
        if resolved_by_params:
            logger.debug("Resolved by param count: %s -> %s", caller_id, callee_name)
            return resolved_by_params, "resolved", None

        # 4. 仍有多候选：AMBIGUOUS
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

    # P3: 全局多候选时尝试参数个数消歧
    all_candidate_ids = [fid for fid, _ in global_candidates]
    resolved_by_params = _try_resolve_by_param_count(
        lookup, raw, all_candidate_ids, repo_root
    )
    if resolved_by_params:
        logger.debug("Global match by param count: %s -> %s", caller_id, callee_name)
        return resolved_by_params, "global_match", None

    # 仍有多候选：AMBIGUOUS（返回所有全局候选）
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
        repo_root: 仓库根目录，用于从源码解析调用点参数个数

    Returns:
        ResolvedCalls（含 external_calls）
    """
    lookup = build_function_lookup(functions)
    resolved = ResolvedCalls()
    global_match_count = 0

    # 收集仓库内所有函数名（用于判断是否为外部调用）
    all_known_names: set[str] = set()
    for f in functions:
        all_known_names.add(f.name)

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
