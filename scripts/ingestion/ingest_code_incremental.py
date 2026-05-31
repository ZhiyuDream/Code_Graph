#!/usr/bin/env python3
from __future__ import annotations

"""
代码增量摄取：基于 git diff 只更新变更的文件。

修复现有增量更新器的问题：
1. parse_files_incremental 缺失
2. 重复导入
3. 头文件变更未处理（过滤后缀包含 .h/.hpp）
"""

import argparse
import logging
import sys
from pathlib import Path

_CODE_GRAPH = Path(__file__).resolve().parent.parent
if str(_CODE_GRAPH) not in sys.path:
    sys.path.insert(0, str(_CODE_GRAPH))

from config import get_compile_commands_path, get_repo_root, NEO4J_DATABASE
from src.neo4j_writer import get_driver
from src.ingestion.incremental import run_incremental_pipeline


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def main() -> int:
    setup_logging()
    logger = logging.getLogger("ingest_code_incremental")

    parser = argparse.ArgumentParser(description="增量更新代码图 v2")
    parser.add_argument("--check", action="store_true", help="只检查是否需要更新")
    args = parser.parse_args()

    build_dir = get_compile_commands_path()
    if not build_dir:
        logger.error("compile_commands.json not found")
        return 1

    repo_root = get_repo_root()
    if not repo_root:
        logger.error("REPO_ROOT not set")
        return 1

    driver = get_driver()
    try:
        driver.verify_connectivity()
    except Exception as e:
        logger.error("Neo4j connection failed: %s", e)
        return 1

    if args.check:
        from src.ingestion.incremental import get_changed_files
        from src.neo4j_writer import get_head_commit

        current_commit = get_head_commit(repo_root)
        with driver.session(database=NEO4J_DATABASE) as session:
            result = session.run("MATCH (r:Repository) RETURN r.last_processed_commit AS commit LIMIT 1")
            record = result.single()
            last_commit = record["commit"] if record else ""

        if last_commit == current_commit:
            print("No update needed")
        else:
            changed = get_changed_files(repo_root, last_commit)
            print(f"Update needed: {last_commit[:8] if last_commit else 'N/A'} -> {current_commit[:8]}")
            print(f"Changed files: {len(changed)}")
            for f in sorted(changed)[:20]:
                print(f"  {f}")
            if len(changed) > 20:
                print(f"  ... and {len(changed) - 20} more")
        driver.close()
        return 0

    try:
        stats = run_incremental_pipeline(
            repo_root=repo_root,
            compile_commands_dir=build_dir,
            driver=driver,
            database=NEO4J_DATABASE,
        )
    except Exception as e:
        logger.error("Incremental update failed: %s", e)
        return 1
    finally:
        driver.close()

    if not stats.get("updated"):
        logger.info("No update performed")
        return 0

    logger.info(
        "=== Incremental Update v2 Complete ===\n"
        "  Files changed: %(files_changed)d\n"
        "  Files deleted: %(files_deleted)d",
        stats,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
