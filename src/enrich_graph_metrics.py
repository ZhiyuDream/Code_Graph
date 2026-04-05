#!/usr/bin/env python3
"""
为 Function 节点批量写入结构指标：fan_in, fan_out, is_leaf, is_orphan。
纯 Cypher 计算，无 LLM 调用。

用法：
  python enrich_graph_metrics.py
"""
from __future__ import annotations

from config import NEO4J_DATABASE
from neo4j_writer import get_driver


def enrich(driver, database: str = NEO4J_DATABASE):
    with driver.session(database=database) as s:
        # fan_out: 该函数调用了多少个其他函数
        r = s.run("""
            MATCH (f:Function)
            OPTIONAL MATCH (f)-[:CALLS]->(callee:Function)
            WITH f, count(callee) AS out
            SET f.fan_out = out
            RETURN count(f) AS updated
        """)
        print(f"fan_out 已写入 {r.single()['updated']} 个节点")

        # fan_in: 该函数被多少个其他函数调用
        r = s.run("""
            MATCH (f:Function)
            OPTIONAL MATCH (caller:Function)-[:CALLS]->(f)
            WITH f, count(caller) AS inp
            SET f.fan_in = inp
            RETURN count(f) AS updated
        """)
        print(f"fan_in  已写入 {r.single()['updated']} 个节点")

        # is_leaf: 不调用任何函数
        r = s.run("""
            MATCH (f:Function)
            SET f.is_leaf = (f.fan_out = 0)
            RETURN count(f) AS updated
        """)
        print(f"is_leaf 已写入 {r.single()['updated']} 个节点")

        # is_orphan: 既无 caller 也无 callee（孤立节点）
        r = s.run("""
            MATCH (f:Function)
            SET f.is_orphan = (f.fan_in = 0 AND f.fan_out = 0)
            RETURN count(f) AS updated
        """)
        print(f"is_orphan 已写入 {r.single()['updated']} 个节点")

        # 统计摘要
        r = s.run("""
            MATCH (f:Function)
            RETURN count(f) AS total,
                   sum(CASE WHEN f.is_leaf THEN 1 ELSE 0 END) AS leaves,
                   sum(CASE WHEN f.is_orphan THEN 1 ELSE 0 END) AS orphans,
                   avg(f.fan_in) AS avg_fan_in,
                   avg(f.fan_out) AS avg_fan_out,
                   max(f.fan_in) AS max_fan_in,
                   max(f.fan_out) AS max_fan_out
        """)
        row = r.single()
        print(f"\n--- 统计 ---")
        print(f"函数总数: {row['total']}")
        print(f"叶子节点: {row['leaves']} ({100*row['leaves']/max(row['total'],1):.1f}%)")
        print(f"孤立节点: {row['orphans']} ({100*row['orphans']/max(row['total'],1):.1f}%)")
        print(f"fan_in  平均={row['avg_fan_in']:.2f}  最大={row['max_fan_in']}")
        print(f"fan_out 平均={row['avg_fan_out']:.2f}  最大={row['max_fan_out']}")

        # Top 10 高 fan_in（被调用最多，改动风险最大）
        r = s.run("""
            MATCH (f:Function)
            WHERE f.fan_in > 0
            RETURN f.name AS name, f.fan_in AS fan_in, coalesce(f.file_path,'') AS file
            ORDER BY f.fan_in DESC LIMIT 10
        """)
        print(f"\nTop 10 高 fan_in（改动风险大）:")
        for rec in r:
            print(f"  {rec['fan_in']:>4}  {rec['name']}  ({rec['file']})")

        # Top 10 高 fan_out（逻辑复杂）
        r = s.run("""
            MATCH (f:Function)
            WHERE f.fan_out > 0
            RETURN f.name AS name, f.fan_out AS fan_out, coalesce(f.file_path,'') AS file
            ORDER BY f.fan_out DESC LIMIT 10
        """)
        print(f"\nTop 10 高 fan_out（逻辑复杂）:")
        for rec in r:
            print(f"  {rec['fan_out']:>4}  {rec['name']}  ({rec['file']})")


if __name__ == "__main__":
    driver = get_driver()
    try:
        driver.verify_connectivity()
        enrich(driver)
    finally:
        driver.close()
