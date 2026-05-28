"""
从 clangd LSP 的 documentSymbol + callHierarchy 中提取结构化符号。

修复点（相比原 clangd_parser.py）：
1. kind=8（Field）不再被错误归类为 "param"
2. 异常向上传播，不再静默吞掉
3. callee_line 被保留在 RawCall 中
4. 支持扁平 documentSymbol：Field 符号向前查找最近的 Class/Struct
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any

from .models import ClassSymbol, FileResult, FunctionSymbol, RawCall, VariableSymbol

logger = logging.getLogger(__name__)

# LSP SymbolKind（数字）
# Method=6, Constructor=9, Destructor=10, Function=12,
# Class=5, Struct=23, Variable=13, Field=7, Property=14, Parameter=8
KIND_FUNCTION = {6, 9, 10, 12}
KIND_CLASS = {5, 23}
KIND_VARIABLE = {7, 8, 13, 14}

KIND_FUNCTION_STR = {"function", "method", "constructor", "destructor"}
KIND_CLASS_STR = {"class", "struct"}
KIND_VARIABLE_STR = {"variable", "field", "property", "parameter"}

# SymbolKind 规范映射（数字 -> 标准名称）
# 注意：clangd 和 LSP 规范略有差异，clangd 有时用 8 表示 Field
_SYMBOL_KIND_NAMES: dict[int, str] = {
    1: "file", 2: "module", 3: "namespace", 4: "package",
    5: "class", 6: "method", 7: "property", 8: "field",
    9: "constructor", 10: "enum", 11: "interface", 12: "function",
    13: "variable", 14: "constant", 15: "string", 16: "number",
    17: "boolean", 18: "array", 19: "object", 20: "key",
    21: "null", 22: "enum_member", 23: "struct", 24: "event",
    25: "operator", 26: "type_parameter",
}


def _is_function(kind: Any) -> bool:
    if isinstance(kind, int):
        return kind in KIND_FUNCTION
    if isinstance(kind, str):
        return kind.lower() in KIND_FUNCTION_STR
    return False


def _is_class(kind: Any) -> bool:
    if isinstance(kind, int):
        return kind in KIND_CLASS
    if isinstance(kind, str):
        return kind.lower() in KIND_CLASS_STR
    return False


def _is_variable(kind: Any) -> bool:
    if isinstance(kind, int):
        return kind in KIND_VARIABLE
    if isinstance(kind, str):
        return kind.lower() in KIND_VARIABLE_STR
    return False


def _range_to_lines(r: dict) -> tuple[int, int]:
    start = r.get("start", {})
    end = r.get("end", {})
    return start.get("line", 0) + 1, end.get("line", 0) + 1


def _kind_to_str(kind: Any) -> str:
    """将 SymbolKind 转为标准名称。"""
    if isinstance(kind, int):
        return _SYMBOL_KIND_NAMES.get(kind, f"unknown({kind})")
    if isinstance(kind, str):
        return kind.lower()
    return "unknown"


def _determine_variable_kind(
    kind_val: Any,
    scope_func: int | None,
    scope_class: int | None,
) -> str:
    """
    确定变量的 kind。

    修复原代码的 bug：kind=8（Field）在 clangd 中
    - 在 class/struct 内部表示字段/成员
    - 在 function 内部（某些 clangd 版本）表示参数
    不应一概归类为 "param"。
    """
    kind_name = _kind_to_str(kind_val)

    # 优先按作用域判断
    if scope_class is not None:
        # 在 class/struct 内部：member
        return "member"
    if scope_func is not None:
        # 在 function 内部：param 或 local
        # SymbolKind 8 在 function 内部某些 clangd 版本表示 parameter
        if kind_name in ("parameter", "field") or (isinstance(kind_val, int) and kind_val == 8):
            return "param"
        return "local"

    # 无作用域信息时，按 kind_name fallback
    if kind_name == "parameter":
        return "param"
    if kind_name in ("field", "property"):
        return "member"

    return "global"


def _walk_document_symbol_tree(
    symbols: list[dict],
    file_path: str,
) -> tuple[list[FunctionSymbol], list[ClassSymbol], list[VariableSymbol]]:
    """
    递归遍历层级的 DocumentSymbol tree。
    """
    functions: list[FunctionSymbol] = []
    classes: list[ClassSymbol] = []
    variables: list[VariableSymbol] = []

    def walk(nodes: list[dict], scope_func: int | None, scope_class: int | None) -> None:
        for s in nodes:
            kind = s.get("kind", 0)
            name = s.get("name", "")
            r = s.get("range") or (s.get("location") or {}).get("range", {})
            start_line, end_line = _range_to_lines(r) if r else (0, 0)
            sel = s.get("selectionRange") or r
            sel_start = (sel or {}).get("start", {})
            start_char = sel_start.get("character", 0)

            if _is_function(kind) and name:
                idx = len(functions)
                func_id = f"{file_path}:{name}:{start_line}"
                # 启发式判断：行数 >=3 认为是定义，否则是声明
                is_def = (end_line - start_line) >= 2
                functions.append(FunctionSymbol(
                    id=func_id,
                    name=name,
                    signature=s.get("detail", "") or "",
                    file_path=file_path,
                    start_line=start_line,
                    end_line=end_line,
                    start_character=start_char,
                    is_definition=is_def,
                ))
                for child in s.get("children", []):
                    walk([child], idx, scope_class)
            elif _is_class(kind) and name:
                cidx = len(classes)
                classes.append(ClassSymbol(
                    name=name,
                    file_path=file_path,
                    start_line=start_line,
                    end_line=end_line,
                ))
                for child in s.get("children", []):
                    walk([child], None, cidx)
            elif _is_variable(kind) and name:
                kind_str = _determine_variable_kind(kind, scope_func, scope_class)
                var_id = f"{file_path}:{start_line}:{start_char}:{name}"
                variables.append(VariableSymbol(
                    id=var_id,
                    name=name,
                    file_path=file_path,
                    start_line=start_line,
                    kind=kind_str,
                    scope_function_index=scope_func,
                    scope_class_index=scope_class,
                    start_character=start_char,
                ))
                for child in s.get("children", []):
                    walk([child], scope_func, scope_class)
            else:
                for child in s.get("children", []):
                    walk([child], scope_func, scope_class)

    walk(symbols, None, None)
    return functions, classes, variables


def _resolve_flat_fields(
    functions: list[FunctionSymbol],
    classes: list[ClassSymbol],
    variables: list[VariableSymbol],
) -> list[VariableSymbol]:
    """
    对扁平 documentSymbol（Field 与 Class 同级，无 children 关联）做启发式修复。

    策略：对每个 kind="member" 但 scope_class_index 为 None 的变量，
    向前查找最近的 Class/Struct（同一文件，行号在变量之前）。
    """
    # 按文件分组
    file_classes: dict[str, list[tuple[int, int, int]]] = {}
    for i, c in enumerate(classes):
        file_classes.setdefault(c.file_path, []).append((c.start_line, c.end_line, i))

    resolved = []
    for v in variables:
        if v.kind == "member" and v.scope_class_index is None:
            # 向前查找最近的 class
            candidates = file_classes.get(v.file_path, [])
            best_idx = None
            best_line = -1
            for start_line, end_line, class_idx in candidates:
                if start_line <= v.start_line <= end_line and start_line > best_line:
                    best_idx = class_idx
                    best_line = start_line
            if best_idx is not None:
                v = VariableSymbol(
                    id=v.id,
                    name=v.name,
                    file_path=v.file_path,
                    start_line=v.start_line,
                    kind=v.kind,
                    scope_function_index=v.scope_function_index,
                    scope_class_index=best_idx,
                    start_character=v.start_character,
                )
        resolved.append(v)
    return resolved


def extract_symbols_from_document(
    symbols: list[dict],
    file_path: str,
) -> tuple[list[FunctionSymbol], list[ClassSymbol], list[VariableSymbol]]:
    """
    从 documentSymbol 结果提取函数、类、变量。

    支持层级 tree 和扁平 list 两种模式。
    """
    functions, classes, variables = _walk_document_symbol_tree(symbols, file_path)

    # 检测是否为扁平模式：如果有 Field 变量但 scope_class_index 全为 None，
    # 且 classes 非空，说明可能需要启发式修复
    has_orphan_fields = any(
        v.kind == "member" and v.scope_class_index is None
        for v in variables
    )
    if has_orphan_fields and classes:
        variables = _resolve_flat_fields(functions, classes, variables)

    return functions, classes, variables


def extract_calls_for_function(
    lsp_request: callable,
    file_uri: str,
    func_index: int,
    func: FunctionSymbol,
    file_path: str,
    repo_root: Path | None = None,
    sleep_after: float = 0.02,
    skip_outgoing: bool = False,
) -> list[RawCall]:
    """
    对单个函数请求 callHierarchy/outgoingCalls。

    Args:
        lsp_request: 发送 LSP request 的函数，签名为 request(proc, method, params) -> result
        file_uri: 文件的 LSP URI
        func_index: 函数在 file_result.functions 中的索引
        func: 函数符号
        file_path: 相对路径
        sleep_after: 每次请求后休眠时间
        skip_outgoing: 如果为 True，直接返回空列表（用于 vendor 目录等场景）

    Returns:
        RawCall 列表
    """
    if skip_outgoing:
        return []

    line0 = max(0, func.start_line - 1)
    char0 = func.start_character

    # prepareCallHierarchy requires cursor on the function name, not the line start.
    # If start_character points to the return type (e.g. 0 for "std::string func(...)"),
    # find the actual function name position in the source line.
    if char0 == 0 and func.name:
        try:
            full_path = str(repo_root / file_path) if repo_root else file_path
            source_line = Path(full_path).read_text(encoding='utf-8', errors='ignore').split('\n')[line0]
            name_pos = source_line.find(func.name)
            if name_pos >= 0:
                char0 = name_pos
        except Exception:
            pass

    items = lsp_request(
        "textDocument/prepareCallHierarchy",
        {"textDocument": {"uri": file_uri}, "position": {"line": line0, "character": char0}},
    )
    if items is None:
        items = []
    elif not isinstance(items, list):
        items = [items]

    calls: list[RawCall] = []
    for item in items:
        outgoing = lsp_request(
            "callHierarchy/outgoingCalls",
            {"item": item},
        )
        if outgoing is None:
            outgoing = []
        elif not isinstance(outgoing, list):
            outgoing = [outgoing]

        for out in outgoing:
            to_item = out.get("to") or {}
            callee_name = to_item.get("name", "")
            if not callee_name:
                continue
            callee_uri = to_item.get("uri", "")
            callee_path = _uri_to_path(callee_uri) if callee_uri else ""
            if repo_root and callee_path and callee_path.startswith(str(repo_root)):
                import os
                callee_path = os.path.relpath(callee_path, repo_root)
            r = to_item.get("range", {})
            callee_line = r.get("start", {}).get("line", 0) + 1
            calls.append(RawCall(
                caller_index=func_index,
                callee_name=callee_name,
                file_path=file_path,
                line=func.start_line,
                callee_file_path=callee_path or None,
                callee_line=callee_line if callee_path else None,
            ))

    if sleep_after > 0:
        time.sleep(sleep_after)

    return calls


def extract_references_for_variable(
    lsp_request: callable,
    file_uri: str,
    var: VariableSymbol,
    all_functions: list[FunctionSymbol],
    repo_root: Path | None = None,
    sleep_after: float = 0.02,
) -> list[tuple[str, str, int]]:
    """
    对单个变量请求 textDocument/references，并将引用位置归属到函数。

    Args:
        lsp_request: 发送 LSP request 的函数
        file_uri: 文件的 LSP URI
        var: 变量符号
        all_functions: 全局函数列表（用于归属引用到函数）
        sleep_after: 每次请求后休眠时间

    Returns:
        (func_id, var_id, ref_line) 列表
    """
    refs = lsp_request(
        "textDocument/references",
        {
            "textDocument": {"uri": file_uri},
            "position": {"line": max(0, var.start_line - 1), "character": var.start_character},
            "context": {"includeDeclaration": True},
        },
    )
    if refs is None:
        refs = []
    elif not isinstance(refs, list):
        refs = [refs]

    var_refs: list[tuple[str, str, int]] = []
    for loc in refs:
        ref_uri = loc.get("uri", "")
        ref_fp = _uri_to_path(ref_uri)
        if repo_root and ref_fp and ref_fp.startswith(str(repo_root)):
            import os
            ref_fp = os.path.relpath(ref_fp, repo_root)
        r = loc.get("range", {})
        ref_line = r.get("start", {}).get("line", 0) + 1

        # 引用行等于变量声明行时跳过
        if ref_fp == var.file_path and ref_line == var.start_line:
            continue

        # 归属到包含该行的函数
        chosen = _find_function_containing_line(all_functions, ref_fp, ref_line)
        if chosen is not None:
            var_refs.append((chosen, var.id, ref_line))

    if sleep_after > 0:
        time.sleep(sleep_after)

    return var_refs


def _find_function_containing_line(
    all_functions: list[FunctionSymbol],
    file_path: str,
    line: int,
) -> str | None:
    """找到包含指定行的函数 ID。"""
    chosen = None
    for af in all_functions:
        if af.file_path != file_path:
            continue
        if af.start_line <= line <= af.end_line:
            chosen = af
            break
    if chosen is None:
        # Fallback: 取 start_line <= line 且 start_line 最大的函数
        candidates = [
            af for af in all_functions
            if af.file_path == file_path and af.start_line <= line
        ]
        if candidates:
            chosen = max(candidates, key=lambda x: x.start_line)
    return chosen.id if chosen else None


def _uri_to_path(uri: str) -> str:
    import os
    if uri.startswith("file://"):
        path = uri[7:]
        if path.startswith("//"):
            path = path[2:]
        return os.path.normpath(path)
    return uri


def _path_to_uri(path: str) -> str:
    return Path(path).resolve().as_uri()


# C/C++ 关键字/保留字，不应被视为宏调用
_CPP_KEYWORDS = {
    "if", "else", "for", "while", "do", "switch", "case", "default",
    "return", "break", "continue", "goto", "sizeof", "typeof", "decltype",
    "static_cast", "dynamic_cast", "reinterpret_cast", "const_cast",
    "alignof", "offsetof", "static_assert", "ASSERT",
    "new", "delete", "try", "catch", "throw",
}

# 匹配标识符后跟左括号的模式（潜在的函数调用或宏调用）
# 注意：这个正则会匹配所有函数调用，需要配合已知函数名做过滤
_MACRO_CALL_RE = re.compile(r'\b([A-Za-z_]\w*)\s*\(')


def _extract_macro_calls_for_function(
    func: FunctionSymbol,
    file_lines: list[str],
    known_func_names: set[str],
) -> list[RawCall]:
    """
    基于正则从函数体中提取潜在的宏调用。

    策略：
    1. 在函数行范围内搜索 identifier( 模式
    2. 排除 C/C++ 关键字
    3. 排除已知函数名（从 documentSymbol 提取的）
    4. 剩余作为宏调用候选

    Returns:
        RawCall 列表（caller_index 需要外部填入）
    """
    raw_calls: list[RawCall] = []
    start = max(0, func.start_line - 1)
    end = min(len(file_lines), func.end_line)

    for line_idx in range(start, end):
        line = file_lines[line_idx]
        # 跳过注释行和空行
        stripped = line.strip()
        if not stripped or stripped.startswith("//") or stripped.startswith("*"):
            continue

        for match in _MACRO_CALL_RE.finditer(line):
            name = match.group(1)
            # 排除关键字和已知函数
            if name in _CPP_KEYWORDS or name in known_func_names:
                continue
            # 排除看起来像类型转换的（如 (type)var）——这里简化处理
            if name in {"char", "int", "float", "double", "void", "bool",
                        "short", "long", "unsigned", "signed", "const", "struct", "class",
                        "enum", "union", "template", "typename", "namespace", "using",
                        "public", "private", "protected", "virtual", "override",
                        "static", "extern", "inline", "constexpr", "consteval"}:
                continue

            raw_calls.append(RawCall(
                caller_index=-1,  # 外部填入
                callee_name=name,
                file_path=func.file_path,
                line=line_idx + 1,
                callee_file_path=None,
                callee_line=None,
            ))

    return raw_calls


def process_file(
    lsp_request: callable,
    abs_path: str,
    file_path: str,
    repo_root: Path | None = None,
    collect_calls: bool = True,
    collect_var_refs: bool = True,
    delay_between_calls: float = 0.02,
    lsp_notify: callable | None = None,
    extract_macros: bool = True,
    skip_vendor_calls: bool = True,
) -> FileResult:
    """
    处理单个文件：获取 documentSymbol、callHierarchy、references。

    Args:
        lsp_request: 发送 LSP request 的函数
        abs_path: 绝对路径（用于 didOpen）
        file_path: 相对路径（用于输出）
        repo_root: 仓库根目录（用于路径归一化）
        collect_calls: 是否收集 outgoingCalls
        collect_var_refs: 是否收集变量引用
        delay_between_calls: 每次 LSP 请求后的延迟
        lsp_notify: 发送 LSP notify 的函数（用于 didOpen/didClose）
        extract_macros: 是否基于正则提取宏调用（补充 clangd 遗漏的宏）
        skip_vendor_calls: 是否跳过 vendor 目录函数的 outgoingCalls

    Returns:
        FileResult
    """
    file_uri = _path_to_uri(abs_path)

    # 判断是否为 vendor 文件
    is_vendor = file_path.startswith("vendor/") or "/vendor/" in file_path

    # 先 didOpen 让 clangd 知道文件
    content = ""
    if lsp_notify:
        try:
            content = Path(abs_path).read_text(encoding="utf-8", errors="replace")
        except Exception:
            content = ""
        lsp_notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": file_uri,
                    "languageId": "cpp",
                    "version": 1,
                    "text": content,
                }
            },
        )

    try:
        # 1. documentSymbol
        symbols = lsp_request(
            "textDocument/documentSymbol",
            {"textDocument": {"uri": file_uri}},
        )
        if symbols is None:
            symbols = []
        elif not isinstance(symbols, list):
            symbols = []

        functions, classes, variables = extract_symbols_from_document(symbols, file_path)

        # 2. outgoingCalls
        calls: list[RawCall] = []
        should_skip_calls = is_vendor and skip_vendor_calls
        if collect_calls:
            for idx, fn in enumerate(functions):
                raw = extract_calls_for_function(
                    lsp_request, file_uri, idx, fn, file_path,
                    repo_root=repo_root,
                    sleep_after=delay_between_calls,
                    skip_outgoing=should_skip_calls,
                )
                calls.extend(raw)

        # 3. 宏调用提取（补充 clangd 遗漏的）
        if extract_macros and not should_skip_calls and content:
            file_lines = content.splitlines()
            known_names = {f.name for f in functions}
            for idx, fn in enumerate(functions):
                macro_calls = _extract_macro_calls_for_function(
                    fn, file_lines, known_names
                )
                for mc in macro_calls:
                    mc.caller_index = idx
                calls.extend(macro_calls)

        # 4. var_refs（需要所有函数的列表做归属）
        var_refs_global: list[tuple[str, str, int]] = []
        if collect_var_refs and variables:
            for v in variables:
                refs = extract_references_for_variable(
                    lsp_request, file_uri, v, functions,
                    repo_root=repo_root,
                    sleep_after=delay_between_calls,
                )
                var_refs_global.extend(refs)
    finally:
        if lsp_notify:
            lsp_notify(
                "textDocument/didClose",
                {"textDocument": {"uri": file_uri}},
            )

    return FileResult(
        file_path=file_path,
        functions=functions,
        classes=classes,
        variables=variables,
        calls=calls,
        var_refs=[],  # 在 assembler 阶段统一处理
        raw={"var_refs_global": var_refs_global},
    )
