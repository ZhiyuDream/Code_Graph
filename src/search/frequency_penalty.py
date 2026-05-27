"""
高频函数降权模块

基于 Neo4j CALLS 关系的入度（被调用次数）识别高频基础函数，
在检索时对这类函数降权，减少噪声干扰。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Dict, Any

# 缓存文件路径
_CACHE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "high_freq_funcs.json"

# 内存缓存
_HIGH_FREQ_SET: set[str] | None = None
_FREQ_MAP: dict[str, int] | None = None

# 默认阈值和降权系数
DEFAULT_THRESHOLD = 50
DEFAULT_PENALTY = 0.5


def _load_from_neo4j(threshold: int = DEFAULT_THRESHOLD) -> dict[str, int]:
    """从 Neo4j 查询高频函数（in-degree >= threshold）"""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from src.neo4j_writer import get_driver
    from config import NEO4J_DATABASE

    driver = get_driver()
    try:
        with driver.session(database=NEO4J_DATABASE) as s:
            r = s.run("""
                MATCH (f:Function)
                OPTIONAL MATCH (f)<-[:CALLS]-(caller:Function)
                WITH f.name AS name, count(caller) AS in_degree
                WHERE in_degree >= $threshold
                RETURN name, in_degree
                ORDER BY in_degree DESC
            """, threshold=threshold)
            return {rec["name"]: rec["in_degree"] for rec in r}
    finally:
        driver.close()


def get_high_frequency_funcs(threshold: int = DEFAULT_THRESHOLD, refresh: bool = False) -> dict[str, int]:
    """
    获取高频函数列表（name -> in_degree）
    
    Args:
        threshold: 入度阈值，默认 50
        refresh: 是否强制重新从 Neo4j 加载
    """
    global _FREQ_MAP

    if _FREQ_MAP is not None and not refresh:
        return {k: v for k, v in _FREQ_MAP.items() if v >= threshold}

    # 尝试从缓存文件加载
    if _CACHE_PATH.exists() and not refresh:
        with open(_CACHE_PATH, "r", encoding="utf-8") as f:
            cached = json.load(f)
        _FREQ_MAP = cached
        return {k: v for k, v in _FREQ_MAP.items() if v >= threshold}

    # 从 Neo4j 加载
    _FREQ_MAP = _load_from_neo4j(threshold=0)  # 加载全部，缓存备用

    # 保存缓存
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(_FREQ_MAP, f, ensure_ascii=False, indent=2)

    return {k: v for k, v in _FREQ_MAP.items() if v >= threshold}


def is_high_frequency(func_name: str, threshold: int = DEFAULT_THRESHOLD) -> bool:
    """判断函数名是否属于高频函数"""
    global _HIGH_FREQ_SET
    if _HIGH_FREQ_SET is None:
        funcs = get_high_frequency_funcs(threshold=threshold)
        _HIGH_FREQ_SET = set(funcs.keys())
    return func_name in _HIGH_FREQ_SET


def apply_penalty(results: List[Dict[str, Any]], penalty: float = DEFAULT_PENALTY, threshold: int = DEFAULT_THRESHOLD) -> List[Dict[str, Any]]:
    """
    对检索结果中的高频函数应用降权
    
    Args:
        results: 检索结果列表，每项需包含 'name' 和 'score' 字段
        penalty: 降权系数（0-1），默认 0.5（score 减半）
        threshold: 高频函数阈值
        
    Returns:
        降权后的结果列表（按 score 重新排序）
    """
    funcs = get_high_frequency_funcs(threshold=threshold)
    high_freq = set(funcs.keys())

    for item in results:
        name = item.get("name", "")
        if name in high_freq:
            original_score = item.get("score", 0)
            item["score"] = original_score * penalty
            item["penalty_applied"] = True
            item["original_score"] = original_score

    # 重新按 score 排序
    results.sort(key=lambda x: -x.get("score", 0))
    return results
