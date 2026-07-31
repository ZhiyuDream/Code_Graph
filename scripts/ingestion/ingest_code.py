#!/usr/bin/env python3
"""Tree-sitter-only full ingestion entrypoint."""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

_CODE_GRAPH = Path(__file__).resolve().parent.parent.parent
if str(_CODE_GRAPH) not in sys.path:
    sys.path.insert(0, str(_CODE_GRAPH))

from config import get_repo_root, NEO4J_DATABASE
from src.neo4j_writer import get_driver, get_head_commit, update_repository_commit
from src.ingestion.tree_sitter_pipeline import run_tree_sitter_pipeline


def setup_logging() -> None:
    log_dir = _CODE_GRAPH / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"ingestion_treesitter_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(log_path, mode="w", encoding="utf-8")],
    )
    print(f"Logging to: {log_path}")


def main() -> int:
    setup_logging()
    logger = logging.getLogger("ingest_code")
    repo_root = get_repo_root()
    if not repo_root:
        logger.error("REPO_ROOT not set.")
        return 1

    driver = get_driver()
    try:
        driver.verify_connectivity()
        stats = run_tree_sitter_pipeline(
            repo_root=repo_root,
            driver=driver,
            database=NEO4J_DATABASE,
            batch_size=5000,
            clear_existing=True,
        )
        sha = get_head_commit(repo_root)
        if sha:
            with driver.session(database=NEO4J_DATABASE) as session:
                record = session.run(
                    "MATCH (r:Repository) RETURN r.id AS id LIMIT 1"
                ).single()
                if record:
                    update_repository_commit(driver, record["id"], sha, NEO4J_DATABASE)
        logger.info(
            "Tree-sitter-only ingestion complete: files=%d functions=%d classes=%d calls_candidate=%d",
            stats.get("files_parsed", 0),
            stats.get("functions", 0),
            stats.get("classes", 0),
            stats.get("neo4j_calls_candidate_submitted", 0),
        )
        return 0
    except Exception as exc:
        logger.exception("Tree-sitter-only pipeline failed: %s", exc)
        return 1
    finally:
        driver.close()


if __name__ == "__main__":
    raise SystemExit(main())
