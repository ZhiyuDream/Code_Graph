"""
控制流提取器：从 C++ 函数体中提取 if/else/switch/try-catch/return 等控制流语句。

用于覆盖 state_control, error_path, parameter_check 类证据。
策略：基于正则启发式提取，无需 AST 解析器。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ControlFlowBlock:
    """控制流块节点。"""
    id: str
    function_id: str
    file_path: str
    line: int
    type: str  # "if" | "else_if" | "else" | "switch" | "case" | "try" | "catch" | "return"
    condition: str = ""  # 条件表达式（如 "ctx == nullptr"）
    is_error_path: bool = False  # 是否为错误/异常返回路径


# 控制流正则模式
# 注意：C++ 条件可能跨多行，这里用单行匹配，截取第一行内容
# 支持 `} else if (...)` 和 `} catch (...)` 这种前面带 `}` 的情况
# if 模式也支持跨行条件（无右括号的情况）
_CONTROL_PATTERNS = [
    # if (condition) — 支持跨行条件，无右括号也匹配
    (r'^\s*\}?\s*if\s*\(\s*(.*)', 'if'),
    # else if (condition)
    (r'^\s*\}?\s*else\s+if\s*\(\s*(.*)', 'else_if'),
    # else
    (r'^\s*\}?\s*else\s*(?:\{|\b|$)', 'else'),
    # switch (condition)
    (r'^\s*\}?\s*switch\s*\(\s*(.*)', 'switch'),
    # case xxx:
    (r'^\s*case\s+(.+?):', 'case'),
    # default:
    (r'^\s*default\s*:', 'default'),
    # try
    (r'^\s*\}?\s*try\s*(?:\{|\b|$)', 'try'),
    # catch (type var)
    (r'^\s*\}?\s*catch\s*\(\s*(.*)', 'catch'),
]

# 错误返回模式（可以出现在行内任何位置）
_ERROR_RETURN_PATTERNS = [
    r'return\s+nullptr\s*;',
    r'return\s+NULL\s*;',
    r'return\s+false\s*;',
    r'return\s+-1\s*;',
    r'return\s+\{\}\s*;',
    r'return\s+0\s*;',
    r'throw\s+',
    r'return\s+""\s*;',
]
_ERROR_RETURN_RE = re.compile('|'.join(_ERROR_RETURN_PATTERNS))

# 提取 return 后面的值
_RETURN_RE = re.compile(r'return\s+(.*?);')

# 简单返回值的排除列表（不创建控制流节点）
_SIMPLE_RETURNS = {
    'true', 'false', '0', '1', '-1', '""', "''", '{}', 'nullptr', 'NULL',
}


def _is_simple_expression(expr: str) -> bool:
    """判断是否为简单表达式（不需要记录控制流）。"""
    expr = expr.strip()
    if expr in _SIMPLE_RETURNS:
        return True
    # 简单的变量名或常量（不含运算符）
    if re.match(r'^[a-zA-Z_]\w*$', expr):
        return True
    # 简单的字符串字面量
    if re.match(r'^"[^"]*"$', expr):
        return True
    return False


def extract_control_flow_for_function(
    function_id: str,
    file_path: str,
    start_line: int,
    end_line: int,
    file_lines: list[str],
) -> list[ControlFlowBlock]:
    """
    从单个函数体中提取控制流语句。

    Args:
        function_id: 函数节点 ID
        file_path: 相对路径
        start_line: 函数开始行（1-based，含）
        end_line: 函数结束行（1-based，含）
        file_lines: 整个文件的所有行（0-based 索引）

    Returns:
        ControlFlowBlock 列表
    """
    blocks: list[ControlFlowBlock] = []
    start_idx = max(0, start_line - 1)
    end_idx = min(len(file_lines), end_line)

    for line_idx in range(start_idx, end_idx):
        line = file_lines[line_idx]
        line_no = line_idx + 1

        # 跳过空行和纯注释行
        stripped = line.strip()
        if not stripped or stripped.startswith('//') or stripped.startswith('*'):
            continue

        matched = False

        # 1. 匹配 if/else/switch/try/catch
        for pattern, cf_type in _CONTROL_PATTERNS:
            m = re.match(pattern, line)
            if m:
                condition = m.group(1).strip() if m.lastindex else ""
                # 去掉可能的尾部 ) 或 ){
                condition = re.sub(r'\)\s*\{?\s*$', '', condition).strip()
                # 截断过长的条件（可能跨行导致）
                if len(condition) > 120:
                    condition = condition[:120] + "..."
                blocks.append(ControlFlowBlock(
                    id=f"{file_path}:{start_line}:{line_no}:{cf_type}",
                    function_id=function_id,
                    file_path=file_path,
                    line=line_no,
                    type=cf_type,
                    condition=condition,
                ))
                matched = True
                break  # 一行只匹配一个模式

        # 2. 匹配错误返回路径（行内任何位置，包括 if 语句内部）
        if _ERROR_RETURN_RE.search(line):
            rm = _RETURN_RE.search(line)
            ret_val = rm.group(1).strip() if rm else ""
            blocks.append(ControlFlowBlock(
                id=f"{file_path}:{start_line}:{line_no}:return",
                function_id=function_id,
                file_path=file_path,
                line=line_no,
                type="return",
                condition=ret_val,
                is_error_path=True,
            ))
            continue

        if matched:
            continue

        # 3. 匹配一般 return（非简单表达式）
        if 'return' in stripped:
            rm = _RETURN_RE.search(line)
            if rm:
                ret_val = rm.group(1).strip()
                if ret_val and not _is_simple_expression(ret_val):
                    blocks.append(ControlFlowBlock(
                        id=f"{file_path}:{start_line}:{line_no}:return",
                        function_id=function_id,
                        file_path=file_path,
                        line=line_no,
                        type="return",
                        condition=ret_val,
                        is_error_path=False,
                    ))

    return blocks


def extract_all_control_flow(
    file_results: list[Any],
    repo_root: str = "",
) -> list[ControlFlowBlock]:
    """
    从所有文件结果中提取控制流块。

    Args:
        file_results: FileResult 列表
        repo_root: 仓库根目录（用于读取源码）

    Returns:
        全局 ControlFlowBlock 列表
    """
    from pathlib import Path

    all_blocks: list[ControlFlowBlock] = []

    for fr in file_results:
        if not fr.functions:
            continue

        # 读取文件内容
        abs_path = Path(repo_root) / fr.file_path if repo_root else Path(fr.file_path)
        try:
            lines = abs_path.read_text(encoding='utf-8', errors='replace').splitlines()
        except Exception:
            continue

        for func in fr.functions:
            blocks = extract_control_flow_for_function(
                function_id=func.id or f"{func.file_path}:{func.name}:{func.start_line}",
                file_path=func.file_path,
                start_line=func.start_line,
                end_line=func.end_line,
                file_lines=lines,
            )
            all_blocks.extend(blocks)

    logger.info("Control flow extraction: %d blocks from %d files",
                len(all_blocks), len(file_results))
    return all_blocks
