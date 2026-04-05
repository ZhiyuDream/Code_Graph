"""
使用 libclang 解析 C/C++ 编译单元，提取 Function、Class 及 CALLS。
依赖 compile_commands.json 所在目录。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

try:
    from clang import cindex

    # 配置 libclang 库路径（需要早于任何 libclang 操作）
    # 优先用环境变量 LIBCLANG_PATH，否则尝试常见路径
    _libclang_path = os.environ.get("LIBCLANG_PATH", "/usr/lib/llvm-20/lib")
    _libclang_file = os.environ.get("CLANG_LIBRARY_FILE", "")
    if _libclang_file and os.path.exists(_libclang_file):
        cindex.Config.set_library_file(_libclang_file)
    elif os.path.exists(_libclang_path):
        cindex.Config.set_library_path(_libclang_path)
except ImportError:
    cindex = None  # type: ignore

# 仅处理 C/C++ 源文件
SOURCE_EXTENSIONS = {".c", ".cpp", ".cc", ".cxx"}


def _get_compilation_db(build_dir: Path) -> Any:
    if cindex is None:
        raise RuntimeError("需要安装 libclang: pip install libclang")
    return cindex.CompilationDatabase.fromDirectory(str(build_dir))


def _normalize_path(path: str, cwd: str) -> str:
    if os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(cwd, path))


def _is_source_file(path: str) -> bool:
    return Path(path).suffix.lower() in SOURCE_EXTENSIONS


def parse_translation_unit(
    source_path: str,
    build_dir: Path,
    repo_root: Path | None = None,
) -> dict[str, Any] | None:
    """
    解析单个编译单元，返回该文件内的 functions、classes、calls。
    返回 None 表示解析失败或非源文件。
    """
    if cindex is None:
        return None
    if not _is_source_file(source_path):
        return None

    compdb = _get_compilation_db(build_dir)
    commands = list(compdb.getCompileCommands(source_path))
    if not commands:
        return None

    cmd = commands[0]
    # 工作目录为 compile command 的 directory
    cwd = cmd.directory
    raw_args = list(cmd.arguments)

    # 过滤掉 build 系统专用 flags，这些不是 AST 解析用的
    # libclang index.parse() 只接受影响预处理/语义的 flags
    # 常见编译 driver 路径（如 /usr/bin/c++, /usr/bin/cc）需要过滤
    # 过滤掉 build 系统专用 flags，这些不是 AST 解析用的
    # libclang index.parse() 只接受影响预处理/语义的 flags
    # 常见编译 driver 路径需要过滤
    COMPILER_PATHS = {"/usr/bin/c++", "/usr/bin/c++-13", "/usr/bin/cc", "/usr/bin/gcc", "/usr/bin/g++", "/usr/bin/g++-13"}
    SOURCE_EXTENSIONS = {".c", ".cpp", ".cc", ".cxx", ".c++", ".h", ".hpp", ".hh", ".hxx"}
    # 过滤 -Werror* 和警告 flags（会干扰 libclang 解析）
    WARNING_FLAGS = {
        "-Wall", "-Wextra", "-Wpedantic", "-Wshadow", "-Wstrict-prototypes",
        "-Wpointer-arith", "-Wmissing-prototypes", "-Wcast-qual", "-Wconversion",
        "-Wsign-conversion", "-Wdouble-promotion", "-Wundef", "-Winline",
        "-Wfloat-equal", "-Wold-style-cast", "-Woverloaded-virtual",
        "-Wnon-virtual-dtor", "-Wctor-dtor-privacy", "-Wdelete-non-virtual-dtor",
        "-Wstrict-null-sentinel", "-Wexit-time-destructors", "-Wglobal-constructors",
        "-Wzero-as-null-pointer-constant", "-Wcomma", "-Wswitch-default",
        "-Wswitch-enum", "-Wlogical-op", "-Wdeprecated", "-Wdeprecated-declarations",
        "-Wno-unused-function", "-Wno-unused-parameter", "-Wno-cast-qual",
    }
    filtered_args: list[str] = []
    skip_next = False
    for i, arg in enumerate(raw_args):
        if skip_next:
            skip_next = False
            continue
        if arg in ("-o", "--output", "-fsyntax-only"):
            skip_next = True
            continue
        if arg in ("-c", "-S", "-E"):
            skip_next = True  # -c <file> 是编译命令，下一个是源文件
            continue
        if arg.startswith("-o"):
            continue  # -ofile
        if arg.startswith("@"):
            continue  # response file
        if arg.startswith("--driver-mode="):
            continue  # --driver-mode=g++ 等
        if arg.startswith("-Werror"):
            continue  # -Werror, -Werror=* 等
        if arg in WARNING_FLAGS:
            continue  # 警告 flags
        if arg.endswith(".o") or arg.endswith(".a") or arg.endswith(".so") or arg.endswith(".d"):
            continue  # output file paths
        if "CMakeFiles" in arg or "CXXCompiler" in arg or "CCompiler" in arg:
            continue
        # 过滤 compiler driver 路径
        if arg in COMPILER_PATHS:
            continue
        if i == 0 and ("/usr/bin/" in arg or "/usr/local/bin/" in arg):
            continue
        # 过滤源文件路径（compile_commands.json 的 args 中可能包含源文件）
        if any(arg.endswith(ext) for ext in SOURCE_EXTENSIONS) and os.path.isfile(arg):
            continue
        filtered_args.append(arg)

    # 若 source_path 为相对路径，转为绝对路径供 parse 使用
    abs_source = _normalize_path(source_path, cwd)
    if not os.path.isfile(abs_source):
        return None

    index = cindex.Index.create()
    try:
        tu = index.parse(abs_source, args=filtered_args, options=cindex.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD)
    except Exception:
        return None

    if tu.diagnostics and any(d.severity >= 3 for d in tu.diagnostics):  # Error 及以上可选择性跳过
        pass  # 仍尝试提取信息，不因诊断失败直接返回

    # 相对路径：若提供 repo_root 则相对于仓库根
    if repo_root and abs_source.startswith(str(repo_root)):
        file_path = os.path.relpath(abs_source, repo_root)
    else:
        file_path = abs_source

    functions: list[dict[str, Any]] = []
    classes: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    # 变量：声明 id -> {name, file_path, start_line, kind, scope_function_idx?, scope_class_idx?}
    variables_dict: dict[str, dict[str, Any]] = {}
    # 引用 (当前函数索引, var_id, 引用所在行)
    var_refs: list[tuple[int, str, int]] = []

    def _get_line(cursor: Any) -> int:
        loc = cursor.location
        return loc.line if loc else 0

    def _get_column(cursor: Any) -> int:
        loc = cursor.location
        return loc.column if loc else 0

    def _get_spelling(cursor: Any) -> str:
        return cursor.spelling or ""

    def _decl_file_path(cursor: Any) -> str:
        """声明所在文件的路径，相对 repo_root 若可能。"""
        loc = cursor.location
        if not loc or not loc.file:
            return file_path
        try:
            p = loc.file.name
        except Exception:
            return file_path
        if repo_root and p.startswith(str(repo_root)):
            return os.path.relpath(p, repo_root)
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(os.path.dirname(abs_source), p))

    def _var_id(fp: str, line: int, col: int, name: str) -> str:
        return f"{fp}:{line}:{col}:{name}"

    # 当前所在函数、类（用于 CONTAINS 归属）
    current_function_index: int = -1
    current_class_index: int = -1

    def _add_variable(cursor: Any, kind: str) -> None:
        fp = _decl_file_path(cursor)
        line = _get_line(cursor)
        col = _get_column(cursor)
        name = _get_spelling(cursor)
        if not name:
            return
        vid = _var_id(fp, line, col, name)
        if vid in variables_dict:
            return
        scope_func = current_function_index if kind in ("param", "local") else None
        scope_class = current_class_index if kind == "member" else None
        variables_dict[vid] = {
            "id": vid,
            "name": name,
            "file_path": fp,
            "start_line": line,
            "kind": kind,
            "scope_function_index": scope_func,
            "scope_class_index": scope_class,
        }

    def _cursor_file_path(cursor: Any) -> str:
        """获取 cursor 所在文件的路径（相对 repo_root 若可能）。"""
        loc = cursor.location
        if not loc or not loc.file:
            return file_path
        try:
            p = loc.file.name
        except Exception:
            return file_path
        if repo_root and p.startswith(str(repo_root)):
            return os.path.relpath(p, repo_root)
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(os.path.dirname(abs_source), p))

    def _is_system_header(fp: str) -> bool:
        """判断是否为系统头文件（应跳过其中定义的 struct/class）。"""
        return fp.startswith("/usr/include") or fp.startswith("/usr/lib")

    def _struct_id(fp: str, name: str, line: int) -> str:
        """Compute class ID consistent with graph_builder._class_id."""
        return f"{fp}:{name}:{line}"

    def visit(cursor: Any, in_other_file_struct: bool = False) -> None:
        """
        遍历 AST cursor。
        in_other_file_struct: 若为 True，表示在来自其他文件的 struct 内部递归（用于收集其字段）。
        """
        nonlocal current_function_index, current_class_index
        kind = cursor.kind
        cursor_fp = _cursor_file_path(cursor)
        is_other_file = cursor_fp != file_path

        # 跳过来自系统头文件的 cursor（避免处理 /usr/include 中的定义）
        if is_other_file and _is_system_header(cursor_fp):
            return

        # 来自项目其他文件的 cursor：
        # - 添加到 classes/functions 列表（使用实际的 file_path）
        # - 递归进入其 children 以收集字段
        if is_other_file:
            # 来自其他文件的 struct/class 定义：添加到 classes 列表，以便字段能找到 parent
            if kind in (cindex.CursorKind.CLASS_DECL, cindex.CursorKind.STRUCT_DECL):
                defn = cursor.get_definition()
                if defn is not None and defn == cursor:
                    name = _get_spelling(cursor)
                    start_line = _get_line(cursor)
                    extent = cursor.extent
                    end_line = extent.end.line if extent else start_line
                    cidx = len(classes)
                    classes.append({
                        "name": name or "?",
                        "file_path": cursor_fp,  # 使用实际的 file_path
                        "start_line": start_line,
                        "end_line": end_line,
                    })
                    prev_c = current_class_index
                    current_class_index = cidx
                    for child in cursor.get_children():
                        visit(child, in_other_file_struct=True)
                    current_class_index = prev_c
                    return
            # FIELD_DECL from other file
            if kind == cindex.CursorKind.FIELD_DECL:
                _add_variable(cursor, "member")
                return
            # 不处理的节点类型
            return

        # 函数定义（含 C 函数与 C++ 成员函数）
        if kind in (
            cindex.CursorKind.CXX_METHOD,
            cindex.CursorKind.FUNCTION_DECL,
            cindex.CursorKind.CONSTRUCTOR,
            cindex.CursorKind.DESTRUCTOR,
        ):
            defn = cursor.get_definition()
            if (defn is not None and defn == cursor) or kind == cindex.CursorKind.FUNCTION_DECL:
                name = _get_spelling(cursor)
                if not name and kind in (cindex.CursorKind.CONSTRUCTOR, cindex.CursorKind.DESTRUCTOR):
                    name = _get_spelling(cursor.type) or "?"
                start_line = _get_line(cursor)
                extent = cursor.extent
                end_line = extent.end.line if extent else start_line
                sig = cursor.type.spelling or ""
                idx = len(functions)
                functions.append({
                    "name": name,
                    "signature": sig,
                    "file_path": file_path,
                    "start_line": start_line,
                    "end_line": end_line,
                })
                prev_f, prev_c = current_function_index, current_class_index
                current_function_index = idx
                for child in cursor.get_children():
                    visit(child)
                current_function_index, current_class_index = prev_f, prev_c
                return

        # 类/结构体定义
        if kind in (cindex.CursorKind.CLASS_DECL, cindex.CursorKind.STRUCT_DECL):
            defn = cursor.get_definition()
            if defn is not None and defn == cursor:
                name = _get_spelling(cursor)
                start_line = _get_line(cursor)
                extent = cursor.extent
                end_line = extent.end.line if extent else start_line
                cidx = len(classes)
                classes.append({
                    "name": name or "?",
                    "file_path": file_path,
                    "start_line": start_line,
                    "end_line": end_line,
                })
                prev_c = current_class_index
                current_class_index = cidx
                for child in cursor.get_children():
                    visit(child)
                current_class_index = prev_c
                return

        # 变量声明：只收集声明，不遍历子节点（避免重复）
        if kind == cindex.CursorKind.PARM_DECL:
            _add_variable(cursor, "param")
        elif kind == cindex.CursorKind.FIELD_DECL:
            _add_variable(cursor, "member")
        elif kind == cindex.CursorKind.VAR_DECL:
            # 局部 vs 全局：沿 lexical_parent 向上找是否在函数体内
            p = cursor.lexical_parent
            is_local = False
            while p:
                if p.kind in (
                    cindex.CursorKind.CXX_METHOD,
                    cindex.CursorKind.FUNCTION_DECL,
                    cindex.CursorKind.CONSTRUCTOR,
                    cindex.CursorKind.DESTRUCTOR,
                    cindex.CursorKind.LAMBDA_EXPR,
                ):
                    is_local = True
                    break
                if p.kind in (cindex.CursorKind.TRANSLATION_UNIT, cindex.CursorKind.NAMESPACE):
                    break
                p = p.lexical_parent
            _add_variable(cursor, "local" if is_local else "global")

        # 引用：DeclRefExpr，绑定到变量声明
        if kind == cindex.CursorKind.DECL_REF_EXPR:
            ref = cursor.referenced
            if ref and ref.kind in (
                cindex.CursorKind.VAR_DECL,
                cindex.CursorKind.PARM_DECL,
                cindex.CursorKind.FIELD_DECL,
            ):
                ref_fp = _decl_file_path(ref)
                ref_line = _get_line(ref)
                ref_col = _get_column(ref)
                ref_name = _get_spelling(ref)
                if ref_name:
                    var_id = _var_id(ref_fp, ref_line, ref_col, ref_name)
                    if var_id not in variables_dict:
                        variables_dict[var_id] = {
                            "id": var_id,
                            "name": ref_name,
                            "file_path": ref_fp,
                            "start_line": ref_line,
                            "kind": "global",
                            "scope_function_index": None,
                            "scope_class_index": None,
                        }
                    if current_function_index >= 0:
                        var_refs.append((current_function_index, var_id, _get_line(cursor)))

        # 调用表达式：记录 (当前函数索引, 被调用名)
        if kind == cindex.CursorKind.CALL_EXPR:
            referenced = cursor.referenced
            callee_name = _get_spelling(referenced) if referenced else _get_spelling(cursor)
            if not callee_name:
                callee_name = cursor.displayname or "?"
            if current_function_index >= 0 and callee_name:
                calls.append({
                    "caller_index": current_function_index,
                    "callee_name": callee_name,
                    "file_path": file_path,
                    "line": _get_line(cursor),
                })

        for child in cursor.get_children():
            visit(child)

    visit(tu.cursor)

    variables_list = list(variables_dict.values())
    return {
        "file_path": file_path,
        "functions": functions,
        "classes": classes,
        "calls": calls,
        "variables": variables_list,
        "var_refs": var_refs,
    }


def _get_source_files_from_compile_commands(build_dir: Path) -> list[str]:
    """从 compile_commands.json 直接读取所有 C/C++ 源文件路径（与 clangd_parser 一致，避免 getAllCompileCommands 为空）。"""
    path = build_dir / "compile_commands.json"
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
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


def collect_all_tus(build_dir: Path, repo_root: Path | None = None) -> list[dict[str, Any]]:
    """
    从 compile_commands.json 中取出所有 C/C++ 源文件，逐个解析，返回成功解析的结果列表。
    优先用 JSON 直接读文件列表（与 clangd 一致）；若为空再尝试 compdb.getAllCompileCommands()。
    """
    if cindex is None:
        return []
    compdb = _get_compilation_db(build_dir)
    # 先尝试从 compile_commands.json 直接取文件列表（部分环境 getAllCompileCommands 返回空）
    source_files = _get_source_files_from_compile_commands(build_dir)
    if not source_files:
        for cmd in compdb.getAllCompileCommands():
            path = cmd.filename
            abs_path = _normalize_path(path, cmd.directory)
            if abs_path not in source_files and _is_source_file(path):
                source_files.append(abs_path)
        source_files = sorted(set(source_files))
    results: list[dict[str, Any]] = []
    for abs_path in source_files:
        # parse_translation_unit 内部会用 compdb.getCompileCommands(path)；path 用绝对路径或相对路径
        data = parse_translation_unit(abs_path, build_dir, repo_root)
        if data:
            results.append(data)
    return results
