"""Graph 检索器 — 基于 Neo4j 代码图的检索。

策略：
1. 从问题提取关键词
2. 用关键词匹配函数名/文件路径
3. 可选：沿 CALLS 关系扩展 1 层调用链
4. 读取源码补充完整代码
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from neo4j import GraphDatabase

from .base import BaseRetriever, RetrievalResult
from ..tools.file_reader import read_function
from config import NEO4J_DATABASE

logger = logging.getLogger(__name__)

_STOPWORDS = {
    "a", "an", "the", "in", "on", "at", "to", "for", "of", "and", "or", "but",
    "is", "are", "was", "were", "be", "with", "by", "from", "it", "its", "this",
    "that", "these", "those", "i", "you", "he", "she", "we", "they",
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


class GraphRetriever(BaseRetriever):
    """基于 Neo4j 代码图的检索器。"""

    def __init__(
        self,
        driver: GraphDatabase.driver,
        repo_root: Path | str,
        database: str = NEO4J_DATABASE,
        enabled: bool = True,
        expand_calls_depth: int = 1,
        max_results: int = 10,
    ):
        super().__init__("graph", enabled)
        self.driver = driver
        self.repo_root = Path(repo_root).resolve()
        self.database = database
        self.expand_calls_depth = expand_calls_depth
        self.max_results = max_results

    def _extract_keywords(self, question: str) -> list[str]:
        words = re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]{2,}\b", question)
        keywords = []
        for w in words:
            w_lower = w.lower()
            if w_lower in _STOPWORDS or w_lower in _CPP_KEYWORDS:
                continue
            if "_" in w or any(c.isupper() for c in w[1:]):
                keywords.insert(0, w)
            else:
                keywords.append(w)
        return keywords[:6]

    def _run(self, cypher: str, params: dict) -> list[dict]:
        with self.driver.session(database=self.database) as s:
            r = s.run(cypher, params)
            return [dict(rec) for rec in r]

    def _fetch_by_keyword(self, keyword: str, limit: int) -> list[dict]:
        """用关键词匹配函数名或文件路径"""
        kw = keyword.lower().strip()
        rows = self._run("""
            MATCH (f:Function)
            WHERE toLower(f.name) CONTAINS $kw
               OR toLower(f.file_path) CONTAINS $kw
            RETURN f.name AS name, f.file_path AS file_path,
                   f.start_line AS start_line, f.end_line AS end_line,
                   f.signature AS signature
            ORDER BY f.start_line ASC
            LIMIT $limit
        """, {"kw": kw, "limit": limit})
        return rows

    def _expand_calls(self, func_name: str, depth: int) -> list[dict]:
        """沿 CALLS 关系扩展"""
        if depth <= 0:
            return []
        rows = self._run("""
            MATCH (f:Function {name: $name})-[:CALLS*1..%d]->(callee:Function)
            RETURN DISTINCT callee.name AS name, callee.file_path AS file_path,
                   callee.start_line AS start_line, callee.end_line AS end_line,
                   callee.signature AS signature
            ORDER BY callee.start_line ASC
            LIMIT 10
        """ % depth, {"name": func_name})
        return rows

    def retrieve(self, question: str, top_k: int = 5) -> list[RetrievalResult]:
        if not self.enabled:
            return []

        keywords = self._extract_keywords(question)
        if not keywords:
            return []

        seen = set()
        all_rows = []

        for kw in keywords[:3]:
            rows = self._fetch_by_keyword(kw, limit=top_k)
            for r in rows:
                key = r.get("name", "")
                if key and key not in seen:
                    seen.add(key)
                    all_rows.append(r)

        # CALLS 扩展（对 top 函数）
        if self.expand_calls_depth > 0:
            for r in list(all_rows)[:3]:
                name = r.get("name", "")
                if name:
                    callees = self._expand_calls(name, self.expand_calls_depth)
                    for c in callees:
                        key = c.get("name", "")
                        if key and key not in seen:
                            seen.add(key)
                            all_rows.append(c)

        results = []
        for r in all_rows[:top_k]:
            fp = r.get("file_path", "")
            sl = r.get("start_line", 0) or 0
            el = r.get("end_line", 0) or 0
            sig = r.get("signature", "")

            # 读取源码
            code = ""
            if fp and sl and el:
                code = read_function(fp, sl, el)

            content = f"Function: {r.get('name')}\nFile: {fp}\nSignature: {sig}\n\n{code}"

            results.append(RetrievalResult(
                id=r.get("name", ""),
                type="function",
                content=content,
                metadata={
                    "file_path": fp,
                    "start_line": sl,
                    "end_line": el,
                    "signature": sig,
                    "fan_in": r.get("fan_in", 0),
                },
                score=0.5 + (r.get("fan_in", 0) or 0) * 0.01,
                source="graph",
            ))

        return results
