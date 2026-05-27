import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT / "src"))   # 核心库
sys.path.insert(0, str(_ROOT))             # 根目录（config.py）

#!/usr/bin/env python3
"""
代码图分析工具：影响分析 + 异常函数检测。

用法：
  # 影响分析：改 ggml_abort 会影响哪些函数？
  python graph_analysis.py impact ggml_abort

  # 影响分析：指定深度
  python graph_analysis.py impact ggml_abort --depth 3

  # 异常检测：找出图中可疑函数
  python graph_analysis.py anomaly

  # 异常检测：自定义阈值
  python graph_analysis.py anomaly --fan-in-threshold 20 --fan-out-threshold 15
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from config import NEO4J_DATABASE
from neo4j_writer import get_driver

OUTPUT_DIR = Path(__file__).resolve().parent / "experiments"


# ── 影响分析 ──────────────────────────────────────────────

def impact_analysis(driver, func_name: str, database: str, max_depth: int = 3):
    """给定函数名，递归查找所有上游 caller，生成影响范围报告。"""
    with driver.session(database=database) as s:
        # 先确认目标函数存在
        r = s.run(
            "MATCH (f:Function) WHERE f.name = $name "
            "RETURN f.name AS name, coalesce(f.file_path,'') AS file, "
            "f.fan_in AS fan_in, f.fan_out AS fan_out",
            name=func_name,
        )
        target = r.single()
        if not target:
            print(f"函数 '{func_name}' 不存在于图中。")
            return None

        print(f"目标函数: {target['name']} ({target['file']})")
        print(f"  fan_in={target['fan_in']}  fan_out={target['fan_out']}")

        # BFS 逐层查 caller
        visited = {func_name}
        layers = []
        current_names = [func_name]

        for depth in range(1, max_depth + 1):
            r = s.run("""
                MATCH (caller:Function)-[:CALLS]->(callee:Function)
                WHERE callee.name IN $names AND NOT caller.name IN $visited
                RETURN DISTINCT caller.name AS name,
                       coalesce(caller.file_path,'') AS file,
                       callee.name AS calls_target,
                       coalesce(caller.fan_in, 0) AS fan_in,
                       coalesce(caller.fan_out, 0) AS fan_out
            """, names=current_names, visited=list(visited))

            records = list(r)
            if not records:
                break

            layer_funcs = []
            next_names = []
            for rec in records:
                layer_funcs.append({
                    "name": rec["name"],
                    "file": rec["file"],
                    "calls": rec["calls_target"],
                    "fan_in": rec["fan_in"],
                    "fan_out": rec["fan_out"],
                })
                visited.add(rec["name"])
                next_names.append(rec["name"])

            layers.append({"depth": depth, "functions": layer_funcs})
            current_names = next_names

        # 统计受影响的文件
        affected_files = set()
        total_affected = 0
        for layer in layers:
            for f in layer["functions"]:
                affected_files.add(f["file"])
                total_affected += 1

        print(f"\n影响范围（最大深度 {max_depth}）:")
        print(f"  受影响函数: {total_affected} 个")
        print(f"  受影响文件: {len(affected_files)} 个")

        for layer in layers:
            print(f"\n  第 {layer['depth']} 层（直接{'调用者' if layer['depth']==1 else '间接调用者'}）: {len(layer['functions'])} 个")
            for f in sorted(layer["functions"], key=lambda x: x["fan_in"], reverse=True)[:15]:
                print(f"    {f['name']} ({f['file']})  fan_in={f['fan_in']}")
            if len(layer["functions"]) > 15:
                print(f"    ... 还有 {len(layer['functions'])-15} 个")

        if affected_files:
            print(f"\n受影响文件列表:")
            for fp in sorted(affected_files):
                if fp:
                    print(f"    {fp}")

        return {
            "target": func_name,
            "target_file": target["file"],
            "max_depth": max_depth,
            "total_affected_functions": total_affected,
            "total_affected_files": len(affected_files),
            "affected_files": sorted(affected_files),
            "layers": layers,
        }


# ── 异常函数检测 ─────────────────────────────────────────

def anomaly_detection(driver, database: str,
                      fan_in_threshold: int = 15,
                      fan_out_threshold: int = 15):
    """检测图中的异常函数。"""
    results = {"anomalies": []}

    with driver.session(database=database) as s:
        # 1. 高 fan_out（逻辑过于复杂）
        r = s.run("""
            MATCH (f:Function)
            WHERE f.fan_out >= $threshold
            RETURN f.name AS name, coalesce(f.file_path,'') AS file,
                   f.fan_out AS fan_out, f.fan_in AS fan_in
            ORDER BY f.fan_out DESC
        """, threshold=fan_out_threshold)
        high_fan_out = [dict(rec) for rec in r]

        # 2. 高 fan_in（改动风险大）
        r = s.run("""
            MATCH (f:Function)
            WHERE f.fan_in >= $threshold
            RETURN f.name AS name, coalesce(f.file_path,'') AS file,
                   f.fan_in AS fan_in, f.fan_out AS fan_out
            ORDER BY f.fan_in DESC
        """, threshold=fan_in_threshold)
        high_fan_in = [dict(rec) for rec in r]

        # 3. 死代码：fan_in=0 且不是入口函数（排除 main/test）
        r = s.run("""
            MATCH (f:Function)
            WHERE f.fan_in = 0 AND f.fan_out > 0
              AND NOT f.name STARTS WITH 'main'
              AND NOT f.name STARTS WITH 'test_'
              AND NOT f.name STARTS WITH 'Test'
            RETURN f.name AS name, coalesce(f.file_path,'') AS file,
                   f.fan_out AS fan_out
            ORDER BY f.fan_out DESC
            LIMIT 30
        """)
        dead_entry = [dict(rec) for rec in r]

        # 4. 调用环检测（长度 2-4 的环）
        r = s.run("""
            MATCH (a:Function)-[:CALLS]->(b:Function)-[:CALLS]->(a)
            WHERE a.name < b.name
            RETURN a.name AS func_a, b.name AS func_b,
                   coalesce(a.file_path,'') AS file_a,
                   coalesce(b.file_path,'') AS file_b
            LIMIT 50
        """)
        cycles_2 = [dict(rec) for rec in r]

        r = s.run("""
            MATCH (a:Function)-[:CALLS]->(b:Function)-[:CALLS]->(c:Function)-[:CALLS]->(a)
            WHERE a.name < b.name AND b.name < c.name
            RETURN a.name AS func_a, b.name AS func_b, c.name AS func_c
            LIMIT 30
        """)
        cycles_3 = [dict(rec) for rec in r]

    # 输出
    print("=" * 60)
    print("异常函数检测报告")
    print("=" * 60)

    print(f"\n1. 高 fan_out（>= {fan_out_threshold}，逻辑复杂）: {len(high_fan_out)} 个")
    for f in high_fan_out[:10]:
        print(f"   fan_out={f['fan_out']:>3}  {f['name']} ({f['file']})")

    print(f"\n2. 高 fan_in（>= {fan_in_threshold}，改动风险大）: {len(high_fan_in)} 个")
    for f in high_fan_in[:10]:
        print(f"   fan_in={f['fan_in']:>3}  {f['name']} ({f['file']})")

    print(f"\n3. 疑似死代码入口（fan_in=0 但有调用其他函数）: {len(dead_entry)} 个")
    for f in dead_entry[:10]:
        print(f"   fan_out={f['fan_out']:>3}  {f['name']} ({f['file']})")

    print(f"\n4. 调用环:")
    print(f"   长度 2 的环: {len(cycles_2)} 个")
    for c in cycles_2[:5]:
        print(f"     {c['func_a']} <-> {c['func_b']}")
    print(f"   长度 3 的环: {len(cycles_3)} 个")
    for c in cycles_3[:5]:
        print(f"     {c['func_a']} -> {c['func_b']} -> {c['func_c']} -> ...")

    results = {
        "high_fan_out": high_fan_out,
        "high_fan_in": high_fan_in,
        "dead_entry_candidates": dead_entry,
        "cycles_length_2": cycles_2,
        "cycles_length_3": cycles_3,
    }
    return results


def main():
    parser = argparse.ArgumentParser(description="代码图分析工具")
    sub = parser.add_subparsers(dest="command")

    p_impact = sub.add_parser("impact", help="影响分析")
    p_impact.add_argument("function", help="目标函数名")
    p_impact.add_argument("--depth", type=int, default=3, help="最大追溯深度（默认 3）")
    p_impact.add_argument("--output", type=Path, default=None)

    p_anomaly = sub.add_parser("anomaly", help="异常函数检测")
    p_anomaly.add_argument("--fan-in-threshold", type=int, default=15)
    p_anomaly.add_argument("--fan-out-threshold", type=int, default=15)
    p_anomaly.add_argument("--output", type=Path, default=None)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    driver = get_driver()
    try:
        driver.verify_connectivity()

        if args.command == "impact":
            result = impact_analysis(driver, args.function, NEO4J_DATABASE, args.depth)
            out = args.output or OUTPUT_DIR / f"impact_{args.function}.json"
        else:
            result = anomaly_detection(
                driver, NEO4J_DATABASE,
                fan_in_threshold=args.fan_in_threshold,
                fan_out_threshold=args.fan_out_threshold,
            )
            out = args.output or OUTPUT_DIR / "anomaly_report.json"

        if result:
            out.parent.mkdir(parents=True, exist_ok=True)
            with open(out, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            print(f"\n报告已写入: {out}")
    finally:
        driver.close()


if __name__ == "__main__":
    main()
