"""Neo4j数据库客户端 - 统一的数据库访问层"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from neo4j_writer import get_driver
from config import NEO4J_DATABASE

# 全局驱动实例（懒加载）
_driver = None

def get_neo4j_driver():
    """获取Neo4j驱动实例（单例模式）"""
    global _driver
    if _driver is None:
        _driver = get_driver()
    return _driver

def close_neo4j_driver():
    """关闭Neo4j驱动"""
    global _driver
    if _driver:
        _driver.close()
        _driver = None

def run_cypher(cypher: str, params: dict = None) -> list[dict]:
    """
    执行Cypher查询并返回结果列表
    
    Args:
        cypher: Cypher查询语句
        params: 查询参数
        
    Returns:
        查询结果列表
    """
    driver = get_neo4j_driver()
    with driver.session(database=NEO4J_DATABASE) as session:
        result = session.run(cypher, params or {})
        return [dict(record) for record in result]

def run_cypher_single(cypher: str, params: dict = None) -> dict | None:
    """
    执行Cypher查询并返回单条结果
    
    Args:
        cypher: Cypher查询语句
        params: 查询参数
        
    Returns:
        单条结果或None
    """
    results = run_cypher(cypher, params)
    return results[0] if results else None
