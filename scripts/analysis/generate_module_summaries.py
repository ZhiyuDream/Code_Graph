#!/usr/bin/env python3
from __future__ import annotations

"""
为 Neo4j 中的 Directory 节点生成模块级摘要（Module Summary）。

用法：
  python generate_module_summaries.py [--dry-run] [--concurrency N] [--limit N]

流程：
  1. 从 Neo4j 读取每个 Directory 下的 Function 列表
  2. 用 LLM 生成一句话功能摘要
  3. 写回 Directory.summary 字段
  4. 建立 Directory-[:PART_OF_MODULE]->Function 边
"""

import argparse
import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional

# 确保能导入 src/
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

from config import LLM_MODEL, NEO4J_DATABASE, OPENAI_API_KEY, OPENAI_BASE_URL
from neo4j_writer import get_driver

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("module_summary")

# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------


def call_llm(prompt: str) -> Optional[dict[str, Any]]:
    if not OPENAI_API_KEY:
        logger.error("OPENAI_API_KEY not set")
        return None
    from openai import OpenAI

    client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)
    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
            temperature=0.0,
        )
    except Exception as e:
        logger.warning("LLM call failed: %s", e)
        return None

    if not resp.choices or not resp.choices[0].message.content:
        return None
    text = resp.choices[0].message.content.strip()
    # 尝试解析 JSON，失败则把整个文本当作 summary
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"summary": text}


# ---------------------------------------------------------------------------
# Neo4j queries
# ---------------------------------------------------------------------------


def fetch_directories_with_functions(driver, database: str, limit: int | None = None):
    """
    返回 [(dir_path, [func_name, ...]), ...]
    只返回至少有一个 Function 的 Directory。
    """
    cypher = """
    MATCH (d:Directory)<-[:CONTAINS*]-(f:Function)
    RETURN d.path AS dir, collect(DISTINCT f.name) AS funcs
    """
    if limit:
        cypher += f" LIMIT {limit}"

    results = []
    with driver.session(database=database) as session:
        for record in session.run(cypher):
            dir_path = record["dir"]
            funcs = record["funcs"]
            if funcs:
                results.append((dir_path, funcs))
    return results


def write_module_summary(driver, database: str, dir_path: str, summary: str, dry_run: bool = False):
    if dry_run:
        logger.info("[DRY-RUN] %s -> %s", dir_path, summary)
        return
    with driver.session(database=database) as session:
        session.run(
            """
            MATCH (d:Directory {path: $dir})
            SET d.summary = $summary, d.is_module = true
            """,
            dir=dir_path,
            summary=summary,
        )


def create_part_of_module_edges(driver, database: str, dir_path: str, dry_run: bool = False):
    if dry_run:
        logger.debug("[DRY-RUN] CREATE PART_OF_MODULE edges for %s", dir_path)
        return
    with driver.session(database=database) as session:
        session.run(
            """
            MATCH (d:Directory {path: $dir})<-[:CONTAINS*]-(f:Function)
            MERGE (d)-[:PART_OF_MODULE]->(f)
            """,
            dir=dir_path,
        )


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


def build_prompt(dir_path: str, func_names: list[str]) -> str:
    # 截断函数列表，避免 prompt 过长
    displayed = func_names[:50]
    funcs_text = "\n".join(f"  - {n}" for n in displayed)
    if len(func_names) > 50:
        funcs_text += f"\n  ... and {len(func_names) - 50} more"

    prompt = f"""You are analyzing a C++ code repository.

Directory: {dir_path}
Functions in this directory ({len(func_names)} total):
{funcs_text}

Summarize the primary responsibility of this directory in 1-2 sentences.
Do NOT list individual functions. Focus on the functional theme.
Keep it under 50 words.

Output JSON: {{"summary": "..."}}
"""
    return prompt


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def process_directory(dir_path: str, func_names: list[str], dry_run: bool = False) -> tuple[str, Optional[str]]:
    prompt = build_prompt(dir_path, func_names)
    result = call_llm(prompt)
    summary = result.get("summary", "") if result else None
    if not summary:
        logger.warning("No summary for %s", dir_path)
    return dir_path, summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate module summaries for Directory nodes")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, do not write to Neo4j")
    parser.add_argument("--concurrency", type=int, default=8, help="LLM concurrency")
    parser.add_argument("--limit", type=int, default=None, help="Max directories to process")
    parser.add_argument("--skip-llm", action="store_true", help="Skip LLM, only create PART_OF_MODULE edges")
    args = parser.parse_args()

    driver = get_driver()
    try:
        driver.verify_connectivity()
    except Exception as e:
        logger.error("Neo4j connection failed: %s", e)
        return 1

    logger.info("Fetching directories with functions...")
    dirs = fetch_directories_with_functions(driver, NEO4J_DATABASE, limit=args.limit)
    logger.info("Found %d directories with functions", len(dirs))

    if not dirs:
        logger.warning("No directories found")
        return 0

    # 1. 生成摘要
    if not args.skip_llm:
        logger.info("Generating summaries with LLM (concurrency=%d)...", args.concurrency)
        processed = 0
        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            futures = {
                executor.submit(process_directory, dp, fn, args.dry_run): dp
                for dp, fn in dirs
            }
            for fut in as_completed(futures):
                dir_path, summary = fut.result()
                if summary:
                    write_module_summary(driver, NEO4J_DATABASE, dir_path, summary, dry_run=args.dry_run)
                    processed += 1
        logger.info("Summaries written: %d/%d", processed, len(dirs))

    # 2. 建立 PART_OF_MODULE 边
    logger.info("Creating PART_OF_MODULE edges...")
    for dp, _ in dirs:
        create_part_of_module_edges(driver, NEO4J_DATABASE, dp, dry_run=args.dry_run)
    logger.info("PART_OF_MODULE edges created")

    driver.close()
    logger.info("Done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
