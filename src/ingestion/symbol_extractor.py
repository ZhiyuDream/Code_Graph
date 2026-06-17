"""
从 clangd LSP 的 documentSymbol + callHierarchy 中提取结构化符号。

核心原则：直接信任 clangd 20+ 的结果，不额外做正则提取或消歧。
- clangd 的 documentSymbol 返回精确的函数/类/变量边界
- clangd 的 callHierarchy 返回精确的跨文件调用关系
- 不需要正则提取"宏调用"来补充（clangd 已经覆盖）
- 不需要 fallback 到正则提取（如果 clangd 失败，说明配置有问题，应修复配置而非用正则凑合）
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from .lsp_client import LSPTimeoutError
from .models import ClassSymbol, FileResult, FunctionSymbol, RawCall, VariableSymbol

logger = logging.getLogger(__name__)

# LSP SymbolKind（数字）
KIND_FUNCTION = {6, 9, 10, 12}
KIND_CLASS = {5, 23}
KIND_VARIABLE = {7, 8, 13, 14}

KIND_FUNCTION_STR = {"function", "method", "constructor", "destructor"}
KIND_CLASS_STR = {"class", "struct"}
KIND_VARIABLE_STR = {"variable", "field", "property", "parameter"}

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


def _count_params_from_detail(detail: str) -> int | None:
    """从 clangd detail（函数签名）中提取参数个数。

    支持的格式：
      - "int (int, int)" → 2
      - "void ()" → 0
      - "void foo(const std::string&)" → 1
      - "auto (auto, auto)" → 2
    Returns None 当无法解析时。
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
                comma_depth = 0
                count = 1
                for c in params:
                    if c in "(<":
                        comma_depth += 1
                    elif c in ")>":
                        comma_depth -= 1
                    elif c == "," and comma_depth == 0:
                        count += 1
                return count
    return None


def _kind_to_str(kind: Any) -> str:
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

    if scope_class is not None:
        return "member"
    if scope_func is not None:
        if kind_name in ("parameter", "field") or (isinstance(kind_val, int) and kind_val == 8):
            return "param"
        return "local"

    if kind_name == "parameter":
        return "param"
    if kind_name in ("field", "property"):
        return "member"

    return "global"


def _walk_document_symbol_tree(
    symbols: list[dict],
    file_path: str,
) -> tuple[list[FunctionSymbol], list[ClassSymbol], list[VariableSymbol]]:
    """递归遍历层级的 DocumentSymbol tree。"""
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
                sig = s.get("detail", "") or ""
                functions.append(FunctionSymbol(
                    id=func_id,
                    name=name,
                    signature=sig,
                    file_path=file_path,
                    start_line=start_line,
                    end_line=end_line,
                    start_character=start_char,
                    is_definition=True,  # 信任 clangd：有 range 的符号就是定义
                    param_count=_count_params_from_detail(sig),
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
    file_classes: dict[str, list[tuple[int, int, int]]] = {}
    for i, c in enumerate(classes):
        file_classes.setdefault(c.file_path, []).append((c.start_line, c.end_line, i))

    resolved = []
    for v in variables:
        if v.kind == "member" and v.scope_class_index is None:
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

    直接信任 clangd 返回的 callee 位置信息，不做额外消歧。
    """
    if skip_outgoing:
        return []

    line0 = max(0, func.start_line - 1)
    char0 = func.start_character

    # 如果 start_character 指向行首（返回类型），尝试定位到函数名。
    # 对于 clangd 20+，selectionRange 通常已指向函数名，char0 很少为 0。
    # 但做一层轻量保护：只在单行范围内搜索函数名，避免读取整个文件。
    if char0 == 0 and func.name and repo_root:
        try:
            full_path = repo_root / file_path
            with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                for _ in range(line0):
                    f.readline()
                source_line = f.readline()
            name_pos = source_line.find(func.name)
            if name_pos >= 0:
                char0 = name_pos
        except Exception:
            pass

    items = lsp_request(
        "textDocument/prepareCallHierarchy",
        {"textDocument": {"uri": file_uri}, "position": {"line": line0, "character": char0}},
        timeout=60.0,
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
            timeout=60.0,
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
            sel = to_item.get("selectionRange") or to_item.get("range", {})
            callee_line = sel.get("start", {}).get("line", 0) + 1
            callee_detail = to_item.get("detail", "") or None
            from_ranges = out.get("fromRanges", [])
            calls.append(RawCall(
                caller_index=func_index,
                callee_name=callee_name,
                file_path=file_path,
                line=func.start_line,
                callee_file_path=callee_path or None,
                callee_line=callee_line if callee_path else None,
                callee_detail=callee_detail,
                from_ranges=from_ranges,
            ))

    if sleep_after > 0:
        time.sleep(sleep_after)

    return calls


def extract_incoming_calls_for_function(
    lsp_request: callable,
    file_uri: str,
    func: FunctionSymbol,
    repo_root: Path | None = None,
    sleep_after: float = 0.02,
) -> list[RawCall]:
    """
    对单个函数请求 callHierarchy/incomingCalls（反向查找 caller）。
    能捕获 lambda 内部的调用关系（outgoingCalls 不能）。
    """
    line0 = max(0, func.start_line - 1)
    char0 = func.start_character

    if char0 == 0 and func.name and repo_root:
        try:
            full_path = repo_root / func.file_path
            with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                for _ in range(line0):
                    f.readline()
                source_line = f.readline()
            name_pos = source_line.find(func.name)
            if name_pos >= 0:
                char0 = name_pos
        except Exception:
            pass

    try:
        items = lsp_request(
            "textDocument/prepareCallHierarchy",
            {"textDocument": {"uri": file_uri}, "position": {"line": line0, "character": char0}},
            timeout=60.0,
        )
    except Exception:
        return []

    if items is None:
        items = []
    elif not isinstance(items, list):
        items = [items]

    calls: list[RawCall] = []
    import os

    for item in items:
        try:
            incoming = lsp_request(
                "callHierarchy/incomingCalls",
                {"item": item},
                timeout=60.0,
            )
        except Exception:
            continue

        if incoming is None:
            incoming = []
        elif not isinstance(incoming, list):
            incoming = [incoming]

        for inc in incoming:
            from_item = inc.get("from") or {}
            caller_name = from_item.get("name", "")
            if not caller_name:
                continue
            caller_uri = from_item.get("uri", "")
            caller_path = _uri_to_path(caller_uri) if caller_uri else ""
            if repo_root and caller_path and caller_path.startswith(str(repo_root)):
                caller_path = os.path.relpath(caller_path, repo_root)
            r = from_item.get("range", {})
            caller_line = r.get("start", {}).get("line", 0) + 1
            calls.append(RawCall(
                caller_index=-1,
                callee_name=func.name,
                file_path=caller_path or func.file_path,
                line=caller_line,
                callee_file_path=func.file_path,
                callee_line=func.start_line,
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
    """
    refs = lsp_request(
        "textDocument/references",
        {
            "textDocument": {"uri": file_uri},
            "position": {"line": max(0, var.start_line - 1), "character": var.start_character},
            "context": {"includeDeclaration": True},
        },
        timeout=60.0,
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

        if ref_fp == var.file_path and ref_line == var.start_line:
            continue

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


def process_file(
    lsp_request: callable,
    abs_path: str,
    file_path: str,
    repo_root: Path | None = None,
    collect_calls: bool = True,
    collect_var_refs: bool = True,
    delay_between_calls: float = 0.02,
    lsp_notify: callable | None = None,
    skip_vendor_calls: bool = True,
) -> FileResult:
    """
    处理单个文件：获取 documentSymbol、callHierarchy、references。

    核心原则：直接信任 clangd，不额外做正则提取或 fallback。
    如果 clangd 失败，记录原因并返回空结果（不 fallback 到正则）。
    """
    file_uri = _path_to_uri(abs_path)

    is_vendor = file_path.startswith("vendor/") or "/vendor/" in file_path

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

    is_header = file_path.endswith((".h", ".hpp", ".hh", ".hxx"))

    try:
        symbols = lsp_request(
            "textDocument/documentSymbol",
            {"textDocument": {"uri": file_uri}},
            timeout=60.0,
        )
        if symbols is None:
            symbols = []
        elif not isinstance(symbols, list):
            symbols = []

        functions, classes, variables = extract_symbols_from_document(symbols, file_path)

        calls: list[RawCall] = []
        should_skip_calls = is_vendor and skip_vendor_calls
        if collect_calls and not should_skip_calls:
            for idx, fn in enumerate(functions):
                raw = extract_calls_for_function(
                    lsp_request, file_uri, idx, fn, file_path,
                    repo_root=repo_root,
                    sleep_after=delay_between_calls,
                    skip_outgoing=should_skip_calls,
                )
                calls.extend(raw)

        var_refs_global: list[tuple[str, str, int]] = []
        if collect_var_refs and variables:
            for v in variables:
                refs = extract_references_for_variable(
                    lsp_request, file_uri, v, functions,
                    repo_root=repo_root,
                    sleep_after=delay_between_calls,
                )
                var_refs_global.extend(refs)

    except LSPTimeoutError:
        logger.warning("LSP timeout for %s, returning empty result", file_path)
        functions, classes, variables = [], [], []
        calls = []
        var_refs_global = []

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
        var_refs=[],
        raw={"var_refs_global": var_refs_global},
    )
