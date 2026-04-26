"""调用链分析工具 - callers/callees扩展"""
from __future__ import annotations

from typing import List, Dict
from ..core.neo4j_client import run_cypher
from .code_reader import enrich_function_with_code


def get_callers(function_name: str, limit: int = 5) -> List[Dict]:
    """获取函数的调用者（谁调用了这个函数）"""
    results = run_cypher("""
        MATCH (caller:Function)-[:CALLS]->(callee:Function {name: $name})
        RETURN caller.name AS name, caller.file_path AS file,
               caller.start_line AS start_line, caller.end_line AS end_line
        LIMIT $limit
    """, {"name": function_name, "limit": limit})
    
    functions = []
    for r in results:
        func = {
            'name': r['name'],
            'file': r['file'],
            'start_line': r['start_line'],
            'end_line': r['end_line']
        }
        # 补充完整代码
        func = enrich_function_with_code(func)
        functions.append(func)
    return functions


def get_callees(function_name: str, limit: int = 5) -> List[Dict]:
    """获取函数的被调用者（这个函数调用了谁）"""
    results = run_cypher("""
        MATCH (caller:Function {name: $name})-[:CALLS]->(callee:Function)
        RETURN callee.name AS name, callee.file_path AS file,
               callee.start_line AS start_line, callee.end_line AS end_line
        LIMIT $limit
    """, {"name": function_name, "limit": limit})
    
    functions = []
    for r in results:
        func = {
            'name': r['name'],
            'file': r['file'],
            'start_line': r['start_line'],
            'end_line': r['end_line']
        }
        # 补充完整代码
        func = enrich_function_with_code(func)
        functions.append(func)
    return functions


def expand_call_chain(
    function_name: str,
    direction: str,
    limit: int = 5
) -> Dict:
    """扩展调用链（统一接口）"""
    if direction == "callers":
        functions = get_callers(function_name, limit)
    else:
        functions = get_callees(function_name, limit)
    
    return {
        "functions": functions,
        "direction": direction,
        "source": function_name
    }
