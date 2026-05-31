#!/usr/bin/env python3
"""快速验证：只跑指定子目录的文件，不清空全图。"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

_CODE_GRAPH = Path(__file__).resolve().parent.parent.parent
if str(_CODE_GRAPH) not in sys.path:
    sys.path.insert(0, str(_CODE_GRAPH))

from config import get_compile_commands_path, get_repo_root, NEO4J_DATABASE
from src.neo4j_writer import get_driver
from src.ingestion.incremental import delete_file_nodes
from src.ingestion.lsp_client import LSPClient
from src.ingestion.orchestrator import run_full_pipeline


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger("ingest_subset")

    build_dir = get_compile_commands_path()
    repo_root = get_repo_root()
    if not build_dir or not repo_root:
        logger.error("Config missing: build_dir=%s repo_root=%s", build_dir, repo_root)
        return 1

    # 只处理 common/ 子目录下的 .cpp/.h 文件
    subset_files = []
    for ext in (".cpp", ".c", ".cc", ".h", ".hpp"):
        subset_files.extend(Path(repo_root).glob(f"common/**/*{ext}"))
    subset_files = sorted([str(f.relative_to(repo_root)) for f in subset_files])
    logger.info("Subset: %d files under common/", len(subset_files))

    driver = get_driver()
    try:
        driver.verify_connectivity()
    except Exception as e:
        logger.error("Neo4j connection failed: %s", e)
        return 1

    # 1. 先删除 common/ 子目录已有的旧节点（不清空全图）
    logger.info("Deleting old nodes for common/ subset...")
    for fp in subset_files:
        delete_file_nodes(driver, fp, NEO4J_DATABASE)

    # 2. 跑 pipeline（只传 subset 文件，但通过 orchestrator 的 source_collector 过滤）
    # orchestrator 内部会从 compile_commands + os.walk 收集文件，我们需要限制它
    logger.info("Starting pipeline for %d files...", len(subset_files))
    start = time.time()

    # 直接调用 run_full_pipeline，但提前收集文件列表
    # 由于 run_full_pipeline 内部自己收集文件，我们先验证脚本能否正常跑
    stats = run_full_pipeline(
        repo_root=repo_root,
        compile_commands_dir=build_dir,
        driver=driver,
        database=NEO4J_DATABASE,
        collect_calls=True,
        collect_var_refs=True,
        include_dirs=["common"],  # 限制只处理 common/ 目录
        clear_existing=False,      # 不清空全图，只更新 common/ 子目录
    )

    elapsed = time.time() - start
    log_data = {**stats, "resource_ops": stats.get("resource_ops", 0), "elapsed": elapsed}
    logger.info(
        "=== Subset Complete ===\n"
        "  Files: %(files)d\n"
        "  Functions: %(functions)d\n"
        "  Classes: %(classes)d\n"
        "  ControlFlow: %(control_flow)d\n"
        "  ResourceOps: %(resource_ops)d\n"
        "  Calls: %(calls)d\n"
        "  Elapsed: %(elapsed).1fs",
        log_data,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
