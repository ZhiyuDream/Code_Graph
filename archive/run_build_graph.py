#!/usr/bin/env python3
"""
代码图构建（单脚本）：一次性完成符号提取 + 图组装 + Neo4j 写入。

替代原 run_stage1.py。

用法:
    python scripts/run_build_graph.py
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

_CODE_GRAPH = Path(__file__).resolve().parent.parent
if str(_CODE_GRAPH) not in sys.path:
    sys.path.insert(0, str(_CODE_GRAPH))

from config import get_compile_commands_path, get_repo_root, NEO4J_DATABASE
from src.neo4j_writer import get_driver, get_head_commit, update_repository_commit
from src.pipeline.neo4j_batch_writer import ensure_constraints, clear_code_graph
from src.pipeline.stage1_clangd import run_full_pipeline


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def main() -> int:
    setup_logging()
    logger = logging.getLogger("run_build_graph")

    build_dir = get_compile_commands_path()
    if not build_dir:
        logger.error("compile_commands.json not found. Set REPO_ROOT or COMPILE_COMMANDS_DIR.")
        return 1

    repo_root = get_repo_root()
    if not repo_root:
        logger.error("REPO_ROOT not set.")
        return 1

    driver = get_driver()
    try:
        driver.verify_connectivity()
    except Exception as e:
        logger.error("Neo4j connection failed: %s", e)
        return 1

    logger.info("=" * 60)
    logger.info("Code Graph Builder")
    logger.info("Repo: %s", repo_root)
    logger.info("=" * 60)

    try:
        stats = run_full_pipeline(
            repo_root=repo_root,
            compile_commands_dir=build_dir,
            driver=driver,
            database=NEO4J_DATABASE,
            collect_calls=True,
            collect_var_refs=True,
            include_dirs=None,
        )
    except Exception as e:
        logger.error("Pipeline failed: %s", e, exc_info=True)
        driver.close()
        return 1
    finally:
        driver.close()

    # 更新 commit
    driver = get_driver()
    try:
        sha = get_head_commit(repo_root)
        if sha:
            with driver.session(database=NEO4J_DATABASE) as session:
                result = session.run("MATCH (r:Repository) RETURN r.id AS id LIMIT 1")
                record = result.single()
                if record:
                    update_repository_commit(driver, record["id"], sha, NEO4J_DATABASE)
                    logger.info("Updated last_processed_commit = %s", sha[:8])
    finally:
        driver.close()

    logger.info("=" * 60)
    logger.info("Build Complete")
    logger.info("  Files:        %(files)d", stats)
    logger.info("  Functions:    %(functions)d", stats)
    logger.info("  Classes:      %(classes)d", stats)
    logger.info("  Variables:    %(variables)d", stats)
    logger.info("  Attributes:   %(attributes)d", stats)
    logger.info("  CALLS:        %(calls)d", stats)
    logger.info("  AMBIGUOUS:    %(ambiguous)d", stats)
    logger.info("  UNRESOLVED:   %(unresolved)d", stats)
    logger.info("  EXTERNAL:     %(external_calls)d", stats)
    logger.info("  CONTROL_FLOW: %(control_flow)d", stats)
    logger.info("  Elapsed:      %(elapsed_total).1fs", stats)
    logger.info("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
