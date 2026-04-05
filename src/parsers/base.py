"""
统一解析器接口。
所有语言解析器（C++, Python, ...）都实现此接口。
切换语言只需传入不同的解析器实例。
"""
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class Parser(ABC):
    """统一解析器接口"""

    @property
    @abstractmethod
    def language(self) -> str:
        """语言标识: 'cpp', 'python', etc."""
        ...

    @abstractmethod
    def get_source_files(self, repo_root: Path) -> list[str]:
        """
        获取仓库中所有需要解析的源文件路径。
        repo_root: 仓库根目录
        返回: 绝对路径列表
        """
        ...

    def collect_all_tus(
        self,
        repo_root: Path,
        files: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """
        收集所有 Translation Unit 的解析结果。

        每个 TU dict 的格式（统一）:
        {
            "file_path": str,           # 相对于 repo_root 的路径
            "functions": [...],           # 函数列表
            "classes": [...],            # 类列表
            "calls": [...],              # 调用关系
            "variables": [...],          # 变量
            "imports": [...],            # 导入语句（可选，用于Python）
            "exports": [...],            # 导出语句（可选）
        }

        默认实现调用 collect_tu 遍历所有文件，子类可重写以并行化。
        """
        if files is None:
            files = self.get_source_files(repo_root)

        results = []
        for fp in files:
            try:
                result = self.collect_tu(repo_root, fp)
                if result:
                    results.append(result)
            except Exception as e:
                # 单文件失败不影响整体
                print(f"  [WARN] Failed to parse {fp}: {e}", flush=True)
        return results

    @abstractmethod
    def collect_tu(self, repo_root: Path, file_path: str) -> dict[str, Any] | None:
        """
        解析单个源文件。

        file_path: 相对于 repo_root 的路径，或绝对路径
        返回: TU dict 格式同 collect_all_tus，失败返回 None
        """
        ...
