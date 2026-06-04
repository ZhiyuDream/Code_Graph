"""Grep 关键词检索器。

通过 ripgrep 搜索代码库，返回匹配的函数/代码片段。
流程：提取关键词 → 执行 rg --json → 解析匹配 → 去重排序。
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path

from .base import BaseRetriever, RetrievalResult

logger = logging.getLogger(__name__)

_STOPWORDS = {
    "a", "an", "the", "in", "on", "at", "to", "for", "of", "and", "or", "but",
    "is", "are", "was", "were", "be", "with", "by", "from", "it", "its", "this",
    "that", "these", "those", "i", "you", "he", "she", "we", "they", "my", "your",
    "his", "her", "our", "their", "what", "which", "who", "when", "where", "why",
    "how", "all", "any", "both", "each", "few", "more", "most", "other", "some",
    "such", "no", "nor", "not", "only", "own", "same", "so", "than", "too", "very",
    "can", "will", "just", "should", "now", "use", "using", "used", "llama", "cpp",
    "error", "bug", "issue", "problem", "fix", "function", "code", "file", "class",
    "struct", "variable", "method", "call", "return", "void", "int", "char", "float",
    "double", "bool", "string", "const", "static", "public", "private", "protected",
}

_CPP_KEYWORDS = {
    "if", "else", "for", "while", "do", "switch", "case", "default", "break",
    "continue", "return", "goto", "try", "catch", "throw", "new", "delete",
    "class", "struct", "enum", "union", "typedef", "typename", "template",
    "namespace", "using", "public", "private", "protected", "virtual", "override",
    "const", "static", "extern", "inline", "volatile", "mutable", "explicit",
    "operator", "sizeof", "typeof", "decltype", "auto", "nullptr", "true", "false",
    "int", "char", "float", "double", "bool", "void", "long", "short", "signed",
    "unsigned", "size_t", "ssize_t", "uint32_t", "uint64_t", "int32_t", "int64_t",
}


class GrepRetriever(BaseRetriever):
    """基于 ripgrep 的关键词代码检索器。"""

    def __init__(
        self,
        repo_root: Path | str,
        enabled: bool = True,
        max_results: int = 20,
        context_lines: int = 5,
        source_extensions: tuple[str, ...] = (".c", ".cpp", ".cc", ".cxx", ".h", ".hpp"),
    ):
        super().__init__("grep", enabled)
        self.repo_root = Path(repo_root).resolve()
        self.max_results = max_results
        self.context_lines = context_lines
        self.source_extensions = source_extensions

    def _extract_keywords(self, question: str) -> list[str]:
        """从问题中提取有效关键词（过滤停用词和 C++ 关键字）。"""
        words = re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]{2,}\b", question)
        keywords = []
        for w in words:
            w_lower = w.lower()
            if w_lower in _STOPWORDS or w_lower in _CPP_KEYWORDS:
                continue
            # 优先保留大驼峰/下划线风格（可能是函数/变量名）
            if "_" in w or any(c.isupper() for c in w[1:]):
                keywords.insert(0, w)
            else:
                keywords.append(w)
        return keywords[:6]

    def _extract_functions_from_file(self, file_path: str) -> list[dict]:
        """一次性扫描文件，提取所有函数定义的位置和内容。返回列表用于缓存。"""
        full_path = Path(file_path)
        if not full_path.exists():
            return []
        try:
            with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
        except Exception:
            return []
        
        if not lines:
            return []
        
        funcs = []  # [(start_idx, end_idx, name), ...]
        
        # 第一步：找到所有可能的函数定义起始行
        potential_starts = []
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or stripped.startswith('//') or stripped.startswith('#'):
                continue
            if '(' in stripped and not stripped.endswith(';'):
                ctrl_keywords = ('if ', 'if(', 'for ', 'for(', 'while ', 'while(', 'switch ', 'switch(', 'catch ', 'catch(')
                if any(stripped.startswith(kw) for kw in ctrl_keywords):
                    continue
                if any(kw in stripped for kw in ['static', 'inline', 'virtual', 'const', 'void', 'int', 'bool', 'auto', 'template', 'struct', 'class', 'explicit', 'extern']):
                    potential_starts.append(i)
                elif re.search(r'[\w:]+\s*\(', stripped):
                    potential_starts.append(i)
        
        # 第二步：用大括号匹配找每个函数的结束位置
        for start in potential_starts:
            brace_start = -1
            for i in range(start, min(len(lines), start + 10)):
                if '{' in lines[i]:
                    brace_start = i
                    break
            if brace_start < 0:
                continue
            
            brace_count = 0
            end_idx = -1
            for i in range(brace_start, min(len(lines), start + 500)):
                for char in lines[i]:
                    if char == '{':
                        brace_count += 1
                    elif char == '}':
                        brace_count -= 1
                        if brace_count == 0:
                            end_idx = i
                            break
                if end_idx >= 0:
                    break
            
            if end_idx < 0:
                continue
            
            # 提取函数名
            sig_text = ''.join(lines[start:min(brace_start + 1, len(lines))])
            func_name = ""
            for match in re.finditer(r'([\w:]+)\s*\(', sig_text):
                candidate = match.group(1).strip()
                if candidate.lower() not in {'if', 'for', 'while', 'switch', 'catch', 'return', 'sizeof', 'decltype', 'static_cast', 'dynamic_cast', 'reinterpret_cast', 'const_cast'}:
                    func_name = candidate
                    if '::' in func_name:
                        func_name = func_name.split('::')[-1]
                    break
            
            funcs.append({
                "start": start,
                "end": end_idx,
                "name": func_name or f"line_{start + 1}",
            })
        
        return funcs

    def _extract_function_at_line(self, file_path: str, line_num: int, file_funcs: list[dict] | None = None) -> dict | None:
        """提取包含指定行的完整函数。file_funcs 用于缓存同一文件的函数边界。"""
        full_path = Path(file_path)
        if not full_path.exists():
            return None
        
        try:
            with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
        except Exception:
            return None
        
        if not lines or line_num < 1 or line_num > len(lines):
            return None
        
        target_idx = line_num - 1
        
        # 使用缓存的函数边界，或重新扫描
        funcs = file_funcs if file_funcs else self._extract_functions_from_file(file_path)
        
        # 找到包含 target_idx 的函数（最内层 = start 最大的）
        containing = []
        for f in funcs:
            if f["start"] <= target_idx <= f["end"]:
                containing.append(f)
        
        if not containing:
            return None
        
        best = max(containing, key=lambda x: x["start"])
        start, end, func_name = best["start"], best["end"], best["name"]
        
        func_lines = lines[start:end + 1]
        content = ''.join(func_lines)
        
        return {
            "name": func_name,
            "file": file_path,
            "line": start + 1,
            "content": content,
            "start_line": start + 1,
            "end_line": end + 1,
        }

    def _grep_file(self, keyword: str) -> list[dict]:
        """对单个关键词执行 ripgrep，返回匹配列表。"""
        cmd = [
            "rg", "-i",
            "--type", "cpp", "--type", "c",
            "-n", "-C", str(self.context_lines),
            "--json",
            keyword,
            str(self.repo_root),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            logger.warning("rg command failed or not found")
            return []

        # 收集所有匹配位置（文件+行号）
        match_positions = []
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            try:
                data = json.loads(line)
            except Exception:
                continue
            if data.get("type") == "match":
                mdata = data.get("data", {})
                file_path = mdata.get("path", {}).get("text", "")
                line_num = mdata.get("line_number", 0)
                if file_path and line_num > 0:
                    match_positions.append((file_path, line_num))

        # 对每个文件只扫描一次函数边界，然后所有匹配复用
        matches = []
        seen_funcs = set()
        file_cache = {}  # file_path -> list of func bounds
        
        for file_path, line_num in match_positions:
            if file_path not in file_cache:
                file_cache[file_path] = self._extract_functions_from_file(file_path)
            func_info = self._extract_function_at_line(file_path, line_num, file_cache[file_path])
            if func_info and func_info["name"] and not func_info["name"].startswith("line_"):
                key = (file_path, func_info["name"])
                if key not in seen_funcs:
                    seen_funcs.add(key)
                    matches.append(func_info)

        return matches

    def retrieve(self, question: str, top_k: int = 5) -> list[RetrievalResult]:
        if not self.enabled:
            return []

        keywords = self._extract_keywords(question)
        if not keywords:
            logger.debug("No keywords extracted from question")
            return []

        logger.debug("Grep keywords: %s", keywords)

        all_matches: list[dict] = []
        for kw in keywords:
            matches = self._grep_file(kw)
            all_matches.extend(matches)

        if not all_matches:
            return []

        # 去重：按 (file, 起始行号) 去重
        seen = set()
        deduped = []
        for m in all_matches:
            key = (m["file"], m["line"])
            if key not in seen:
                seen.add(key)
                deduped.append(m)

        # 排序：按关键词命中数排序
        def _score(m: dict) -> int:
            text = m["content"].lower()
            return sum(1 for kw in keywords if kw.lower() in text)

        deduped.sort(key=_score, reverse=True)

        results = []
        for m in deduped[:top_k]:
            rel_path = m["file"]
            if rel_path.startswith(str(self.repo_root)):
                rel_path = rel_path[len(str(self.repo_root)) + 1:]

            func_name = m.get("name", "")
            # 过滤掉没提取到函数名的结果
            if not func_name or func_name.startswith("line_"):
                continue

            results.append(RetrievalResult(
                id=func_name,
                type="function",
                content=m["content"],
                metadata={
                    "name": func_name,
                    "file_path": rel_path,
                    "line": m.get("line", 0),
                    "start_line": m.get("start_line", 0),
                    "end_line": m.get("end_line", 0),
                    "keyword_hits": _score(m),
                },
                score=min(1.0, _score(m) / max(1, len(keywords))),
                source="grep",
            ))

        return results
