"""
Python AST 解析器。
使用 Python 内置 ast 模块解析 Python 源代码，构建函数/类/调用关系。
"""
from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Any

from .base import Parser


SOURCE_EXTENSIONS = {".py"}


class PythonASTVisitor(ast.NodeVisitor):
    """
    遍历 Python AST，收集：
    - 函数定义（FunctionDef, AsyncFunctionDef）
    - 类定义（ClassDef）
    - 函数调用（Call）
    - 导入语句（Import, ImportFrom）
    """

    def __init__(self, file_path: str):
        self.file_path = file_path
        self.functions: list[dict[str, Any]] = []
        self.classes: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self.variables: list[dict[str, Any]] = []
        self.imports: list[dict[str, Any]] = []
        self._current_class: list[str] = []  # 栈，用于嵌套类
        self._current_function: list[str] = []  # 栈，用于嵌套函数
        self._seen_names: set[str] = set()  # 去重

    def _safe_name(self, node: ast.AST) -> str:
        return ast.unparse(node) if hasattr(ast, "unparse") else ""

    def _get_line(self, node: ast.AST) -> int:
        return getattr(node, "lineno", 0)

    # ── 导入语句 ──────────────────────────────────────────────────────

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            self.imports.append({
                "module": alias.name,
                "name": alias.asname if alias.asname else None,
                "line": self._get_line(node),
                "type": "import",
            })

    def visit_ImportFrom(self, node: ast.ImportFrom):
        module = node.module or ""
        for alias in node.names:
            self.imports.append({
                "module": module,
                "name": alias.name,
                "asname": alias.asname,
                "line": self._get_line(node),
                "type": "from",
                "level": node.level,  # 0=绝对导入, 1+=相对导入
            })

    # ── 类定义 ─────────────────────────────────────────────────────────

    def visit_ClassDef(self, node: ast.ClassDef):
        class_name = node.name
        start = self._get_line(node)
        end = node.end_lineno or start

        # 基类信息
        bases = []
        for base in node.bases:
            bases.append(self._safe_name(base))

        self.classes.append({
            "name": class_name,
            "start_line": start,
            "end_line": end,
            "bases": bases,
            "file_path": self.file_path,
        })

        # 进入类作用域
        self._current_class.append(class_name)
        self.generic_visit(node)
        self._current_class.pop()

    # ── 函数定义 ───────────────────────────────────────────────────────

    def _add_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef, is_method: bool = False):
        func_name = node.name
        start = self._get_line(node)
        end = node.end_lineno or start

        # 方法标志（类内部定义）
        if self._current_class:
            is_method = True

        # 装饰器
        decorators = []
        for dec in node.decorator_list:
            decorators.append(self._safe_name(dec))

        # 参数（提取为字符串签名）
        args: list[str] = []
        args_obj = getattr(node, "args", None)
        if args_obj:
            for arg in getattr(args_obj, "posonlyargs", []) or []:
                args.append(arg.arg)
            for arg in getattr(args_obj, "args", []) or []:
                args.append(arg.arg)
            for arg in getattr(args_obj, "kwonlyargs", []) or []:
                args.append(arg.arg)
            vararg = getattr(args_obj, "vararg", None)
            if vararg:
                args.append(f"*{vararg.arg}")
            kwarg = getattr(args_obj, "kwarg", None)
            if kwarg:
                args.append(f"**{kwarg.arg}")

        # 去重（同名函数在同一文件）
        key = f"{self.file_path}:{func_name}:{start}"
        if key in self._seen_names:
            return
        self._seen_names.add(key)

        func_id = f"{self.file_path}:{func_name}:{start}"

        self.functions.append({
            "id": func_id,
            "name": func_name,
            "start_line": start,
            "end_line": end,
            "signature": f"def {func_name}({', '.join(args)})",
            "file_path": self.file_path,
            "is_method": is_method,
            "decorators": decorators,
            "is_async": isinstance(node, ast.AsyncFunctionDef),
            "parent_class": self._current_class[-1] if self._current_class else None,
        })

        # 收集函数体内的调用
        self._current_function.append(func_name)
        self._visit_body(node)
        self._current_function.pop()

    def _visit_body(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        """只遍历函数/类体，收集 Call 和嵌套定义"""
        for child in node.body:
            self.visit(child)
        # 也遍历装饰器（装饰器中可能有调用）
        for dec in getattr(node, "decorator_list", []):
            self.visit(dec)
        # 遍历参数默认值（Python 3.x 结构和 3.14 不同，兼容处理）
        args_obj = getattr(node, "args", None)
        if args_obj:
            # defaults: posonlyargs + args 的默认值（从右向左对齐）
            for default in getattr(args_obj, "defaults", []):
                self.visit(default)
            # kw_defaults: kwonlyargs 的默认值
            for default in getattr(args_obj, "kw_defaults", []):
                if default is not None:
                    self.visit(default)
            for arg in (getattr(args_obj, "posonlyargs", []) or []) + (getattr(args_obj, "args", []) or []) + (getattr(args_obj, "kwonlyargs", []) or []):
                if getattr(arg, "annotation", None):
                    self.visit(arg.annotation)

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self._add_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        self._add_function(node)

    # ── 函数调用 ───────────────────────────────────────────────────────

    def visit_Call(self, node: ast.Call):
        # 记录调用（用于构建 CALLS 边）
        caller_name = self._current_function[-1] if self._current_function else None
        if caller_name:
            caller_file = self.file_path
            # callee 可能是 Name, Attribute, Subscript
            callee_name = ""
            callee_expr = node.func

            if isinstance(callee_expr, ast.Name):
                callee_name = callee_expr.id
            elif isinstance(callee_expr, ast.Attribute):
                # self.method() 或 obj.method()
                callee_name = callee_expr.attr
            elif isinstance(callee_expr, ast.Subscript):
                callee_name = self._safe_name(callee_expr)

            if callee_name and not callee_name.startswith("_"):
                self.calls.append({
                    "caller_name": caller_name,
                    "caller_file": caller_file,
                    "caller_line": self._get_line(node),
                    "callee_name": callee_name,
                })

        self.generic_visit(node)


def parse_python_file(file_path: str) -> dict[str, Any] | None:
    """
    解析单个 Python 文件，返回 TU dict。
    """
    try:
        with open(file_path, encoding="utf-8", errors="replace") as f:
            source = f.read()
    except Exception:
        return None

    try:
        tree = ast.parse(source, filename=file_path)
    except SyntaxError:
        return None

    visitor = PythonASTVisitor(file_path)
    visitor.visit(tree)

    # 构建 calls：需要找到 caller 的全局 index
    # 由于 visitor 在遍历时不知道 caller 的 index，
    # 需要在返回后由调用者根据 caller_name + caller_file + caller_line 来匹配
    # 这里我们直接存储 caller_name，让后续步骤匹配
    return {
        "file_path": file_path,
        "functions": visitor.functions,
        "classes": visitor.classes,
        "calls": visitor.calls,
        "variables": visitor.variables,
        "imports": visitor.imports,
    }


class PythonParser(Parser):
    """Python AST 解析器"""

    @property
    def language(self) -> str:
        return "python"

    def get_source_files(self, repo_root: Path) -> list[str]:
        """遍历 repo_root，收集所有 .py 文件（排除 __pycache__）"""
        root = Path(repo_root).resolve()
        files = []
        for dirpath, dirnames, filenames in os.walk(root):
            # 跳过 __pycache__ 和隐藏目录
            dirnames[:] = [d for d in dirnames if d != "__pycache__" and not d.startswith(".")]

            for fname in filenames:
                if Path(fname).suffix == ".py":
                    fpath = os.path.join(dirpath, fname)
                    rel = os.path.relpath(fpath, root)
                    files.append(fpath)
        return sorted(files)

    def collect_tu(self, repo_root: Path, file_path: str) -> dict[str, Any] | None:
        """解析单个 Python 文件"""
        return parse_python_file(file_path)

    def collect_all_tus(
        self,
        repo_root: Path,
        files: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """
        重写：并行解析所有 Python 文件。
        关键：Python 的调用关系需要后处理（解析器只记录 caller_name，
        需要在全局函数列表中查找 caller_index）。
        """
        raw_results = super().collect_all_tus(repo_root, files)

        # 构建 file_path -> {func_name -> func_idx} 映射
        file_func_index: dict[str, dict[str, int]] = {}
        for result in raw_results:
            fp = result["file_path"]
            func_index: dict[str, int] = {}
            for i, fn in enumerate(result["functions"]):
                func_index[fn["name"]] = i
            file_func_index[fp] = func_index

        # 全局函数列表（用于跨文件查找）
        all_funcs: list[tuple[str, str]] = []  # (file_path, func_name)
        for result in raw_results:
            for fn in result["functions"]:
                all_funcs.append((result["file_path"], fn["name"]))

        # 处理每个文件的 calls：将 caller_name 转为 caller_index
        processed = []
        for result in raw_results:
            fp = result["file_path"]
            func_idx_map = file_func_index.get(fp, {})
            calls = []
            for call in result.get("calls", []):
                caller_name = call["caller_name"]
                caller_idx = func_idx_map.get(caller_name, -1)
                if caller_idx >= 0:
                    # 尝试解析 callee 的文件路径
                    # 已知问题：Python 中不经过类型分析很难确定跨文件调用
                    # 这里先只处理同文件调用，callee_file_path 留空
                    calls.append({
                        "caller_index": caller_idx,
                        "caller_name": caller_name,
                        "callee_name": call["callee_name"],
                        "line": call["caller_line"],
                        "callee_file_path": None,  # Python 需要 import 分析推断
                    })
            result["calls"] = calls
            processed.append(result)

        return processed
