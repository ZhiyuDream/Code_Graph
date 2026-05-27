"""文件邻域扩展 - 同文件/同 class 的函数扩展"""
from __future__ import annotations

from typing import List, Dict
from ..core.neo4j_client import run_cypher
from .code_reader import enrich_function_with_code


def expand_same_file(function_name: str, file_path: str | None = None, limit: int = 10) -> Dict:
    """
    扩展同文件中的其他函数（按行号邻近排序）
    
    Args:
        function_name: 目标函数名
        file_path: 文件路径（如果为None则自动查询）
        limit: 最大返回数量
        
    Returns:
        {"functions": [...], "source": function_name, "type": "same_file"}
    """
    # 如果未提供 file_path，先查询
    if not file_path:
        results = run_cypher("""
            MATCH (f:Function {name: $name})
            RETURN f.file_path AS file_path, f.start_line AS start_line
            LIMIT 1
        """, {"name": function_name})
        if not results:
            return {"functions": [], "source": function_name, "type": "same_file"}
        file_path = results[0]['file_path']
        target_line = results[0]['start_line']
    else:
        # 查询目标函数的行号
        results = run_cypher("""
            MATCH (f:Function {name: $name, file_path: $file_path})
            RETURN f.start_line AS start_line
            LIMIT 1
        """, {"name": function_name, "file_path": file_path})
        target_line = results[0]['start_line'] if results else 0
    
    # 查询同文件的其他函数，按行号距离排序
    results = run_cypher("""
        MATCH (f:Function)
        WHERE f.file_path = $file_path AND f.name <> $name
        RETURN f.name AS name, f.file_path AS file, 
               f.start_line AS start_line, f.end_line AS end_line,
               abs(f.start_line - $target_line) AS distance
        ORDER BY distance ASC
        LIMIT $limit
    """, {"file_path": file_path, "name": function_name, "target_line": target_line, "limit": limit})
    
    functions = []
    for r in results:
        func = {
            'name': r['name'],
            'file': r['file'],
            'start_line': r['start_line'],
            'end_line': r['end_line'],
            'distance': r['distance']
        }
        func = enrich_function_with_code(func)
        functions.append(func)
    
    return {
        "functions": functions,
        "source": function_name,
        "type": "same_file",
        "file_path": file_path
    }


def expand_same_class(function_name: str, limit: int = 10) -> Dict:
    """
    扩展同 class/struct/namespace 中的其他方法
    
    通过解析函数名推断 class（如 "llm_build_apertus::llm_build_apertus" → class="llm_build_apertus"）
    
    Args:
        function_name: 目标函数名
        limit: 最大返回数量
        
    Returns:
        {"functions": [...], "source": function_name, "type": "same_class"}
    """
    # 推断 class/namespace 名
    class_name = None
    if '::' in function_name:
        # C++ 类方法: Class::method → class = Class
        parts = function_name.split('::')
        class_name = parts[0]
    
    if not class_name:
        return {"functions": [], "source": function_name, "type": "same_class"}
    
    # 查询同名 namespace/class 前缀的其他函数
    pattern = f"{class_name}::%"
    results = run_cypher("""
        MATCH (f:Function)
        WHERE f.name STARTS WITH $prefix AND f.name <> $name
        RETURN f.name AS name, f.file_path AS file,
               f.start_line AS start_line, f.end_line AS end_line
        LIMIT $limit
    """, {"prefix": class_name + "::", "name": function_name, "limit": limit})
    
    functions = []
    for r in results:
        func = {
            'name': r['name'],
            'file': r['file'],
            'start_line': r['start_line'],
            'end_line': r['end_line']
        }
        func = enrich_function_with_code(func)
        functions.append(func)
    
    return {
        "functions": functions,
        "source": function_name,
        "type": "same_class",
        "class_name": class_name
    }
