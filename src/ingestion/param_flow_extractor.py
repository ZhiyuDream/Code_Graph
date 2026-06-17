"""
参数流提取器：从 C++ 函数体中提取参数的使用模式。

用于覆盖 config_param_flow, param_config 类证据。
策略：基于正则启发式提取，无需 AST 解析器。
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# 参数使用模式
# 1. 参数读取（成员访问、数组索引、取地址）
_PARAM_READ_RE = re.compile(
    r'\b(\w+)\s*\.\s*(\w+)',  # param.field
)
_PARAM_ARRAY_RE = re.compile(
    r'\b(\w+)\s*\[',  # param[
)
_PARAM_REF_RE = re.compile(
    r'&\s*(\w+)\b',  # &param
)

# 2. 参数赋值到局部变量
_PARAM_ASSIGN_RE = re.compile(
    r'(?:const\s+)?\w+\s*[*&]?\s+(\w+)\s*=\s*(\w+)',  # Type local = param
)

# 3. 参数传递（下游调用）
_PARAM_PASS_RE = re.compile(
    r'\b(\w+)\s*\([^)]*\b(\w+)\b',  # callee(..., param, ...)
)

# 4. 参数返回
_PARAM_RETURN_RE = re.compile(
    r'return\s+(\w+)',  # return param
)

# 5. 配置级联（结构体字段赋值）
_CONFIG_CASCADE_RE = re.compile(
    r'\b(\w+)\s*\.\s*(\w+)\s*=\s*([^;]+)',  # param.field = value
)


def extract_param_flow_for_function(
    function_name: str,
    file_path: str,
    start_line: int,
    end_line: int,
    file_lines: list[str],
    param_names: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    从单个函数体中提取参数流信息。

    Args:
        function_name: 函数名
        file_path: 相对路径
        start_line: 函数开始行（1-based，含）
        end_line: 函数结束行（1-based，含）
        file_lines: 整个文件的所有行（0-based 索引）
        param_names: 已知参数名列表（如果为 None，则通过正则启发式推断）

    Returns:
        param_usage 列表，每项为 {"param": str, "operations": list[str], "lines": list[int]}
    """
    start_idx = max(0, start_line - 1)
    end_idx = min(len(file_lines), end_line)

    # 如果未提供参数名，尝试从函数签名推断
    if param_names is None and start_idx < len(file_lines):
        sig_line = file_lines[start_idx]
        param_names = _infer_param_names(sig_line)

    if not param_names:
        return []

    param_names_set = set(param_names)
    usage: dict[str, dict[str, Any]] = {}

    for param in param_names:
        usage[param] = {"param": param, "operations": set(), "lines": set()}

    # 从 start_idx + 1 开始，跳过函数签名行（避免将签名中的参数误匹配为调用）
    for line_idx in range(start_idx + 1, end_idx):
        line = file_lines[line_idx]
        line_no = line_idx + 1

        # 跳过空行和注释行
        stripped = line.strip()
        if not stripped or stripped.startswith('//') or stripped.startswith('*'):
            continue

        # 去掉行内注释
        code_only = re.sub(r'//.*$', '', line)

        # 1. 参数成员访问 / 数组索引 / 取地址
        for m in _PARAM_READ_RE.finditer(code_only):
            param = m.group(1)
            field = m.group(2)
            if param in param_names_set:
                usage[param]["operations"].add(f"field_read:{field}")
                usage[param]["lines"].add(line_no)

        for m in _PARAM_ARRAY_RE.finditer(code_only):
            param = m.group(1)
            if param in param_names_set:
                usage[param]["operations"].add("array_index")
                usage[param]["lines"].add(line_no)

        for m in _PARAM_REF_RE.finditer(code_only):
            param = m.group(1)
            if param in param_names_set:
                usage[param]["operations"].add("ref_pass")
                usage[param]["lines"].add(line_no)

        # 2. 参数赋值到局部变量
        for m in _PARAM_ASSIGN_RE.finditer(code_only):
            local_var = m.group(1)
            src = m.group(2)
            if src in param_names_set:
                usage[src]["operations"].add(f"assign_to:{local_var}")
                usage[src]["lines"].add(line_no)

        # 3. 参数传递（下游调用）
        for m in _PARAM_PASS_RE.finditer(code_only):
            callee = m.group(1)
            arg = m.group(2)
            if arg in param_names_set and callee != arg:
                usage[arg]["operations"].add(f"pass_to:{callee}")
                usage[arg]["lines"].add(line_no)

        # 4. 参数返回
        for m in _PARAM_RETURN_RE.finditer(code_only):
            param = m.group(1)
            if param in param_names_set:
                usage[param]["operations"].add("return")
                usage[param]["lines"].add(line_no)

        # 5. 配置级联（结构体字段赋值）
        for m in _CONFIG_CASCADE_RE.finditer(code_only):
            param = m.group(1)
            field = m.group(2)
            value = m.group(3).strip()
            if param in param_names_set:
                usage[param]["operations"].add(f"field_assign:{field}={value[:30]}")
                usage[param]["lines"].add(line_no)

    # 转换为列表格式
    result = []
    for param in sorted(param_names):
        info = usage[param]
        if info["operations"]:
            result.append({
                "param": param,
                "operations": sorted(info["operations"]),
                "lines": sorted(info["lines"]),
            })

    return result


def _infer_param_names(sig_line: str) -> list[str]:
    """从函数签名行中推断参数名。"""
    # 匹配: Type param, Type param) 或 Type param)
    # 先找到参数列表（最外层括号）
    if '(' not in sig_line or ')' not in sig_line:
        return []

    # 提取最外层括号内的内容
    depth = 0
    start = None
    for i, ch in enumerate(sig_line):
        if ch == '(':
            if depth == 0:
                start = i
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0 and start is not None:
                params_str = sig_line[start + 1 : i]
                break
    else:
        return []

    if not params_str or params_str == "void":
        return []

    # 分割参数，处理嵌套模板
    params = []
    current = ""
    angle_depth = 0
    paren_depth = 0
    for ch in params_str:
        if ch == '<':
            angle_depth += 1
        elif ch == '>':
            angle_depth -= 1
        elif ch == '(':
            paren_depth += 1
        elif ch == ')':
            paren_depth -= 1
        elif ch == ',' and angle_depth == 0 and paren_depth == 0:
            params.append(current.strip())
            current = ""
            continue
        current += ch
    params.append(current.strip())

    # 从每个参数中提取参数名
    param_names = []
    for p in params:
        p = p.strip()
        if not p:
            continue
        # 去掉默认值
        p = re.sub(r'=.*$', '', p).strip()
        # 去掉 const, & 等修饰
        # 参数格式: const std::string & param
        # 或: int param
        # 或: void (*)(int) callback
        tokens = p.split()
        if not tokens:
            continue
        # 最后一个非修饰符 token 通常是参数名
        # 但需要排除 * 和 &
        for t in reversed(tokens):
            t = t.strip('*&(),')
            if t and t not in ('const', 'volatile', 'restrict', 'static', 'inline'):
                if re.match(r'^[A-Za-z_]\w*$', t):
                    param_names.append(t)
                    break

    return param_names


def extract_all_param_flow(
    file_results: list[Any],
    repo_root: str = "",
) -> dict[str, list[dict[str, Any]]]:
    """
    从所有文件结果中提取参数流信息。

    Returns:
        {func_id: param_usage_list}
    """
    from pathlib import Path

    all_flows: dict[str, list[dict[str, Any]]] = {}

    for fr in file_results:
        if not fr.functions:
            continue

        abs_path = Path(repo_root) / fr.file_path if repo_root else Path(fr.file_path)
        try:
            lines = abs_path.read_text(encoding='utf-8', errors='replace').splitlines()
        except Exception:
            continue

        for func in fr.functions:
            flow = extract_param_flow_for_function(
                function_name=func.name,
                file_path=func.file_path,
                start_line=func.start_line,
                end_line=func.end_line,
                file_lines=lines,
            )
            if flow:
                func_id = func.id or f"{func.file_path}:{func.name}:{func.start_line}"
                all_flows[func_id] = flow

    logger.info("Param flow extraction: %d functions with param usage",
                len(all_flows))
    return all_flows
