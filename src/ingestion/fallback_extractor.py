"""
LSP 超时时退回到正则提取符号。

用于处理 clangd 无法在规定时间内完成解析的复杂文件（如大型头文件、
大量模板实例化的文件）。用正则提取函数/类/变量声明，保证文件不丢失。

精度低于 LSP，但能保证文件出现在图中。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from .models import ClassSymbol, FileResult, FunctionSymbol, RawCall, VariableSymbol

logger = logging.getLogger(__name__)


# 函数定义/声明匹配
# 匹配模式: return_type func_name(args) [const] [override] [final] [noexcept]
# 改进：支持一层嵌套括号（如 void (*)(int)）
_FUNC_RE = re.compile(
    r'^\s*(?:inline\s+|static\s+|virtual\s+|explicit\s+)*'
    r'(?:[\w:<>,\s&*]+?)\s+'          # 返回类型（非贪婪）
    r'(\w+)\s*'                         # 函数名
    r'\((?:[^()]*|\([^()]*\))*\)\s*'   # 参数列表，支持一层嵌套
    r'(?:const\s*|override\s*|final\s*|noexcept\s*)*'
    r'(?:\{|;|\s*=\s*0\s*;?)',          # { 定义, ; 声明, = 0 纯虚函数
    re.MULTILINE,
)

# 类/结构体定义匹配
_CLASS_RE = re.compile(
    r'^\s*(?:class|struct)\s+(?:[A-Z_\w]+\s+)?'  # class/struct [macro]
    r'(\w+)'                                       # 类名
    r'(?:\s*:\s*[\w\s,]+)?'                        # 继承列表
    r'\s*\{',                                       # {
    re.MULTILINE,
)

# 变量声明匹配（简单的全局/成员变量）
# 匹配: type var_name [= ...];
_VAR_RE = re.compile(
    r'^\s*(?:static\s+|const\s+|constexpr\s+)*'
    r'(?:[\w:<>,\s&*]+?)\s+'          # 类型
    r'(\w+)\s*'                         # 变量名
    r'(?:\[[^\]]*\])?'                  # 可选数组维度
    r'(?:\s*=\s*[^;]+)?'                # 可选初始化
    r'\s*;',
    re.MULTILINE,
)

# 宏调用匹配（函数式宏）
# 改进：匹配所有标识符调用，不限于大写开头（LLM 生成代码中常有非大写宏）
# 排除 C++ 关键字和已知函数名的过滤在调用处进行
_MACRO_CALL_RE = re.compile(
    r'\b([A-Za-z_]\w*)\s*\(',
)

# C/C++ 关键字，不应视为宏
_CPP_KEYWORDS = {
    "if", "else", "for", "while", "do", "switch", "case", "default",
    "return", "break", "continue", "goto", "sizeof", "typeof", "decltype",
    "static_cast", "dynamic_cast", "reinterpret_cast", "const_cast",
    "alignof", "offsetof", "static_assert", "ASSERT",
    "new", "delete", "try", "catch", "throw",
}


def _is_likely_function(line: str, name: str) -> bool:
    """启发式判断是否为函数定义/声明。"""
    stripped = line.strip()
    # 排除以 // 开头的注释行
    if stripped.startswith("//"):
        return False
    # 排除以 * 开头的注释行
    if stripped.startswith("*"):
        return False
    # 排除 using 声明
    if stripped.startswith("using "):
        return False
    # 排除 typedef
    if stripped.startswith("typedef "):
        return False
    # 排除 #include
    if stripped.startswith("#include"):
        return False
    # 排除 #define
    if stripped.startswith("#define"):
        return False
    # 排除 namespace
    if "namespace" in stripped:
        return False
    # 排除 template 声明行（不含函数名后的括号）
    if stripped.startswith("template"):
        return False
    return True


def extract_symbols_fallback(
    content: str,
    file_path: str,
) -> tuple[list[FunctionSymbol], list[ClassSymbol], list[VariableSymbol]]:
    """
    用正则从源码中提取符号（LSP 超时后的 fallback）。

    Returns:
        (functions, classes, variables)
    """
    functions: list[FunctionSymbol] = []
    classes: list[ClassSymbol] = []
    variables: list[VariableSymbol] = []

    lines = content.splitlines()

    # 1. 提取函数
    for line_idx, line in enumerate(lines, start=1):
        for m in _FUNC_RE.finditer(line):
            name = m.group(1)
            if not name or name in _CPP_KEYWORDS:
                continue
            if not _is_likely_function(line, name):
                continue
            # 检查是否已在列表中（去重）
            if any(f.name == name and f.start_line == line_idx for f in functions):
                continue
            func_id = f"{file_path}:{name}:{line_idx}"
            functions.append(FunctionSymbol(
                id=func_id,
                name=name,
                signature="",
                file_path=file_path,
                start_line=line_idx,
                end_line=line_idx + 1,
                start_character=line.find(name),
                is_definition="{" in line,
                param_count=None,
            ))

    # 2. 提取类
    for line_idx, line in enumerate(lines, start=1):
        for m in _CLASS_RE.finditer(line):
            name = m.group(1)
            if not name:
                continue
            if any(c.name == name and c.start_line == line_idx for c in classes):
                continue
            classes.append(ClassSymbol(
                name=name,
                file_path=file_path,
                start_line=line_idx,
                end_line=line_idx + 1,
            ))

    # 3. 提取变量（简单的全局变量）
    for line_idx, line in enumerate(lines, start=1):
        for m in _VAR_RE.finditer(line):
            name = m.group(1)
            if not name or name in _CPP_KEYWORDS:
                continue
            if any(v.name == name and v.start_line == line_idx for v in variables):
                continue
            var_id = f"{file_path}:{line_idx}:0:{name}"
            variables.append(VariableSymbol(
                id=var_id,
                name=name,
                file_path=file_path,
                start_line=line_idx,
                kind="global",
            ))

    logger.info(
        "Fallback extraction for %s: %d functions, %d classes, %d variables",
        file_path, len(functions), len(classes), len(variables),
    )
    return functions, classes, variables


def process_file_fallback(
    content: str,
    file_path: str,
    repo_root: str | None = None,
    extract_macros: bool = True,
) -> FileResult:
    """
    LSP 超时时用正则提取符号（不丢失文件）。

    Args:
        content: 文件内容
        file_path: 相对路径
        repo_root: 仓库根目录
        extract_macros: 是否提取宏调用

    Returns:
        FileResult
    """
    functions, classes, variables = extract_symbols_fallback(content, file_path)

    # 宏调用提取（和 LSP 版本一致）
    calls: list[RawCall] = []
    if extract_macros and functions:
        file_lines = content.splitlines()
        known_names = {f.name for f in functions}
        for idx, func in enumerate(functions):
            start = max(0, func.start_line - 1)
            end = min(len(file_lines), func.end_line)
            for line_idx in range(start, end):
                line = file_lines[line_idx]
                stripped = line.strip()
                if not stripped or stripped.startswith("//") or stripped.startswith("*"):
                    continue
                for match in _MACRO_CALL_RE.finditer(line):
                    name = match.group(1)
                    if name in _CPP_KEYWORDS or name in known_names:
                        continue
                    calls.append(RawCall(
                        caller_index=idx,
                        callee_name=name,
                        file_path=file_path,
                        line=line_idx + 1,
                        callee_file_path=None,
                        callee_line=None,
                    ))

    return FileResult(
        file_path=file_path,
        functions=functions,
        classes=classes,
        variables=variables,
        calls=calls,
    )
