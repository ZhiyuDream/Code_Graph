"""
源文件收集器：从 compile_commands.json 和仓库目录收集需要解析的 C/C++ 源文件。

策略：
1. 从 compile_commands.json 收集主要编译单元（.cpp/.c/.cc/.cxx）
2. 通过 os.walk 收集项目自身的头文件（.h/.hpp），用于解析 inline/模板定义
3. 补充收集不在 compile_commands.json 中的源文件（条件编译的后端文件）
4. 按文件类型排序：先实现文件，后头文件，确保 clangd 先建立编译上下文
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SOURCE_EXTS = {".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hh", ".hxx"}
HEADER_EXTS = {".h", ".hpp", ".hh", ".hxx"}
IMPL_EXTS = {".c", ".cpp", ".cc", ".cxx"}

# 排除生成的模板实例化文件：只有 #include + 宏调用，clangd 解析超时
SKIP_PATTERNS = ["template-instances/fattn-", "template-instances/mmq-"]

# os.walk 时跳过的目录
SKIP_DIRS = {".git", "build", "bin", "obj", ".cache", "node_modules", "venv", ".venv", "__pycache__"}
SKIP_VENDOR = {"vendor", "third_party", "deps"}


def _should_skip(fpath: str) -> bool:
    for pat in SKIP_PATTERNS:
        if pat in fpath:
            return True
    return False


def _should_include(rel_path: str, include_dirs: list[str] | None) -> bool:
    if include_dirs is None:
        return True
    top = rel_path.split(os.sep)[0]
    return top in include_dirs


def collect_source_files(
    compile_commands_dir: Path,
    repo_root: Path,
    include_dirs: list[str] | None = None,
) -> list[str]:
    """
    收集源文件。

    正确用法（clangd 规范）：
    1. **只从 compile_commands.json 收集** —— 不在 compile_commands 中的文件缺少编译上下文，
       clangd 无法正确解析（会导致 timeout 或返回空符号）。
    2. **不通过 os.walk 补充 .h 头文件** —— 头文件中的符号应通过包含它的 .cpp 文件的
       documentSymbol 间接获取（预处理后的符号列表包含来自头文件的 inline/模板定义）。
       对头文件单独调用 documentSymbol 只有在 clangd 已通过某个 .cpp 建立了编译上下文后才有效。
    3. **按文件类型排序**：先 .cpp/.c/.cc/.cxx（实现文件），后 .h/.hpp（头文件）
       确保 clangd 先索引实现文件，为后续头文件解析建立上下文。

    Args:
        compile_commands_dir: compile_commands.json 所在目录
        repo_root: 仓库根目录
        include_dirs: 若指定，只收集这些目录下的文件（相对于 repo_root 的顶层目录名）

    Returns:
        相对于 repo_root 的源文件路径列表（已排序、已过滤）
    """
    cc_path = compile_commands_dir / "compile_commands.json"
    source_files: set[str] = set()

    if not cc_path.exists():
        raise RuntimeError(f"compile_commands.json not found: {cc_path}")

    # 1. 从 compile_commands.json 收集编译单元（.cpp/.c/.cc/.cxx）
    with open(cc_path, encoding="utf-8") as f:
        data = json.load(f)

    for entry in data:
        fpath = entry.get("file", "")
        if not fpath or Path(fpath).suffix.lower() not in IMPL_EXTS:
            continue
        if _should_skip(fpath):
            continue
        if not os.path.isabs(fpath):
            cwd = entry.get("directory", "")
            fpath = os.path.normpath(os.path.join(cwd, fpath))
        rel = os.path.relpath(fpath, repo_root)
        if rel.startswith("build") and os.sep in rel:
            continue
        if not _should_include(rel, include_dirs):
            continue
        source_files.add(fpath)

    # 2. 收集头文件（.h/.hpp）— clangd 需要它们来解析 inline/模板定义
    # 只收集项目自身的头文件，排除 vendor/deps
    if repo_root.exists():
        for root, dirs, files in os.walk(repo_root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".") and d not in SKIP_VENDOR]
            for f in files:
                ext = Path(f).suffix.lower()
                if ext not in HEADER_EXTS:
                    continue
                full_path = os.path.normpath(os.path.join(root, f))
                rel = os.path.relpath(full_path, repo_root)
                if rel.startswith("build") and os.sep in rel:
                    continue
                if not _should_include(rel, include_dirs):
                    continue
                if _should_skip(rel):
                    continue
                source_files.add(full_path)

    # 3. 补充收集不在 compile_commands.json 中的 .cpp/.c 源文件
    # 这些通常是条件编译的后端文件（如 SYCL/CANN/WebGPU/RPC），默认 build 不包含它们
    # clangd 会尝试用 fallback 配置解析；若失败则退回到正则提取（不丢失文件）
    source_files_from_cc = set(source_files)
    if repo_root.exists():
        for root, dirs, files in os.walk(repo_root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".") and d not in SKIP_VENDOR]
            for f in files:
                ext = Path(f).suffix.lower()
                if ext not in IMPL_EXTS:
                    continue
                full_path = os.path.normpath(os.path.join(root, f))
                rel = os.path.relpath(full_path, repo_root)
                if rel.startswith("build") and os.sep in rel:
                    continue
                if not _should_include(rel, include_dirs):
                    continue
                if _should_skip(rel):
                    continue
                if full_path not in source_files_from_cc:
                    source_files.add(full_path)
                    logger.debug("Supplementing source file not in compile_commands.json: %s", rel)

    # 按文件类型排序：先实现文件(.cpp/.c)，后头文件(.h/.hpp)
    # clangd 先索引 .cpp 文件，为后续 .h 文件解析建立编译上下文
    def _sort_key(path: str) -> tuple:
        ext = Path(path).suffix.lower()
        is_header = 1 if ext in HEADER_EXTS else 0
        return (is_header, path)

    sorted_paths = sorted(source_files, key=_sort_key)

    # 过滤不存在的文件（compile_commands 过期导致）
    existing_paths = [p for p in sorted_paths if os.path.exists(p)]
    skipped = len(sorted_paths) - len(existing_paths)
    if skipped > 0:
        logger.warning("Skipped %d files not found (compile_commands may be stale)", skipped)

    # 转换为相对路径
    result = []
    for abs_path in existing_paths:
        if abs_path.startswith(str(repo_root)):
            result.append(os.path.relpath(abs_path, repo_root))
        else:
            result.append(abs_path)
    return result
