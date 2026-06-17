"""
控制流提取器：从 C++ 函数体中提取 if/else/switch/try-catch/return 等控制流语句。

用于覆盖 state_control, error_path, parameter_check 类证据。
策略：基于正则启发式提取，无需 AST 解析器。

改进点：
1. 支持跨多行条件匹配（括号深度计数）
2. 增加语义子类型（parameter_check, resource_check, state_validation, error_guard）
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
    condition: str = ""  # 条件表达式（如 "ctx == nullptr"），截断版
    is_error_path: bool = False  # 是否为错误/异常返回路径
    semantic_type: str = ""  # 语义子类型
    multi_line: bool = False  # 条件是否跨多行
    full_condition: str = ""  # 完整条件（不截断）


# 控制流正则模式
# 支持 `} else if (...)` 和 `} catch (...)` 这种前面带 `}` 的情况
# 注意：这里只匹配语句开头，条件部分可能跨多行，由后续逻辑处理
_CONTROL_PATTERNS = [
    # if (condition)
    (re.compile(r'^(\s*\}?\s*if\s*\()(.*)$'), 'if'),
    # else if (condition)
    (re.compile(r'^(\s*\}?\s*else\s+if\s*\()(.*)$'), 'else_if'),
    # else
    (re.compile(r'^\s*\}?\s*else\s*(?:\{|\b|$)'), 'else'),
    # switch (condition)
    (re.compile(r'^(\s*\}?\s*switch\s*\()(.*)$'), 'switch'),
    # case xxx:
    (re.compile(r'^\s*case\s+(.+?):'), 'case'),
    # default:
    (re.compile(r'^\s*default\s*:'), 'default'),
    # try
    (re.compile(r'^\s*\}?\s*try\s*(?:\{|\b|$)'), 'try'),
    # catch (type var)
    (re.compile(r'^(\s*\}?\s*catch\s*\()(.*)$'), 'catch'),
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

# 语义子类型推断规则
# parameter_check: 检查参数有效性（空指针、范围、边界）
_PARAMETER_CHECK_RE = re.compile(
    r'(?:'
    r'(?:==|!=)\s*(?:nullptr|NULL|0|nullptr_t)'
    r'|(?:<|>|<=|>=)\s*(?:0|1|-1|\w+_count|count|size|len|length)'
    r'|!\s*\w+'  # !param
    r'|\w+\s*\.\s*empty\s*\(\)'
    r'|\w+\s*\.\s*size\s*\(\)'
    r'|std::\w+\s*\(\s*\w+\s*\)'
    r'|(?:is_valid|is_empty|has_|can_|should_|needs_)\s*\('
    r')',
    re.IGNORECASE,
)

# resource_check: 检查资源句柄有效性（要求 -> 成员访问，更具体）
_RESOURCE_CHECK_RE = re.compile(
    r'(?:ctx|context|device|handle|ptr|pointer|fd|file|sock|socket|conn|connection|inst|instance|gpu|buf|buffer|res|resource)'
    r'\s*->\s*\w+\s*(?:==|!=)\s*(?:nullptr|NULL|0)',
    re.IGNORECASE,
)

# state_validation: 检查对象内部状态
_STATE_VALIDATION_RE = re.compile(
    r'(?:initialized|ready|valid|dirty|clean|open|closed|active|enabled|disabled|flag|status|state|mode)'
    r'|\bdata\s*\.\s*\w+\s*(?:==|!=|!|\b)',
    re.IGNORECASE,
)


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


def _count_parens(text: str) -> int:
    """计算未闭合的左括号数量（考虑字符串和注释）。"""
    depth = 0
    in_string = False
    string_char = None
    i = 0
    while i < len(text):
        ch = text[i]
        if in_string:
            if ch == '\\' and i + 1 < len(text):
                i += 2
                continue
            if ch == string_char:
                in_string = False
                string_char = None
        else:
            if ch in '"\'':
                in_string = True
                string_char = ch
            elif ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
        i += 1
    return depth


def _collect_multi_line_condition(
    start_idx: int,
    end_idx: int,
    file_lines: list[str],
    initial_condition: str,
) -> tuple[str, str, bool, int]:
    """
    收集跨多行的条件，直到括号平衡。

    Returns:
        (full_condition, truncated_condition, is_multi_line, consumed_lines)
    """
    full = initial_condition
    # 在 initial_condition 前面加一个虚拟 '(' 来计算括号深度
    # 因为 initial_condition 来自正则捕获组，不包含开头的 '('
    depth = _count_parens("(" + full)
    consumed = 0

    # 如果 depth <= 0，说明当前行已经完整
    if depth <= 0:
        # 去掉尾部 ){ 或 )
        full = re.sub(r'\)\s*\{?\s*$', '', full).strip()
        truncated = full if len(full) <= 120 else full[:120] + "..."
        return full, truncated, False, 0

    # 跨多行：继续读取后续行
    idx = start_idx + 1
    while idx < end_idx and depth > 0:
        line = file_lines[idx]
        # 去掉行内注释
        line_no_comment = re.sub(r'//.*$', '', line)
        full += " " + line_no_comment.strip()
        depth = _count_parens(full)
        consumed += 1
        idx += 1

    # 去掉尾部 ){ 或 )
    full = re.sub(r'\)\s*\{?\s*$', '', full).strip()
    is_multi_line = consumed > 0
    truncated = full if len(full) <= 120 else full[:120] + "..."
    return full, truncated, is_multi_line, consumed


def _infer_semantic_type(
    cf_type: str,
    condition: str,
    file_lines: list[str],
    line_idx: int,
    end_idx: int,
) -> str:
    """
    根据条件内容和上下文推断语义子类型。

    优先级：error_guard > resource_check > parameter_check > state_validation
    resource_check 优先于 parameter_check，因为更具体。
    """
    if cf_type not in ('if', 'else_if', 'switch', 'catch'):
        return ""

    cond_lower = condition.lower()

    # error_guard: 检查后面几行是否有错误返回
    if cf_type in ('if', 'else_if'):
        for j in range(line_idx + 1, min(line_idx + 5, end_idx)):
            line_text = file_lines[j]
            if _ERROR_RETURN_RE.search(line_text):
                # 进一步判断是 resource_check 还是 parameter_check 还是 generic error_guard
                if _RESOURCE_CHECK_RE.search(condition):
                    return "resource_check"
                if _PARAMETER_CHECK_RE.search(condition):
                    return "parameter_check"
                return "error_guard"

    if _RESOURCE_CHECK_RE.search(condition):
        return "resource_check"
    if _PARAMETER_CHECK_RE.search(condition):
        return "parameter_check"
    if _STATE_VALIDATION_RE.search(condition):
        return "state_validation"

    return ""


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

    line_idx = start_idx
    while line_idx < end_idx:
        line = file_lines[line_idx]
        line_no = line_idx + 1

        # 跳过空行和纯注释行
        stripped = line.strip()
        if not stripped or stripped.startswith('//') or stripped.startswith('*'):
            line_idx += 1
            continue

        matched = False
        skip_extra = 0  # 多行条件消耗的额外行数

        # 1. 匹配 if/else/switch/try/catch
        for pattern_re, cf_type in _CONTROL_PATTERNS:
            m = pattern_re.match(line)
            if m:
                # 有捕获组的模式（if, else_if, switch, catch）需要处理条件
                if m.lastindex and m.lastindex >= 2:
                    initial_condition = m.group(2).strip()
                    full_cond, truncated_cond, is_multi_line, consumed = _collect_multi_line_condition(
                        line_idx, end_idx, file_lines, initial_condition,
                    )
                    semantic = _infer_semantic_type(
                        cf_type, full_cond, file_lines, line_idx, end_idx,
                    )
                    blocks.append(ControlFlowBlock(
                        id=f"{file_path}:{start_line}:{line_no}:{cf_type}",
                        function_id=function_id,
                        file_path=file_path,
                        line=line_no,
                        type=cf_type,
                        condition=truncated_cond,
                        semantic_type=semantic,
                        multi_line=is_multi_line,
                        full_condition=full_cond,
                    ))
                    skip_extra = consumed
                    matched = True
                    break
                else:
                    # 无条件的模式（else, case, default, try）
                    condition = ""
                    if cf_type == 'case' and m.lastindex:
                        condition = m.group(1).strip()
                    semantic = _infer_semantic_type(
                        cf_type, condition, file_lines, line_idx, end_idx,
                    ) if condition else ""
                    blocks.append(ControlFlowBlock(
                        id=f"{file_path}:{start_line}:{line_no}:{cf_type}",
                        function_id=function_id,
                        file_path=file_path,
                        line=line_no,
                        type=cf_type,
                        condition=condition,
                        semantic_type=semantic,
                    ))
                    matched = True
                    break

        # 2. 匹配错误返回路径（行内任何位置，包括 if 语句内部）
        # 注意：即使 matched=True（如 if (!p) return nullptr;），也要检测错误返回
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
            line_idx += 1 + skip_extra
            continue

        if matched:
            line_idx += 1 + skip_extra
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

        line_idx += 1

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
