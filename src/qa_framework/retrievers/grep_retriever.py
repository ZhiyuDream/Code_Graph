"""
Grep 关键词检索器。
从 llama.cpp 源码中通过 ripgrep 搜索包含问题关键词的文件和行，
返回匹配的函数/代码片段。
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path

from .base import BaseRetriever, RetrievalResult

logger = logging.getLogger(__name__)

# 停用词过滤
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

# C/C++ 关键字过滤（避免 grep 这些）
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
        return keywords[:6]  # 最多 6 个关键词

    def _grep_file(self, keyword: str) -> list[dict]:
        """对单个关键词执行 ripgrep。"""
        cmd = [
            "rg",
            "-i",
            "--type", "cpp",
            "--type", "c",
            "-n",
            "-C", str(self.context_lines),
            "--json",
            keyword,
            str(self.repo_root),
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            logger.warning("rg command failed or not found")
            return []

        matches = []
        current_file = None
        current_lines = []
        current_start = None

        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            try:
                data = json.loads(line)
            except Exception:
                continue

            msg_type = data.get("type")
            if msg_type == "begin":
                current_file = data.get("data", {}).get("path", {}).get("text", "")
            elif msg_type == "end":
                if current_file and current_lines:
                    matches.append({
                        "file": current_file,
                        "line": current_start,
                        "content": "\n".join(current_lines),
                    })
                current_file = None
                current_lines = []
                current_start = None
            elif msg_type == "match":
                mdata = data.get("data", {})
                line_num = mdata.get("line_number", 0)
                lines_data = mdata.get("lines", {})
                text = lines_data.get("text", "")
                if current_start is None:
                    current_start = line_num
                current_lines.append(text)
            elif msg_type == "context":
                cdata = data.get("data", {})
                text = cdata.get("lines", {}).get("text", "")
                current_lines.append(text)

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

        # 去重：按 (file, 行号范围) 去重
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
        for i, m in enumerate(deduped[:top_k]):
            rel_path = m["file"]
            if rel_path.startswith(str(self.repo_root)):
                rel_path = rel_path[len(str(self.repo_root)) + 1:]

            results.append(RetrievalResult(
                id=f"grep:{rel_path}:{m['line']}",
                type="chunk",
                content=m["content"],
                metadata={
                    "file_path": rel_path,
                    "line": m["line"],
                    "keyword_hits": _score(m),
                },
                score=min(1.0, _score(m) / max(1, len(keywords))),
                source="grep",
            ))

        return results
