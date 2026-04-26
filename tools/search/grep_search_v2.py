#!/usr/bin/env python3
"""
Grep搜索工具 V2 - 参考 Claude Code CLI 的 GrepTool 实现优化

与 V1 的关键差异：
1. 使用 rg --json 替代脆弱的正则解析
2. 支持 files_with_matches / content / count 三种输出模式
3. 支持 -A/-B/-C 灵活上下文（不仅限于 -C）
4. --max-columns 截断超长行，防止 minified 污染
5. mtime 降序排序（最近修改的文件优先）
6. head_limit + offset 分页
7. 区分固定字符串(-F)和正则搜索
8. 全词匹配(-w)支持
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import List, Dict, Literal

REPO_ROOT = Path("/data/yulin/RUC/llama.cpp")


def _run_rg(
    keyword: str,
    codebase_path: Path,
    output_mode: Literal["content", "files_with_matches", "count"] = "content",
    context_before: int = 0,
    context_after: int = 0,
    fixed_strings: bool = False,
    word_regexp: bool = False,
    case_sensitive: bool = False,
    max_columns: int = 500,
    head_limit: int = 250,
    offset: int = 0,
    glob_patterns: List[str] | None = None,
    type_filter: str = "cpp",
) -> str:
    """
    构建并执行 ripgrep 命令，返回原始 stdout。
    参考 Claude Code: src/utils/ripgrep.ts + src/tools/GrepTool/GrepTool.ts
    """
    cmd = ["rg"]

    # --- 输出模式 ---
    if output_mode == "files_with_matches":
        cmd.append("-l")
    elif output_mode == "count":
        cmd.append("-c")
    else:  # content (默认)
        cmd.append("--json")
        cmd.append("-n")  # 行号

    # --- 上下文控制（Claude Code 支持 -A/-B/-C 独立设置）---
    if output_mode == "content":
        if context_before > 0:
            cmd += ["-B", str(context_before)]
        if context_after > 0:
            cmd += ["-A", str(context_after)]
        # 如果都没有设置，默认给3行上下文（兼容旧接口）
        if context_before == 0 and context_after == 0:
            cmd += ["-C", "3"]

    # --- 匹配模式 ---
    if fixed_strings:
        cmd.append("-F")  # 禁用正则，字面匹配
    if word_regexp:
        cmd.append("-w")  # 全词匹配
    if not case_sensitive:
        cmd.append("-i")  # 忽略大小写

    # --- 截断与限制（Claude Code 核心设计）---
    cmd += ["--max-columns", str(max_columns)]

    # --- 文件类型与过滤 ---
    if type_filter:
        cmd += ["--type-add", f"cpp:*.{{c,cpp,h,hpp}}", f"-t{type_filter}"]

    # --- 排除 VCS 和噪声目录（Claude Code 默认行为）---
    cmd += ["--glob", "!.git/**"]
    cmd += ["--glob", "!.svn/**"]
    cmd += ["--glob", "!build/**"]
    cmd += ["--glob", "!CMakeFiles/**"]
    cmd += ["--glob", "!node_modules/**"]

    if glob_patterns:
        for g in glob_patterns:
            cmd += ["--glob", g]

    # --- 隐藏文件支持 ---
    cmd.append("--hidden")

    # --- 搜索目标 ---
    cmd.append(keyword)
    cmd.append(str(codebase_path))

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

    if result.returncode not in (0, 1):  # rg 返回1表示无匹配，也是正常
        return ""

    stdout = result.stdout

    # --- 分页：head_limit + offset（Claude Code 策略）---
    if output_mode == "files_with_matches" and stdout:
        lines = stdout.strip().split("\n")
        lines = lines[offset:offset + head_limit]
        stdout = "\n".join(lines)
    elif output_mode == "content" and stdout:
        # --json 模式下，每行是一个JSON对象
        lines = stdout.strip().split("\n")
        lines = lines[offset:offset + head_limit * 10]  # content模式给更多余量
        stdout = "\n".join(lines)

    return stdout


def _parse_json_output(stdout: str) -> List[Dict]:
    """
    解析 rg --json 输出。
    rg --json 每行是一个独立JSON对象，类型包括：
    - {"type":"begin","data":{"path":{"text":"..."}}}
    - {"type":"match","data":{"path":...,"lines":...,"line_number":...}}
    - {"type":"context","data":{"path":...,"lines":...,"line_number":...}}
    - {"type":"end","data":{"path":...}}
    - {"type":"summary","data":...}
    """
    matches = []
    current_match = None

    for line in stdout.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        obj_type = obj.get("type")
        data = obj.get("data", {})

        if obj_type == "match":
            # 保存之前的 match
            if current_match:
                matches.append(current_match)

            path = _get_path(data)
            line_num = data.get("line_number", 0)
            lines_text = _get_lines_text(data)

            current_match = {
                "file": path,
                "line_number": line_num,
                "lines": [{"line": line_num, "content": lines_text}],
                "score": 0.6,
            }

        elif obj_type == "context" and current_match:
            line_num = data.get("line_number", 0)
            lines_text = _get_lines_text(data)
            current_match["lines"].append({
                "line": line_num,
                "content": lines_text,
            })

        elif obj_type == "end":
            if current_match:
                matches.append(current_match)
                current_match = None

    if current_match:
        matches.append(current_match)

    return matches


def _get_path(data: dict) -> str:
    """从 rg json 中提取文件路径"""
    path_data = data.get("path", {})
    if isinstance(path_data, dict):
        return path_data.get("text", "")
    return str(path_data)


def _get_lines_text(data: dict) -> str:
    """从 rg json 中提取匹配文本，去除末尾换行符"""
    lines_data = data.get("lines", {})
    if isinstance(lines_data, dict):
        text = lines_data.get("text", "")
        if text:
            return text.rstrip("\n")
        # bytes 模式解码
        bytes_data = lines_data.get("bytes")
        if bytes_data:
            try:
                return bytes(bytes_data).decode("utf-8", errors="replace").rstrip("\n")
            except Exception:
                return ""
    return str(lines_data).rstrip("\n")


def _sort_by_mtime(matches: List[Dict], base_path: Path) -> List[Dict]:
    """
    按文件修改时间（mtime）降序排序。
    Claude Code 策略：最近修改的文件优先。
    """
    def get_mtime(m):
        try:
            file_path = m.get("file", "")
            # file 可能是相对路径或绝对路径
            p = Path(file_path)
            if not p.is_absolute():
                p = base_path / file_path
            if p.exists():
                return os.path.getmtime(p)
        except Exception:
            pass
        return 0

    return sorted(matches, key=get_mtime, reverse=True)


def grep_codebase_v2(
    keyword: str,
    codebase_path: str = None,
    limit: int = 5,
    context_lines: int = 3,
    output_mode: Literal["content", "files_with_matches", "count"] = "content",
    fixed_strings: bool = False,
    word_regexp: bool = False,
    case_sensitive: bool = False,
    max_columns: int = 500,
    head_limit: int = 250,
    offset: int = 0,
    sort_by_mtime: bool = True,
) -> List[Dict] | int | List[str]:
    """
    优化的 grep 搜索，参考 Claude Code CLI 的 GrepTool 实现。

    Args:
        keyword: 搜索关键词
        codebase_path: 代码库路径（默认 llama.cpp）
        limit: 最大返回结果数（content模式下的match组数）
        context_lines: 上下文行数（兼容旧接口，映射为 -C）
        output_mode: 输出模式 - content/files_with_matches/count
        fixed_strings: 是否使用固定字符串搜索（-F）
        word_regexp: 是否全词匹配（-w）
        case_sensitive: 是否区分大小写
        max_columns: 单行最大字符数，超长截断
        head_limit: 结果上限（Claude Code 默认250）
        offset: 分页偏移
        sort_by_mtime: 是否按文件修改时间降序排序

    Returns:
        content模式: List[Dict] - 匹配结果列表
        files_with_matches模式: List[str] - 文件路径列表
        count模式: int - 总匹配数
    """
    if codebase_path is None:
        codebase_path = REPO_ROOT
    else:
        codebase_path = Path(codebase_path)

    stdout = _run_rg(
        keyword=keyword,
        codebase_path=codebase_path,
        output_mode=output_mode,
        context_before=context_lines // 2 if context_lines > 0 else 0,
        context_after=context_lines // 2 if context_lines > 0 else 0,
        fixed_strings=fixed_strings,
        word_regexp=word_regexp,
        case_sensitive=case_sensitive,
        max_columns=max_columns,
        head_limit=head_limit,
        offset=offset,
    )

    if not stdout.strip():
        if output_mode == "count":
            return 0
        elif output_mode == "files_with_matches":
            return []
        return []

    if output_mode == "files_with_matches":
        files = [line.strip() for line in stdout.strip().split("\n") if line.strip()]
        if sort_by_mtime:
            files = _sort_files_by_mtime(files, codebase_path)
        return files[:limit]

    if output_mode == "count":
        # rg -c 每行是 "file:count"，取总和
        total = 0
        for line in stdout.strip().split("\n"):
            if ":" in line:
                try:
                    total += int(line.split(":")[-1])
                except ValueError:
                    pass
        return total

    # content 模式
    matches = _parse_json_output(stdout)

    # 手动截断内容（--max-columns 在 --json 模式下不自动截断）
    for m in matches:
        for line_entry in m.get("lines", []):
            content = line_entry.get("content", "")
            if len(content) > max_columns:
                line_entry["content"] = content[:max_columns]
                line_entry["truncated"] = True

    if sort_by_mtime:
        matches = _sort_by_mtime(matches, codebase_path)

    # 兼容 V1 格式：移除顶层 line_number，融入 lines 结构
    for m in matches:
        if "line_number" in m:
            # 将 line_number 作为 lines 第一个元素的 line
            if m.get("lines") and len(m["lines"]) > 0:
                # 如果第一个元素没有 line，填入 line_number
                if m["lines"][0].get("line") is None:
                    m["lines"][0]["line"] = m["line_number"]
            del m["line_number"]

    return matches[:limit]


def _sort_files_by_mtime(files: List[str], base_path: Path) -> List[str]:
    """按 mtime 降序排序文件列表"""
    def get_mtime(f):
        try:
            p = Path(f)
            if not p.is_absolute():
                p = base_path / f
            if p.exists():
                return os.path.getmtime(p)
        except Exception:
            pass
        return 0
    return sorted(files, key=get_mtime, reverse=True)


# ============================================================================
# 兼容层：保持 V1 接口不变
# ============================================================================

def grep_codebase(
    keyword: str,
    codebase_path: str = None,
    limit: int = 5,
    context_lines: int = 3
) -> List[Dict]:
    """
    V1 兼容接口。内部调用 V2 实现，参数映射为等效行为。
    """
    return grep_codebase_v2(
        keyword=keyword,
        codebase_path=codebase_path,
        limit=limit,
        context_lines=context_lines,
        output_mode="content",
        fixed_strings=False,
        word_regexp=False,
        case_sensitive=False,
        max_columns=500,
        head_limit=250,
        offset=0,
        sort_by_mtime=True,
    )


# ============================================================================
# 新增工具函数（面向调用方的便利封装）
# ============================================================================

def grep_files(keyword: str, codebase_path: str = None, limit: int = 10) -> List[str]:
    """
    快速定位包含关键词的文件列表（files_with_matches 模式）。
    适合大范围筛选，比 content 模式快得多。
    """
    return grep_codebase_v2(
        keyword=keyword,
        codebase_path=codebase_path,
        limit=limit,
        output_mode="files_with_matches",
        sort_by_mtime=True,
    )


def grep_count(keyword: str, codebase_path: str = None) -> int:
    """
    统计关键词出现次数（count 模式）。
    适合判断关键词的"流行度"，决定是否值得深入搜索。
    """
    return grep_codebase_v2(
        keyword=keyword,
        codebase_path=codebase_path,
        output_mode="count",
    )


def grep_identifier(
    identifier: str,
    codebase_path: str = None,
    limit: int = 5,
    context_lines: int = 3
) -> List[Dict]:
    """
    搜索代码标识符（函数名、变量名等）。
    使用 -Fw（固定字符串 + 全词匹配），避免误匹配子串。
    """
    return grep_codebase_v2(
        keyword=identifier,
        codebase_path=codebase_path,
        limit=limit,
        context_lines=context_lines,
        fixed_strings=True,
        word_regexp=True,
        case_sensitive=False,
        output_mode="content",
        sort_by_mtime=True,
    )


# ============================================================================
# V1 兼容的辅助函数
# ============================================================================

def extract_entities_from_question(question: str, use_llm: bool = True) -> List[str]:
    """V1 兼容接口，保持不变"""
    import re

    if use_llm:
        try:
            from ..core.llm_client import call_llm_json

            prompt = f"""从以下问题中提取关键代码实体（函数名、类名、变量名等）。

问题: {question}

要求:
1. 只提取具体的标识符名称（如函数名、类名）
2. 如果问题问的是"函数xxx"，提取xxx
3. 如果问的是"模块yyy"，提取yyy
4. 最多返回3个最相关的实体

返回JSON格式:
{{"entities": ["entity1", "entity2", "entity3"]}}

只输出JSON:"""

            result = call_llm_json(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=100
            )

            if result and "entities" in result:
                entities = result.get("entities", [])
                filtered = [e for e in entities if len(e) >= 3 and e.lower() not in [
                    "function", "class", "module", "variable", "code"
                ]]
                return filtered[:3]
        except Exception:
            pass

    patterns = [
        r'\b[a-zA-Z_][a-zA-Z0-9_]*::[a-zA-Z_][a-zA-Z0-9_]*\b',
        r'\bggml_[a-z_]+\b',
        r'\bllama_[a-z_]+\b',
        r'\b[A-Z][a-z]+[A-Z][a-zA-Z]*\b',
    ]

    entities = []
    for pattern in patterns:
        matches = re.findall(pattern, question)
        entities.extend(matches)

    seen = set()
    filtered = []
    for e in entities:
        if len(e) >= 3 and e.lower() not in ['function', 'class', 'module', 'variable', 'code']:
            if e not in seen:
                seen.add(e)
                filtered.append(e)

    return filtered[:3]


def search_module_functions(module_name: str, limit: int = 5) -> List[Dict]:
    """V1 兼容接口，保持不变"""
    import re
    from .code_reader import REPO_ROOT

    functions = []
    possible_paths = [
        f"ggml/src/{module_name}/{module_name}.cpp",
        f"ggml/src/{module_name.replace('-', '_')}.cpp",
        f"src/{module_name}.cpp",
        f"src/{module_name.replace('-', '_')}.cpp",
        f"{module_name}.cpp",
    ]

    for rel_path in possible_paths:
        full_path = REPO_ROOT / rel_path
        if full_path.exists():
            try:
                with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()

                func_pattern = r'(?:static\s+)?(?:\w+\s+)*(\w+)\s*\([^)]*\)\s*\{'
                matches = list(re.finditer(func_pattern, content))

                for i, match in enumerate(matches[:limit]):
                    func_name = match.group(1)
                    start_pos = max(0, match.start() - 100)
                    end_pos = min(len(content), match.end() + 500)
                    func_code = content[start_pos:end_pos]

                    functions.append({
                        'name': func_name,
                        'file': rel_path,
                        'text': func_code,
                        'score': 0.7,
                        'source': 'module_file'
                    })

                if functions:
                    break

            except Exception:
                continue

    return functions


def convert_grep_to_function_results(grep_results: List[Dict]) -> List[Dict]:
    """V1 兼容接口，保持不变"""
    import re
    from .code_reader import enrich_function_with_code, read_function_from_file

    functions = []
    seen_files = set()

    for result in grep_results:
        file_path = result.get('file', '')
        lines = result.get('lines', [])

        if not lines or file_path in seen_files:
            continue

        seen_files.add(file_path)

        match_line = None
        for l in lines:
            if l.get('line') is not None:
                match_line = l
                break

        if not match_line:
            continue

        content = match_line.get('content', '')
        func_name = 'unknown'
        line_num = match_line.get('line', 1)

        func_match = re.search(r'(?:\w+\s+)*(\w+)\s*\(', content)
        if func_match:
            candidate = func_match.group(1)
            if candidate not in ['if', 'for', 'while', 'switch', 'return', 'sizeof']:
                func_name = candidate

        code_lines = [l.get('content', '') for l in lines]
        code_text = '\n'.join(code_lines)

        func = {
            'name': func_name,
            'file': file_path,
            'text': code_text[:800],
            'score': result.get('score', 0.5),
            'source': 'grep_fallback',
            'match_line': line_num
        }

        if func_name != 'unknown':
            full_code = read_function_from_file(
                file_path=file_path,
                func_name=func_name,
                max_lines=40
            )
            if full_code and len(full_code) > len(code_text):
                func['text'] = full_code
                func['code_enriched'] = True

        functions.append(func)

    return functions
