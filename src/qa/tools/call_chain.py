"""调用链扩展工具 — callers / callees / same_file / same_class"""
from __future__ import annotations

from typing import Optional

from ..models import RetrievedFunction
from ..retrievers.base import RetrievalResult
from config import NEO4J_DATABASE


def _neo4j_query(cypher: str, params: dict) -> list[dict]:
    """执行 Neo4j 查询（延迟导入，避免循环依赖）"""
    from src.core.neo4j_client import run_cypher
    return run_cypher(cypher, params)


def _row_to_function(r: dict, source_tag: str) -> RetrievedFunction:
    """Neo4j 结果行转为 RetrievedFunction"""
    return RetrievedFunction(
        name=r.get("name", ""),
        file_path=r.get("file_path") or r.get("file", ""),
        start_line=r.get("start_line", 0) or 0,
        end_line=r.get("end_line", 0) or 0,
        signature=r.get("signature", ""),
        score=0.5,
        source=source_tag,
    )


def expand_callers(
    function_name: str,
    limit: int = 5,
    file_path: Optional[str] = None,
) -> list[RetrievedFunction]:
    """扩展调用者（谁调用了这个函数）"""
    if file_path:
        results = _neo4j_query("""
            MATCH (caller:Function)-[:CALLS]->(callee:Function {name: $name, file_path: $file_path})
            RETURN caller.name AS name, caller.file_path AS file_path,
                   caller.start_line AS start_line, caller.end_line AS end_line,
                   caller.signature AS signature
            LIMIT $limit
        """, {"name": function_name, "file_path": file_path, "limit": limit})
    else:
        results = _neo4j_query("""
            MATCH (caller:Function)-[:CALLS]->(callee:Function {name: $name})
            RETURN caller.name AS name, caller.file_path AS file_path,
                   caller.start_line AS start_line, caller.end_line AS end_line,
                   caller.signature AS signature
            LIMIT $limit
        """, {"name": function_name, "limit": limit})

    return [_row_to_function(r, f"caller_of_{function_name}") for r in results]


def expand_callees(
    function_name: str,
    limit: int = 5,
    file_path: Optional[str] = None,
) -> list[RetrievedFunction]:
    """扩展被调用者（这个函数调用了谁）"""
    if file_path:
        results = _neo4j_query("""
            MATCH (caller:Function {name: $name, file_path: $file_path})-[:CALLS]->(callee:Function)
            RETURN callee.name AS name, callee.file_path AS file_path,
                   callee.start_line AS start_line, callee.end_line AS end_line,
                   callee.signature AS signature
            LIMIT $limit
        """, {"name": function_name, "file_path": file_path, "limit": limit})
    else:
        results = _neo4j_query("""
            MATCH (caller:Function {name: $name})-[:CALLS]->(callee:Function)
            RETURN callee.name AS name, callee.file_path AS file_path,
                   callee.start_line AS start_line, callee.end_line AS end_line,
                   callee.signature AS signature
            LIMIT $limit
        """, {"name": function_name, "limit": limit})

    return [_row_to_function(r, f"callee_of_{function_name}") for r in results]


def expand_same_file(
    function_name: str,
    file_path: Optional[str] = None,
    limit: int = 10,
) -> list[RetrievedFunction]:
    """扩展同文件中的其他函数（按行号距离排序）"""
    if not file_path:
        res = _neo4j_query("""
            MATCH (f:Function {name: $name})
            RETURN f.file_path AS file_path, f.start_line AS start_line
            LIMIT 1
        """, {"name": function_name})
        if not res:
            return []
        file_path = res[0]["file_path"]
        target_line = res[0]["start_line"] or 0
    else:
        res = _neo4j_query("""
            MATCH (f:Function {name: $name, file_path: $file_path})
            RETURN f.start_line AS start_line
            LIMIT 1
        """, {"name": function_name, "file_path": file_path})
        target_line = res[0]["start_line"] if res else 0

    results = _neo4j_query("""
        MATCH (f:Function)
        WHERE f.file_path = $file_path AND f.name <> $name
        RETURN f.name AS name, f.file_path AS file_path,
               f.start_line AS start_line, f.end_line AS end_line,
               f.signature AS signature,
               abs(f.start_line - $target_line) AS distance
        ORDER BY distance ASC
        LIMIT $limit
    """, {"file_path": file_path, "name": function_name, "target_line": target_line, "limit": limit})

    return [_row_to_function(r, f"same_file_of_{function_name}") for r in results]


def expand_same_class(function_name: str, limit: int = 10) -> list[RetrievedFunction]:
    """扩展同 class/struct/namespace 中的其他方法"""
    class_name = None
    if "::" in function_name:
        parts = function_name.split("::")
        class_name = parts[0]

    if not class_name:
        return []

    results = _neo4j_query("""
        MATCH (f:Function)
        WHERE f.name STARTS WITH $prefix AND f.name <> $name
        RETURN f.name AS name, f.file_path AS file_path,
               f.start_line AS start_line, f.end_line AS end_line,
               f.signature AS signature
        LIMIT $limit
    """, {"prefix": class_name + "::", "name": function_name, "limit": limit})

    return [_row_to_function(r, f"same_class_of_{function_name}") for r in results]
