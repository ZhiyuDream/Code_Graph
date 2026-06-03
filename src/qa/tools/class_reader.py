"""类完整实现读取工具。

通过函数名推断所属类，从 Neo4j 查询类成员，
再从源码文件读取完整类定义（含所有方法）。
"""
from __future__ import annotations

from typing import Optional

from ..models import RetrievedFunction
from .file_reader import read_lines, find_class_bounds
from config import NEO4J_DATABASE


def _neo4j_query(cypher: str, params: dict) -> list[dict]:
    from src.core.neo4j_client import run_cypher
    return run_cypher(cypher, params)


def infer_class_name(function_name: str) -> Optional[str]:
    """从函数名推断类名（C++ Class::method 格式）"""
    if "::" in function_name:
        return function_name.split("::")[0]
    return None


def get_class_members(class_name: str, limit: int = 30) -> list[dict]:
    """从 Neo4j 查询类的成员函数"""
    # 策略1：函数名以 Class:: 开头
    results = _neo4j_query("""
        MATCH (f:Function)
        WHERE f.name STARTS WITH $prefix
        RETURN f.name AS name, f.file_path AS file_path,
               f.start_line AS start_line, f.end_line AS end_line,
               f.signature AS signature
        ORDER BY f.start_line
        LIMIT $limit
    """, {"prefix": class_name + "::", "limit": limit})

    if results:
        return results

    # 策略2：查找 member_of_class 属性
    results = _neo4j_query("""
        MATCH (f:Function)
        WHERE f.member_of_class = $class_name
        RETURN f.name AS name, f.file_path AS file_path,
               f.start_line AS start_line, f.end_line AS end_line,
               f.signature AS signature
        ORDER BY f.start_line
        LIMIT $limit
    """, {"class_name": class_name, "limit": limit})

    return results


def expand_class(function_name: str) -> Optional[RetrievedFunction]:
    """
    展开函数所在类的完整实现。
    返回一个 RetrievedFunction，其 body 为完整类代码。
    """
    class_name = infer_class_name(function_name)

    # 策略1：函数名含 ::，直接推断类名
    if class_name:
        members = get_class_members(class_name)
    else:
        # 策略2：函数名不含 ::，通过 Neo4j 查 member_of_class
        res = _neo4j_query(
            "MATCH (f:Function {name: $name}) RETURN f.member_of_class AS cls LIMIT 1",
            {"name": function_name}
        )
        class_name = res[0]["cls"] if res and res[0].get("cls") else None
        if not class_name:
            return None
        members = get_class_members(class_name)

    if not members:
        return None

    # 确定主要文件（取成员最多的文件）
    from collections import Counter
    file_counts = Counter(m["file_path"] for m in members)
    main_file = file_counts.most_common(1)[0][0]

    # 读取文件内容
    lines = read_lines(main_file)
    if not lines:
        return None

    start_idx, end_idx = find_class_bounds(lines, class_name)
    if start_idx < 0:
        return None

    class_code = "".join(lines[start_idx:end_idx])

    return RetrievedFunction(
        name=class_name,
        file_path=main_file,
        start_line=start_idx + 1,
        end_line=end_idx,
        signature=f"class {class_name}",
        body=class_code,
        score=0.5,
        source=f"class_of_{function_name}",
    )
