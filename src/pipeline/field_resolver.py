"""
字段解析器：补充和修正 struct/class 成员变量（Field）的 parent 关联。

背景：clangd 20 的 textDocument/documentSymbol 有时返回扁平列表，
Field 符号不与 Class 关联。field_resolver 用 class 的 [start_line, end_line]
范围做精确匹配，修正 orphan field 的 scope_class_index。

原则：只用 clangd 20 的数据，不引入 libclang。
"""
from __future__ import annotations

import logging
from typing import Any

from .models import ClassSymbol, FileResult, VariableSymbol

logger = logging.getLogger(__name__)


def _find_enclosing_class(
    classes: list[ClassSymbol],
    file_path: str,
    line: int,
) -> int | None:
    """
    找到包含指定行的 class 索引。
    要求：class.start_line <= line <= class.end_line
    """
    best_idx = None
    best_line = -1
    for i, c in enumerate(classes):
        if c.file_path != file_path:
            continue
        if c.start_line <= line <= c.end_line and c.start_line > best_line:
            best_idx = i
            best_line = c.start_line
    return best_idx


def resolve_file_fields(file_result: FileResult) -> FileResult:
    """
    对单个文件的 variables 做字段归属修正。

    规则：
    1. 已有 scope_class_index 的 member：保持不变
    2. 无 scope_class_index 但 kind="member" 的变量：用 class 范围精确匹配
    3. 仍无法匹配：保持原样（由 graph_assembler 按 parent_type="File" 处理）
    """
    if not file_result.classes or not file_result.variables:
        return file_result

    fixed_vars: list[VariableSymbol] = []
    changed = 0

    for v in file_result.variables:
        if v.kind != "member" or v.scope_class_index is not None:
            fixed_vars.append(v)
            continue

        # 用 class 的 [start, end] 范围匹配
        enclosing = _find_enclosing_class(
            file_result.classes, v.file_path, v.start_line
        )
        if enclosing is not None:
            fixed_vars.append(VariableSymbol(
                id=v.id,
                name=v.name,
                file_path=v.file_path,
                start_line=v.start_line,
                kind=v.kind,
                scope_function_index=v.scope_function_index,
                scope_class_index=enclosing,
                start_character=v.start_character,
            ))
            changed += 1
        else:
            fixed_vars.append(v)

    if changed > 0:
        logger.debug("Resolved %d orphan fields in %s", changed, file_result.file_path)

    return FileResult(
        file_path=file_result.file_path,
        functions=file_result.functions,
        classes=file_result.classes,
        variables=fixed_vars,
        calls=file_result.calls,
        var_refs=file_result.var_refs,
        raw=file_result.raw,
    )


def enrich_file_results(file_results: list[FileResult]) -> list[FileResult]:
    """
    批量修正所有文件的字段归属。

    Args:
        file_results: symbol_extractor 产出的文件结果列表

    Returns:
        修正后的 FileResult 列表
    """
    enriched: list[FileResult] = []
    total_resolved = 0

    for fr in file_results:
        resolved = resolve_file_fields(fr)
        resolved_count = sum(
            1 for v in resolved.variables
            if v.kind == "member" and v.scope_class_index is not None
        )
        total_resolved += resolved_count
        enriched.append(resolved)

    logger.info(
        "Field resolution: %d member fields with parent class across %d files",
        total_resolved, len(file_results)
    )

    return enriched
