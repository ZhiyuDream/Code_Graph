"""
Graph 检索器 — 从 Neo4j 代码图中检索相关节点，并读取完整源码。

核心设计：
1. 通过多种策略从 Neo4j 检索相关 Function/Class/Variable
2. 对每个节点，用 file_path + start_line + end_line 从源码读取完整代码
3. 返回给 LLM 的是「完整代码实现」而非仅签名

检索策略（可组合）：
- keyword_match: 函数名/文件路径包含问题关键词
- calls_expansion: 从命中函数沿 CALLS 展开 1-2 层调用链
- issue_mentions: 通过 Issue-MENTIONS->Function 关联
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from neo4j import GraphDatabase

from .base import BaseRetriever, RetrievalResult

# 高频函数降权（P2 优化）
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
from tools.search.frequency_penalty import is_high_frequency, DEFAULT_THRESHOLD

logger = logging.getLogger(__name__)

# 停用词
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


class GraphRetriever(BaseRetriever):
    """
    基于 Neo4j 代码图的检索器，返回完整代码内容。
    """

    def __init__(
        self,
        driver: GraphDatabase.driver,
        repo_root: Path | str,
        database: str = "neo4j",
        enabled: bool = True,
        max_code_chars: int = 3000,
        expand_calls_depth: int = 1,
    ):
        super().__init__("graph", enabled)
        self.driver = driver
        self.repo_root = Path(repo_root).resolve()
        self.database = database
        self.max_code_chars = max_code_chars
        self.expand_calls_depth = expand_calls_depth

    def _extract_keywords(self, question: str) -> list[str]:
        """提取有效关键词。"""
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

    def _read_code(
        self,
        file_path: str,
        start_line: int,
        end_line: int,
    ) -> str:
        """从源码文件读取指定行范围的代码。"""
        abs_path = self.repo_root / file_path
        if not abs_path.exists():
            return f"[File not found: {file_path}]"

        try:
            with open(abs_path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except Exception as e:
            return f"[Read error: {e}]"

        total = len(lines)
        s = max(0, start_line - 1)
        e = min(total, end_line)
        if s >= total:
            return f"[Line range {start_line}-{end_line} out of bounds, file has {total} lines]"

        snippet = lines[s:e]
        code = "".join(snippet)

        if len(code) > self.max_code_chars:
            code = code[:self.max_code_chars] + "\n... (truncated)"

        return code

    def _fetch_functions_by_keyword(self, keywords: list[str], limit: int = 10) -> list[dict]:
        """通过关键词匹配函数名和文件路径。"""
        if not keywords:
            return []

        # 构建 OR 条件
        conditions = " OR ".join(
            f"toLower(f.name) CONTAINS toLower('{kw.replace(chr(39), chr(39)+chr(39))}')"
            f" OR toLower(f.file_path) CONTAINS toLower('{kw.replace(chr(39), chr(39)+chr(39))}')"
            for kw in keywords
        )

        cypher = f"""
            MATCH (f:Function)
            WHERE {conditions}
            RETURN f.id AS id, f.name AS name, f.file_path AS file_path,
                   f.start_line AS start_line, f.end_line AS end_line,
                   f.signature AS signature
            LIMIT $limit
        """
        with self.driver.session(database=self.database) as s:
            r = s.run(cypher, limit=limit)
            return [dict(rec) for rec in r]

    def _fetch_functions_by_issue_mentions(self, keywords: list[str], limit: int = 10) -> list[dict]:
        """通过 Issue MENTIONS 边找到相关函数。"""
        if not keywords:
            return []

        kw_pattern = ".*(" + "|".join(re.escape(kw.lower()) for kw in keywords) + ").*"
        cypher = """
            MATCH (i:Issue)-[:MENTIONS]->(f:Function)
            WHERE toLower(i.title) =~ $pattern
               OR toLower(i.question) =~ $pattern
               OR toLower(i.answer) =~ $pattern
            RETURN f.id AS id, f.name AS name, f.file_path AS file_path,
                   f.start_line AS start_line, f.end_line AS end_line,
                   f.signature AS signature,
                   count(i) AS issue_count
            ORDER BY issue_count DESC
            LIMIT $limit
        """
        with self.driver.session(database=self.database) as s:
            r = s.run(cypher, pattern=kw_pattern, limit=limit)
            return [dict(rec) for rec in r]

    def _expand_calls(self, func_ids: list[str], depth: int = 1) -> list[dict]:
        """沿 CALLS 边展开调用链。"""
        if not func_ids or depth <= 0:
            return []

        cypher = f"""
            MATCH (f:Function)-[:CALLS*1..{depth}]->(callee:Function)
            WHERE f.id IN $func_ids
            RETURN DISTINCT callee.id AS id, callee.name AS name,
                   callee.file_path AS file_path,
                   callee.start_line AS start_line, callee.end_line AS end_line,
                   callee.signature AS signature
            LIMIT $limit
        """
        with self.driver.session(database=self.database) as s:
            r = s.run(cypher, func_ids=func_ids, limit=50)
            return [dict(rec) for rec in r]

    def _fetch_callers(self, func_ids: list[str]) -> list[dict]:
        """获取调用者（上游）。"""
        if not func_ids:
            return []

        cypher = """
            MATCH (caller:Function)-[:CALLS]->(f:Function)
            WHERE f.id IN $func_ids
            RETURN DISTINCT caller.id AS id, caller.name AS name,
                   caller.file_path AS file_path,
                   caller.start_line AS start_line, caller.end_line AS end_line,
                   caller.signature AS signature
            LIMIT $limit
        """
        with self.driver.session(database=self.database) as s:
            r = s.run(cypher, func_ids=func_ids, limit=30)
            return [dict(rec) for rec in r]

    def _expand_module(self, func_ids: list[str], keywords: list[str] = None, limit: int = 5) -> list[dict]:
        """
        P2: 模块扩展 — 召回命中函数所属 Module 的其他函数。

        策略：
        1. 找到 func_ids 所属的 Module
        2. 对每个 Module，召回与问题关键词相关的 limit 个函数
           （按度数排序，但只保留函数名或文件路径包含关键词的函数）
        """
        if not func_ids:
            return []

        # 构建关键词过滤条件（函数名或文件路径包含关键词）
        if keywords:
            kw_conditions = " OR ".join(
                f"toLower(other.name) CONTAINS toLower('{kw.replace(chr(39), chr(39)+chr(39))}')"
                f" OR toLower(other.file_path) CONTAINS toLower('{kw.replace(chr(39), chr(39)+chr(39))}')"
                for kw in keywords
            )
            cypher = f"""
                MATCH (f:Function)-[:BELONGS_TO]->(m:Module)
                WHERE f.id IN $func_ids
                WITH DISTINCT m
                MATCH (other:Function)-[:BELONGS_TO]->(m)
                WHERE NOT other.id IN $func_ids AND ({kw_conditions})
                OPTIONAL MATCH (other)-[r:CALLS]->()
                WITH other, count(r) AS degree
                ORDER BY degree DESC
                LIMIT $limit
                RETURN other.id AS id, other.name AS name,
                       other.file_path AS file_path,
                       other.start_line AS start_line, other.end_line AS end_line,
                       other.signature AS signature
            """
        else:
            # 无关键词时回退到原有逻辑（按度数排序）
            cypher = """
                MATCH (f:Function)-[:BELONGS_TO]->(m:Module)
                WHERE f.id IN $func_ids
                WITH DISTINCT m
                MATCH (other:Function)-[:BELONGS_TO]->(m)
                WHERE NOT other.id IN $func_ids
                OPTIONAL MATCH (other)-[r:CALLS]->()
                WITH other, count(r) AS degree
                ORDER BY degree DESC
                LIMIT $limit
                RETURN other.id AS id, other.name AS name,
                       other.file_path AS file_path,
                       other.start_line AS start_line, other.end_line AS end_line,
                       other.signature AS signature
            """
        with self.driver.session(database=self.database) as s:
            r = s.run(cypher, func_ids=func_ids, limit=limit)
            return [dict(rec) for rec in r]

    def retrieve(self, question: str, top_k: int = 5) -> list[RetrievalResult]:
        if not self.enabled:
            return []

        keywords = self._extract_keywords(question)
        logger.debug("Graph keywords: %s", keywords)

        # 1. 关键词匹配函数
        funcs_by_kw = self._fetch_functions_by_keyword(keywords, limit=top_k * 2)
        # 2. Issue MENTIONS 关联函数
        funcs_by_issue = self._fetch_functions_by_issue_mentions(keywords, limit=top_k)

        # 合并并去重（原始命中）
        primary_funcs: dict[str, dict] = {}
        for f in funcs_by_kw + funcs_by_issue:
            fid = f.get("id")
            if fid and fid not in primary_funcs:
                primary_funcs[fid] = f

        # 3. CALLS 展开（过滤超高频函数，减少噪声）
        call_funcs: dict[str, dict] = {}
        if self.expand_calls_depth > 0 and primary_funcs:
            expanded = self._expand_calls(list(primary_funcs.keys()), depth=self.expand_calls_depth)
            for f in expanded:
                fid = f.get("id")
                if fid and fid not in primary_funcs:
                    # 过滤超高频基础函数（如 ggml_abort, max, min 等）
                    if not is_high_frequency(f.get("name", "")):
                        call_funcs[fid] = f

        # 4. P2: Module 扩展 — 召回同模块的其他函数（过滤超高频）
        module_funcs: dict[str, dict] = {}
        all_so_far = {**primary_funcs, **call_funcs}
        module_expanded = self._expand_module(list(all_so_far.keys()), keywords=keywords, limit=top_k)
        for f in module_expanded:
            fid = f.get("id")
            if fid and fid not in all_so_far:
                # 过滤超高频基础函数
                if not is_high_frequency(f.get("name", "")):
                    module_funcs[fid] = f

        # 5. 按优先级合并并截断到 top_k * 2（给后续去重留空间）
        combined: dict[str, dict] = {}
        # 优先级1: 原始命中
        for fid, f in list(primary_funcs.items())[:top_k]:
            combined[fid] = f
        # 优先级2: CALLS 扩展
        for fid, f in list(call_funcs.items())[:top_k]:
            if fid not in combined:
                combined[fid] = f
        # 优先级3: Module 扩展
        module_limit = max(3, top_k // 2)
        for fid, f in list(module_funcs.items())[:module_limit]:
            if fid not in combined:
                combined[fid] = f

        # 6. 读取完整代码
        results = []
        for fid, f in list(combined.items())[:top_k]:
            file_path = f.get("file_path", "")
            start_line = f.get("start_line", 0) or 0
            end_line = f.get("end_line", 0) or start_line

            code = self._read_code(file_path, start_line, end_line)

            # 组装 content：签名 + 完整代码
            signature = f.get("signature", "")
            content = f"""Function: {f.get('name', '')}
File: {file_path} (lines {start_line}-{end_line})
Signature: {signature}

--- Code ---
{code}"""

            results.append(RetrievalResult(
                id=fid,
                type="function",
                content=content,
                metadata={
                    "file_path": file_path,
                    "start_line": start_line,
                    "end_line": end_line,
                    "name": f.get("name", ""),
                    "signature": signature,
                },
                score=1.0,  # Graph 检索不计算分数
                source="graph",
            ))

        return results
