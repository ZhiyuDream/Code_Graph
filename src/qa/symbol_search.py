"""Symbol Search — 基于函数名的文件定位工具函数

注意：符号提取逻辑已迁移到 query_analyzer.py（LLM-based）。
此模块仅保留 grep 搜索相关的底层工具函数。
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def grep_symbol_files(symbol: str, repo_root: str | Path) -> list[str]:
    """
    用 ripgrep 搜索符号名在代码库中出现的所有文件。
    返回相对路径列表（去重、排序，排除 build/ 目录）。
    """
    repo_root = Path(repo_root)
    try:
        cmd = [
            "rg", "-l", "-i",
            "--type", "cpp", "--type", "c", "--type", "h",
            symbol, str(repo_root),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode not in (0, 1):
            logger.warning("rg error for %s: rc=%d", symbol, result.returncode)
            return []

        files = []
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            # 转为相对路径
            rel = line.replace(str(repo_root) + "/", "").replace(str(repo_root), "")
            if rel.startswith("./"):
                rel = rel[2:]
            if rel.startswith("/"):
                rel = rel[1:]
            # 过滤 build/ 目录
            if rel.startswith("build/"):
                continue
            files.append(rel)

        return sorted(set(files))
    except Exception as e:
        logger.warning("grep_symbol_files error for %s: %s", symbol, e)
        return []
