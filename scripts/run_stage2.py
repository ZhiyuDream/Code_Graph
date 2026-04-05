#!/usr/bin/env python3
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT.parent))
"""
阶段 2：流程理解与 Workflow 入库。
从 Neo4j 中通过图结构（无 CALLS 入边的 Function）发现入口候选，沿 CALLS 展开得到调用子图，
创建 Workflow 节点及 WORKFLOW_ENTRY、PART_OF_WORKFLOW 关系写入 Neo4j。

前置：需先运行阶段 1（run_stage1.py 或 run_stage1_clangd.py）生成代码图。
入口不定死规则，后续可接入 agent（读文档、grep）再筛或增补。展开深度与节点上限见下方常量或环境变量。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_CODE_GRAPH = Path(__file__).resolve().parent
if str(_CODE_GRAPH) not in sys.path:
    sys.path.insert(0, str(_CODE_GRAPH))

from config import NEO4J_DATABASE
from entry_candidates import get_entry_candidates
from src.neo4j_writer import get_driver
from src.workflow_expand import expand_all_entries
from src.workflow_writer import clear_workflows, ensure_workflow_constraint, write_workflows

# 展开参数：深度上限、单 Workflow 节点数上限（可通过环境变量覆盖）
DEPTH_LIMIT = int(os.environ.get("WORKFLOW_DEPTH_LIMIT", "5"))
NODE_LIMIT = int(os.environ.get("WORKFLOW_NODE_LIMIT", "500"))


def main() -> int:
    print("阶段 2：流程理解与 Workflow 入库。")
    driver = get_driver()
    try:
        driver.verify_connectivity()
    except Exception as e:
        print(f"Neo4j 连接失败: {e}")
        return 1

    print("发现入口候选（图结构：无 CALLS 入边的 Function）…")
    candidates = get_entry_candidates(driver, database=NEO4J_DATABASE)
    print(f"  候选数: {len(candidates)}")
    if not candidates:
        print("未发现任何入口候选，请检查阶段 1 是否已跑。")
        driver.close()
        return 0

    print(f"沿 CALLS 展开（depth_limit={DEPTH_LIMIT}, node_limit={NODE_LIMIT}）…")
    subgraphs = expand_all_entries(
        driver,
        candidates,
        depth_limit=DEPTH_LIMIT,
        node_limit=NODE_LIMIT,
        database=NEO4J_DATABASE,
    )
    total_nodes = sum(len(s.get("function_ids") or []) for s in subgraphs)
    total_edges = sum(len(s.get("edges") or []) for s in subgraphs)
    print(f"  子图数: {len(subgraphs)}, 总参与节点: {total_nodes}, 总 CALLS 边: {total_edges}")

    ensure_workflow_constraint(driver, database=NEO4J_DATABASE)
    print("清空已有 Workflow…")
    clear_workflows(driver, database=NEO4J_DATABASE)
    print("写入 Workflow 与边…")
    write_workflows(
        driver,
        subgraphs,
        depth_limit=DEPTH_LIMIT,
        node_limit=NODE_LIMIT,
        database=NEO4J_DATABASE,
    )
    driver.close()
    print("阶段 2 完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
