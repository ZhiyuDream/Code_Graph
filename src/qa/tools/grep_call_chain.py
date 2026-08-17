"""Grep-based call chain tools for Tree-sitter-only environments.

When Neo4j CALLS edges are unavailable or imprecise, use grep to find
callers (who calls this function) and callees (what this function calls).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path


def grep_files(pattern: str, repo_root: Path, limit: int = 10) -> list[str]:
    """Search files containing pattern under repo_root."""
    if not pattern or not pattern.strip():
        return []
    try:
        result = subprocess.run(
            ["grep", "-r", "-l", "-E", pattern, str(repo_root)],
            capture_output=True, text=True, timeout=30,
        )
        files = result.stdout.strip().split("\n") if result.stdout.strip() else []
        rel_files = []
        for f in files:
            if f.startswith(str(repo_root)):
                rel = f[len(str(repo_root)):].lstrip("/")
            else:
                rel = f
            # 过滤非代码目录
            if rel.startswith((".git/", ".github/", "vendor/", "third_party/", "deps/", "build/")):
                continue
            if not rel.endswith((".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx")):
                continue
            rel_files.append(rel)
        return rel_files[:limit]
    except Exception:
        return []


def grep_callers(function_name: str, repo_root: Path, limit: int = 10) -> list[dict]:
    """Find functions that call function_name.

    Approach:
    1. Grep for files containing "function_name("
    2. For each file, extract all function definitions
    3. Check which function's body contains the matching line
    4. Return those functions as callers

    Returns list of dicts: {"name": str, "file": str, "line": int, "content": str}
    """
    if not function_name:
        return []

    pattern = rf"\b{re.escape(function_name)}\s*\("
    files = grep_files(pattern, repo_root, limit=limit * 2)

    callers = []
    for fp in files:
        try:
            abs_path = repo_root / fp
            with open(abs_path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()

            # 找到所有包含 function_name( 的行号
            match_lines = []
            for i, line in enumerate(lines, 1):
                if re.search(pattern, line):
                    stripped = line.strip()
                    if stripped.startswith("//") or stripped.startswith("*"):
                        continue
                    match_lines.append((i, stripped))

            if not match_lines:
                continue

            # 提取文件中的所有函数定义
            func_defs = []  # (name, start_line, end_line)
            brace_count = 0
            current_func = None
            current_start = 0

            # 匹配函数定义，排除控制语句（if/for/while/switch/catch）
            func_pattern = re.compile(r"^\s*(?!if\b|for\b|while\b|switch\b|catch\b)(?:[\w:<>]+\s+)*?([a-zA-Z_][a-zA-Z0-9_:]*)\s*\([^)]*\)\s*(?:const)?\s*\{")
            for i, line in enumerate(lines, 1):
                m = func_pattern.match(line)
                if m:
                    if current_func is not None:
                        func_defs.append((current_func, current_start, i - 1))
                    current_func = m.group(1).split("::")[-1]
                    current_start = i
                    brace_count = 1
                elif current_func is not None:
                    brace_count += line.count("{") - line.count("}")
                    if brace_count == 0:
                        func_defs.append((current_func, current_start, i))
                        current_func = None

            if current_func is not None:
                func_defs.append((current_func, current_start, len(lines)))

            # 对每个匹配行，找到包含它的函数
            for line_num, content in match_lines:
                caller_name = ""
                for name, start, end in func_defs:
                    if start <= line_num <= end:
                        caller_name = name
                        break
                callers.append({
                    "name": caller_name,
                    "file": fp,
                    "line": line_num,
                    "content": content,
                })
                if len(callers) >= limit:
                    return callers
        except Exception:
            continue
    return callers[:limit]


def grep_callees(
    function_name: str,
    file_path: str,
    start_line: int,
    end_line: int,
    repo_root: Path,
    limit: int = 10,
) -> list[dict]:
    """Extract functions called by a given function definition.

    Returns a list of dicts: {"name": str, "file": str}
    """
    try:
        abs_path = repo_root / file_path
        with open(abs_path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        s = max(0, start_line - 1)
        e = min(len(lines), end_line)
        code = "".join(lines[s:e])

        calls = re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_:]*)\s*\(", code)
        keywords = {
            "if", "for", "while", "switch", "return", "sizeof",
            "catch", "throw", "new", "delete", "static_cast",
            "dynamic_cast", "const_cast", "reinterpret_cast",
        }
        callees = []
        seen = set()
        for call in calls:
            name = call.split("::")[-1]
            if name not in keywords and len(name) > 2 and name not in seen:
                seen.add(name)
                callees.append({"name": name, "file": file_path})
        return callees[:limit]
    except Exception:
        return []


def read_function(
    function_name: str,
    file_path: str,
    repo_root: Path,
    max_chars: int = 0,  # 0 表示不截断
) -> dict:
    """Read a function's full implementation from a file.

    Args:
        function_name: Name of the function to read.
        file_path: Path to the file containing the function.
        repo_root: Repository root path.
        max_chars: Maximum characters to return; 0 means no truncation.

    Returns:
        Dict with "name", "file", "start_line", "end_line", "code".
    """
    try:
        abs_path = repo_root / file_path
        with open(abs_path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        # 查找函数定义行：更宽松的匹配
        # 用负向后行断言代替 \b：析构函数（~xxx）、operator== 等非单词字符开头的名字 \b 永远匹配不到
        start_idx = None
        pattern = rf"(?<![A-Za-z0-9_]){re.escape(function_name)}\s*\("
        for i, line in enumerate(lines):
            if re.search(pattern, line):
                stripped = line.strip()
                # 排除注释
                if stripped.startswith("//") or stripped.startswith("*"):
                    continue
                # 排除明显的函数调用（行尾是分号，且没有大括号）
                # 注意：多行函数定义的签名可能以逗号结尾，不能简单排除逗号结尾
                if stripped.endswith(";") and "{" not in stripped:
                    continue
                # 优先选择看起来像函数定义的：行首是 C/C++ 类型/修饰符关键字，
                # 或行内（或下一行）带 { —— 均为语言级特征，不依赖具体仓库的命名习惯
                if stripped.startswith((
                    "static ", "void ", "int ", "bool ", "auto ",
                    "template ", "inline ", "constexpr ", "extern ", "virtual ",
                    "std::", "string ", "char ", "float ", "double ", "struct ", "class ",
                )):
                    start_idx = i
                    break
                # 通用定义特征：行尾是 { 或下一非空行以 { 开头（且当前行不是控制语句/调用）
                if (stripped.endswith("{") or stripped.endswith(")") or stripped.endswith(",")) and not stripped.startswith(
                    ("if ", "if(", "for ", "for(", "while ", "while(", "switch ", "switch(", "return ", "catch ")
                ):
                    nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
                    if stripped.endswith("{") or nxt.startswith("{"):
                        start_idx = i
                        break
                # 如果没有匹配到定义特征，先记录下来作为备选
                if start_idx is None:
                    start_idx = i

        if start_idx is None:
            # Neo4j 行号表兜底：正则扫描找不到时（析构函数、宏内定义、特殊命名），
            # 用 tree-sitter 解析出的精确行号直接切区间（001/034 案例：~dtor 读不到导致死锁）
            try:
                from src.core.neo4j_client import run_cypher
                rows = run_cypher(
                    "MATCH (f:Function {file_path: $fp}) "
                    "WHERE f.name = $name OR f.name ENDS WITH $suffix "
                    "RETURN f.name AS name, f.start_line AS start, f.end_line AS end "
                    "ORDER BY f.start_line LIMIT 1",
                    {"fp": file_path, "name": function_name,
                     "suffix": "::" + function_name.split("::")[-1]},
                )
            except Exception:
                rows = []
            if rows and rows[0].get("start") and rows[0].get("end"):
                s, e = rows[0]["start"], rows[0]["end"]
                code = "".join(lines[s - 1:e])
                if max_chars > 0 and len(code) > max_chars:
                    code = code[:max_chars] + "\n... (truncated)"
                return {
                    "name": rows[0]["name"] or function_name,
                    "file": file_path,
                    "start_line": s,
                    "end_line": e,
                    "code": code,
                }
            return {"name": function_name, "file": file_path, "error": "function not found in this file"}

        # 向前回溯，找到函数签名的真正开始（处理多行签名和模板）
        sig_start = start_idx
        for i in range(start_idx - 1, max(-1, start_idx - 15), -1):
            line = lines[i].strip()
            # 遇到空行、注释、预处理指令、其他语句结束符，停止
            if not line or line.startswith("//") or line.startswith("#") or line.endswith(";"):
                break
            # 如果行尾有 ) 或 , 或 {，可能是签名的一部分
            if line.endswith((")", ",", "{", ":", ">")):
                sig_start = i
                break
            sig_start = i

        # 查找函数结束：匹配大括号
        brace_count = 0
        end_idx = start_idx
        started = False
        for i in range(sig_start, len(lines)):
            line = lines[i]
            for ch in line:
                if ch == "{":
                    brace_count += 1
                    started = True
                elif ch == "}":
                    brace_count -= 1
                    if started and brace_count == 0:
                        end_idx = i
                        break
            if started and brace_count == 0:
                break

        code = "".join(lines[sig_start:end_idx + 1])
        if max_chars > 0 and len(code) > max_chars:
            code = code[:max_chars] + "\n... (truncated)"

        return {
            "name": function_name,
            "file": file_path,
            "start_line": sig_start + 1,
            "end_line": end_idx + 1,
            "code": code,
        }
    except Exception as e:
        return {"name": function_name, "file": file_path, "error": str(e)}
