"""
C++ 解析器（基于 clangd LSP）。
包装 clangd_parser.collect_all_via_clangd() 为统一 Parser 接口。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

# 添加 src 路径以便导入 clangd_parser
_SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SRC))

from .base import Parser

# 复用 clangd_parser 的 source file 收集逻辑
try:
    from clangd_parser import (
        _get_source_files_from_compile_commands as _cpp_get_files,
        collect_all_via_clangd as _collect_all,
    )
    CLANGD_AVAILABLE = True
except Exception:
    CLANGD_AVAILABLE = False


class CppParser(Parser):
    """
    C++ 解析器，使用 clangd LSP。

    使用方式：
        parser = CppParser("/path/to/llama.cpp/build")
        results = parser.collect_all_tus(repo_root=Path("/path/to/llama.cpp"))
    """

    @property
    def language(self) -> str:
        return "cpp"

    def __init__(self, compile_commands_dir: str | Path):
        """
        compile_commands_dir: compile_commands.json 所在目录（通常是 build/）
        """
        self.compile_commands_dir = Path(compile_commands_dir)

    def get_source_files(self, repo_root: Path) -> list[str]:
        if not CLANGD_AVAILABLE:
            raise RuntimeError("clangd_parser 不可用")
        return _cpp_get_files(self.compile_commands_dir)

    def collect_all_tus(
        self,
        repo_root: Path,
        files: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        if not CLANGD_AVAILABLE:
            raise RuntimeError("clangd_parser 不可用")

        results, var_refs_global = _collect_all(
            compile_commands_dir=self.compile_commands_dir,
            repo_root=repo_root,
            collect_var_refs=True,
        )
        return results

    def collect_tu(self, repo_root: Path, file_path: str) -> dict[str, Any] | None:
        # clangd 不支持单文件解析，需要全量处理
        raise NotImplementedError("C++ 单文件解析请使用 collect_all_tus()")
