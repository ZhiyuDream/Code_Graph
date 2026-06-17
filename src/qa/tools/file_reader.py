"""文件读取工具 — 从源码文件读取函数/类/行范围"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import os

# 优先从 config.py 读取 REPO_ROOT（支持 .env 文件配置），fallback 到环境变量
_repo_root = ""
try:
    from config import REPO_ROOT as _CFG_REPO_ROOT
    _repo_root = _CFG_REPO_ROOT
except ImportError:
    pass
if not _repo_root:
    _repo_root = os.environ.get("REPO_ROOT", "")

REPO_ROOT = Path(_repo_root) if _repo_root else Path("/data/yulin/RUC/llama.cpp")


def resolve_path(file_path: str) -> Path | None:
    """解析文件路径，支持相对路径和绝对路径"""
    if file_path.startswith("/"):
        p = Path(file_path)
    else:
        p = REPO_ROOT / file_path
    return p if p.exists() else None


def read_lines(file_path: str, start_line: int = 1, end_line: int = 0) -> list[str]:
    """读取指定行范围（1-based，含头含尾）。end_line=0 表示读到末尾。"""
    p = resolve_path(file_path)
    if not p:
        return []
    try:
        with open(p, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except Exception:
        return []
    s = max(0, start_line - 1)
    e = len(lines) if end_line == 0 else min(len(lines), end_line)
    return lines[s:e]


def read_function(file_path: str, start_line: int, end_line: int) -> str:
    """读取函数完整实现"""
    lines = read_lines(file_path, start_line, end_line)
    return "".join(lines)


def read_full_file(file_path: str) -> str:
    """读取完整文件内容"""
    p = resolve_path(file_path)
    if not p:
        return f"// 文件不存在: {file_path}"
    try:
        with open(p, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception as e:
        return f"// 读取文件失败: {e}"


def extract_signature(lines: list[str], func_name: str) -> str:
    """从代码行列表中提取函数签名"""
    if not lines:
        return ""
    # 简单策略：找到包含函数名的行，向前回溯到函数签名开始
    for i, line in enumerate(lines):
        if func_name in line and "(" in line:
            # 向前回溯最多10行，找签名起始
            start = max(0, i - 10)
            sig_lines = []
            for j in range(start, i + 1):
                l = lines[j].rstrip("\n")
                if l.strip() or j == i:
                    sig_lines.append(l)
            # 合并多行签名
            sig = " ".join(l.strip() for l in sig_lines)
            # 截断到第一个 { 或 ; 之前
            if "{" in sig:
                sig = sig[:sig.index("{")]
            if ";" in sig and "(" in sig and sig.index(";") > sig.index("("):
                sig = sig[:sig.index(";")]
            return sig.strip()
    return lines[0].strip() if lines else ""


def find_class_bounds(lines: list[str], class_name: str) -> tuple[int, int]:
    """在文件中找到类的起止行号（0-based）"""
    start_idx = -1
    # 匹配 class/struct 定义
    class_pattern = re.compile(
        rf"^(?:\s*template\s*<[^>]+>\s*)?(?:\s*class|struct)\s+{re.escape(class_name)}\b"
    )
    for i, line in enumerate(lines):
        if class_pattern.search(line):
            start_idx = i
            break

    if start_idx < 0:
        return -1, -1

    # 从大括号开始计数
    brace_count = 0
    in_class = False
    for i in range(start_idx, len(lines)):
        for ch in lines[i]:
            if ch == "{":
                brace_count += 1
                in_class = True
            elif ch == "}":
                brace_count -= 1
                if in_class and brace_count == 0:
                    return start_idx, i + 1
    return start_idx, len(lines)
