"""Unified call chain interface for Tree-sitter and Neo4j environments.

Default implementation uses grep (Tree-sitter-friendly, no Neo4j dependency).
Neo4j implementation is kept as optional backend for clangd-based graphs.
"""
from __future__ import annotations

import os
from pathlib import Path

from .grep_call_chain import (
    grep_files,
    grep_callers,
    grep_callees,
    read_function,
)


# ── Backend Selection ───────────────────────────────────────────────

_USE_NEO4J = os.environ.get("USE_NEO4J_CALL_CHAIN", "0") == "1"


def _neo4j_get_callers(function_name: str, limit: int = 10) -> list[dict]:
    """Neo4j backend for callers (optional)."""
    try:
        from src.core.neo4j_client import run_cypher
        results = run_cypher("""
            MATCH (caller:Function)-[:CALLS]->(callee:Function {name: $name})
            RETURN caller.name AS name, caller.file_path AS file,
                   caller.start_line AS start_line, caller.end_line AS end_line
            LIMIT $limit
        """, {"name": function_name, "limit": limit})
        return [
            {
                "name": r["name"],
                "file": r["file"],
                "line": r["start_line"],
                "content": "",
            }
            for r in results
        ]
    except Exception:
        return []


def _neo4j_get_callees(function_name: str, limit: int = 10) -> list[dict]:
    """Neo4j backend for callees (optional)."""
    try:
        from src.core.neo4j_client import run_cypher
        results = run_cypher("""
            MATCH (caller:Function {name: $name})-[:CALLS]->(callee:Function)
            RETURN callee.name AS name, callee.file_path AS file,
                   callee.start_line AS start_line, callee.end_line AS end_line
            LIMIT $limit
        """, {"name": function_name, "limit": limit})
        return [
            {
                "name": r["name"],
                "file": r["file"],
                "line": r["start_line"],
                "content": "",
            }
            for r in results
        ]
    except Exception:
        return []


# ── Unified Interface ───────────────────────────────────────────────

def find_callers(function_name: str, repo_root: Path, limit: int = 10) -> list[dict]:
    """Find who calls this function.

    Returns list of dicts: {"name", "file", "line", "content"}
    """
    if _USE_NEO4J:
        return _neo4j_get_callers(function_name, limit)
    return grep_callers(function_name, repo_root, limit)


def find_callees(
    function_name: str,
    file_path: str,
    start_line: int,
    end_line: int,
    repo_root: Path,
    limit: int = 10,
) -> list[dict]:
    """Find what this function calls.

    Returns list of dicts: {"name", "file"}
    """
    if _USE_NEO4J:
        return _neo4j_get_callees(function_name, limit)
    return grep_callees(function_name, file_path, start_line, end_line, repo_root, limit)


def search_symbol(symbol_name: str, repo_root: Path, limit: int = 10) -> list[str]:
    """Search files containing a symbol."""
    return grep_files(rf"\b{symbol_name}\b", repo_root, limit)


def read_function_by_name(
    function_name: str,
    file_path: str,
    repo_root: Path,
    max_chars: int = 4000,
) -> dict:
    """Read a function's full implementation by name."""
    return read_function(function_name, file_path, repo_root, max_chars)


def expand_callers(function_name: str, limit: int = 10, file_path: str | None = None) -> list[dict]:
    """兼容旧接口：扩展调用者。"""
    from config import REPO_ROOT
    repo_root = Path(REPO_ROOT) if REPO_ROOT else Path(".")
    return find_callers(function_name, repo_root, limit)


def expand_callees(function_name: str, limit: int = 10, file_path: str | None = None) -> list[dict]:
    """兼容旧接口：扩展被调用者。"""
    from config import REPO_ROOT
    repo_root = Path(REPO_ROOT) if REPO_ROOT else Path(".")
    if not file_path:
        return []
    # 需要 start_line/end_line，这里无法获取，返回空
    return []


__all__ = [
    "find_callers",
    "find_callees",
    "expand_callers",
    "expand_callees",
    "search_symbol",
    "read_function_by_name",
    "grep_files",
    "grep_callers",
    "grep_callees",
    "read_function",
]
