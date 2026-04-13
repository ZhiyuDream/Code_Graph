"""
通过 clangd LSP 获取 documentSymbol（函数、类）与 call hierarchy（outgoingCalls），
产出与 ast_parser.collect_all_tus() 相同结构的列表，供 graph_builder 使用。
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from src.clangd_client import start_clangd, initialize, request, notify

# 与 ast_parser 一致
SOURCE_EXTENSIONS = {".c", ".cpp", ".cc", ".cxx", ".h", ".hpp"}

# LSP SymbolKind（数字）：Method=6, Constructor=9, Function=12, Class=5, Struct=23, Variable=13, Field=7, etc.
KIND_FUNCTION = {6, 9, 10, 12}
KIND_CLASS = {5, 23}
KIND_FUNCTION_STR = {"function", "method", "constructor"}
KIND_CLASS_STR = {"class", "struct"}
# 变量类：Variable=13, Field=7/14（成员）, 部分实现用 8 表示 Parameter
KIND_VARIABLE = {7, 8, 13, 14}
KIND_VARIABLE_STR = {"variable", "field", "property", "parameter"}


def _uri_to_path(uri: str) -> str:
    if uri.startswith("file://"):
        path = uri[7:]
        if path.startswith("//"):
            path = path[2:]
        return os.path.normpath(path)
    return uri


def _path_to_uri(path: str) -> str:
    return Path(os.path.abspath(path)).as_uri()


def _get_source_files_from_compile_commands(compile_commands_dir: Path) -> list[str]:
    """从 compile_commands.json 中取出所有 C/C++ 源文件路径（绝对路径）。"""
    path = compile_commands_dir / "compile_commands.json"
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    seen: set[str] = set()
    out: list[str] = []
    for entry in data:
        fpath = entry.get("file", "")
        if not fpath or Path(fpath).suffix.lower() not in SOURCE_EXTENSIONS:
            continue
        if not os.path.isabs(fpath):
            cwd = entry.get("directory", "")
            fpath = os.path.normpath(os.path.join(cwd, fpath))
        if fpath not in seen:
            seen.add(fpath)
            out.append(fpath)
    return sorted(out)


def _get_header_files(repo_root: Path) -> list[str]:
    """遍历仓库，找出所有头文件（.h, .hpp）的绝对路径。"""
    if not repo_root or not repo_root.exists():
        return []
    
    header_exts = {".h", ".hpp"}
    headers: list[str] = []
    
    # 跳过的目录
    skip_dirs = {'.git', 'build', 'bin', 'obj', '.cache', 'node_modules', 'venv', '.venv', '__pycache__'}
    
    for root, dirs, files in os.walk(repo_root):
        # 跳过不需要的目录
        dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith('.')]
        
        for f in files:
            if Path(f).suffix.lower() in header_exts:
                full_path = os.path.join(root, f)
                headers.append(os.path.normpath(full_path))
    
    return sorted(headers)


def _range_to_lines(r: dict) -> tuple[int, int]:
    start = r.get("start", {})
    end = r.get("end", {})
    return start.get("line", 0) + 1, end.get("line", 0) + 1


def _collect_symbols_from_children(symbols: list[Any], file_path: str, repo_root: Path | None) -> tuple[list[dict], list[dict], list[dict]]:
    """从 documentSymbol 结果收集 functions、classes、variables（支持层级 DocumentSymbol）。"""
    functions: list[dict[str, Any]] = []
    classes: list[dict[str, Any]] = []
    variables: list[dict[str, Any]] = []

    def is_function(k: Any) -> bool:
        if isinstance(k, int):
            return k in KIND_FUNCTION
        if isinstance(k, str):
            return k.lower() in KIND_FUNCTION_STR
        return False

    def is_class(k: Any) -> bool:
        if isinstance(k, int):
            return k in KIND_CLASS
        if isinstance(k, str):
            return k.lower() in KIND_CLASS_STR
        return False

    def is_variable(k: Any) -> bool:
        if isinstance(k, int):
            return k in KIND_VARIABLE
        if isinstance(k, str):
            return k.lower() in KIND_VARIABLE_STR
        return False

    def walk(nodes: list[Any], scope_func: int | None, scope_class: int | None) -> None:
        for s in nodes:
            kind = s.get("kind", 0)
            name = s.get("name", "")
            r = s.get("range") or (s.get("location") or {}).get("range", {})
            start_line, end_line = _range_to_lines(r) if r else (0, 0)
            sel = s.get("selectionRange") or r
            sel_start = (sel or {}).get("start", {})
            start_char = sel_start.get("character", 0)
            if is_function(kind):
                idx = len(functions)
                functions.append({
                    "name": name,
                    "signature": s.get("detail", "") or "",
                    "file_path": file_path,
                    "start_line": start_line,
                    "end_line": end_line,
                    "start_character": start_char,
                })
                for child in s.get("children", []):
                    walk([child], idx, scope_class)
            elif is_class(kind):
                cidx = len(classes)
                classes.append({
                    "name": name,
                    "file_path": file_path,
                    "start_line": start_line,
                    "end_line": end_line,
                })
                for child in s.get("children", []):
                    walk([child], None, cidx)
            elif is_variable(kind) and name:
                var_id = f"{file_path}:{start_line}:{start_char}:{name}"
                kind_str = "param" if (isinstance(kind, int) and kind == 8) or (isinstance(kind, str) and kind.lower() == "parameter") else ("member" if scope_class is not None else ("local" if scope_func is not None else "global"))
                variables.append({
                    "id": var_id,
                    "name": name,
                    "file_path": file_path,
                    "start_line": start_line,
                    "kind": kind_str,
                    "scope_function_index": scope_func,
                    "scope_class_index": scope_class,
                    "start_character": start_char,
                })
                for child in s.get("children", []):
                    walk([child], scope_func, scope_class)
            else:
                for child in s.get("children", []):
                    walk([child], scope_func, scope_class)

    walk(symbols, None, None)
    return functions, classes, variables


def _open_file(proc: Any, file_uri: str, file_path: str) -> None:
    """发送 textDocument/didOpen，使 clangd 解析该文件后再响应 documentSymbol。"""
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception:
        content = ""
    notify(
        proc,
        "textDocument/didOpen",
        {
            "textDocument": {"uri": file_uri, "languageId": "cpp", "version": 1, "text": content},
        },
    )


def _document_symbol(proc: Any, file_uri: str, file_path: str | None = None) -> list[dict]:
    """请求 textDocument/documentSymbol，返回顶层 symbol 列表（含 children）。若传 file_path 会先 didOpen。"""
    if file_path:
        _open_file(proc, file_uri, file_path)
    result = request(
        proc,
        "textDocument/documentSymbol",
        {
            "textDocument": {"uri": file_uri},
        },
    )
    if result is None:
        return []
    return result if isinstance(result, list) else []


def _prepare_call_hierarchy(proc: Any, file_uri: str, line: int, character: int) -> list[dict]:
    """LSP line 为 0-based。返回 CallHierarchyItem 列表（可能多个重载）。"""
    result = request(
        proc,
        "textDocument/prepareCallHierarchy",
        {
            "textDocument": {"uri": file_uri},
            "position": {"line": line, "character": character},
        },
    )
    if result is None:
        return []
    return result if isinstance(result, list) else [result]


def _outgoing_calls(proc: Any, item: dict) -> list[dict]:
    """返回 CallHierarchyOutgoingCall 列表，每项含 to: CallHierarchyItem。"""
    result = request(proc, "callHierarchy/outgoingCalls", {"item": item})
    if result is None:
        return []
    return result if isinstance(result, list) else []


def _references(proc: Any, file_uri: str, line: int, character: int) -> list[dict]:
    """textDocument/references，line/character 为 0-based。返回 Location 列表。"""
    result = request(
        proc,
        "textDocument/references",
        {
            "textDocument": {"uri": file_uri},
            "position": {"line": line, "character": character},
            "context": {"includeDeclaration": True},
        },
    )
    if result is None:
        return []
    return result if isinstance(result, list) else []


def _func_id(file_path: str, name: str, start_line: int) -> str:
    """与 graph_builder 一致。"""
    return f"{file_path}:{name}:{start_line}"


def collect_all_via_clangd(
    compile_commands_dir: Path,
    repo_root: Path | None = None,
    *,
    delay_after_init: float = 3.0,
    delay_between_files: float = 0.05,
    collect_var_refs: bool = True,
) -> tuple[list[dict[str, Any]], list[tuple[str, str, int]]]:
    """
    启动 clangd，遍历所有源文件，获取 documentSymbol（含 Variable）、每个函数的 outgoingCalls、
    每个变量的 textDocument/references。
    返回 (results, var_refs_global)。results 与 ast_parser 同结构，每项含 variables；var_refs_global 为 (func_id, var_id, line) 列表（跨文件引用）。
    collect_var_refs：是否对每个变量请求 references（会显著增加耗时）。
    """
    repo = repo_root or compile_commands_dir.parent
    source_files = _get_source_files_from_compile_commands(compile_commands_dir)
    
    # 获取头文件并合并
    header_files = _get_header_files(Path(repo))
    all_files = sorted(set(source_files + header_files))
    
    print(f"compile_commands.json 源文件: {len(source_files)} 个")
    print(f"发现头文件: {len(header_files)} 个")
    print(f"总计待解析: {len(all_files)} 个文件")
    
    if not all_files:
        return [], []

    proc = start_clangd(Path(repo).resolve(), Path(compile_commands_dir).resolve())
    try:
        initialize(proc, Path(repo))
        time.sleep(delay_after_init)
        repo_str = str(repo) if repo_root else ""

        results: list[dict[str, Any]] = []
        # 全局 (func_id, file_path, start_line, end_line) 用于把引用位置归属到函数
        all_functions: list[dict[str, Any]] = []
        var_refs_global: list[tuple[str, str, int]] = []

        for i, abs_path in enumerate(all_files):
            if repo_root and abs_path.startswith(str(repo_root)):
                file_path = os.path.relpath(abs_path, repo_root)
            else:
                file_path = abs_path
            file_uri = _path_to_uri(abs_path)

            try:
                symbols = _document_symbol(proc, file_uri, abs_path)
            except Exception:
                symbols = []
            functions, classes, variables = _collect_symbols_from_children(symbols, file_path, repo_root)

            for fn in functions:
                all_functions.append({
                    "id": _func_id(file_path, fn["name"], fn["start_line"]),
                    "file_path": file_path,
                    "start_line": fn["start_line"],
                    "end_line": fn.get("end_line", fn["start_line"]),
                })

            calls: list[dict[str, Any]] = []
            for idx, fn in enumerate(functions):
                line0 = max(0, fn["start_line"] - 1)
                char0 = fn.get("start_character", 0)
                try:
                    items = _prepare_call_hierarchy(proc, file_uri, line0, char0)
                except Exception:
                    items = []
                for item in items:
                    try:
                        outgoing = _outgoing_calls(proc, item)
                    except Exception:
                        outgoing = []
                    for out in outgoing:
                        to_item = out.get("to") or {}
                        callee_name = to_item.get("name", "")
                        if not callee_name:
                            continue
                        callee_uri = to_item.get("uri", "")
                        callee_path = _uri_to_path(callee_uri) if callee_uri else ""
                        if repo_root and callee_path.startswith(str(repo_root)):
                            callee_path = os.path.relpath(callee_path, repo_root)
                        r = to_item.get("range", {})
                        callee_line = r.get("start", {}).get("line", 0) + 1
                        calls.append({
                            "caller_index": idx,
                            "callee_name": callee_name,
                            "file_path": file_path,
                            "line": fn.get("start_line", 0),
                            "callee_file_path": callee_path or None,
                            "callee_line": callee_line if callee_path else None,
                        })
                time.sleep(0.02)

            if collect_var_refs and variables:
                for v in variables:
                    try:
                        refs = _references(proc, file_uri, max(0, v["start_line"] - 1), v.get("start_character", 0))
                    except Exception:
                        refs = []
                    for loc in refs:
                        ref_uri = loc.get("uri", "")
                        ref_fp = _uri_to_path(ref_uri)
                        if repo_root and ref_fp.startswith(str(repo_root)):
                            ref_fp = os.path.relpath(ref_fp, repo_root)
                        r = loc.get("range", {})
                        ref_line = r.get("start", {}).get("line", 0) + 1
                        # 引用行等于变量声明行时，不归属到任何函数，避免把声明误归到错误函数（如 fallback 选到前面的 size()）
                        if ref_fp == file_path and ref_line == v["start_line"]:
                            continue
                        chosen = None
                        for af in all_functions:
                            if af["file_path"] != ref_fp:
                                continue
                            if af["start_line"] <= ref_line <= af["end_line"]:
                                chosen = af
                                break
                        if chosen is None:
                            # Fallback: end_line 可能不完整，取同文件中 start_line <= ref_line 且 start_line 最大的函数
                            candidates = [af for af in all_functions if af["file_path"] == ref_fp and af["start_line"] <= ref_line]
                            if candidates:
                                chosen = max(candidates, key=lambda x: x["start_line"])
                        if chosen is not None:
                            var_refs_global.append((chosen["id"], v["id"], ref_line))
                    time.sleep(0.02)
            time.sleep(delay_between_files)

            results.append({
                "file_path": file_path,
                "functions": functions,
                "classes": classes,
                "calls": calls,
                "variables": variables,
                "var_refs": [],  # clangd 侧引用已放入 var_refs_global
            })
            if (i + 1) % 50 == 0:
                print(f"  clangd: 已处理 {i + 1}/{len(source_files)} 个文件…")
        return results, var_refs_global
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        except Exception:
            pass
