#!/usr/bin/env python3
"""
并行代码全量摄取：多 clangd 实例分片处理，统一写入 Neo4j。

用法：
    python scripts/ingestion/ingest_parallel.py [--workers N]

策略：
1. 将文件列表分成 N 份
2. 启动 N 个进程，每个进程独立启动 clangd，处理自己的文件子集
3. 主进程收集所有 FileResult，统一执行 incomingCalls + resolver + graph_builder + neo4j_writer

注意：
- 每个 worker 使用独立的 clangd 缓存目录（XDG_CACHE_HOME），避免磁盘竞争
- incomingCalls 仍需单实例 LSPClient，因为需要全局 outgoingCalls 统计
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
import time
from multiprocessing import Pool
from pathlib import Path

# scripts/ingestion/ingest_parallel.py -> parent.parent.parent = Code_Graph/
_CODE_GRAPH = Path(__file__).resolve().parent.parent.parent
if str(_CODE_GRAPH) not in sys.path:
    sys.path.insert(0, str(_CODE_GRAPH))

from config import get_compile_commands_path, get_repo_root, NEO4J_DATABASE
from src.neo4j_writer import get_driver, get_head_commit, update_repository_commit
from src.ingestion.neo4j_writer import ensure_constraints, clear_code_graph
from src.ingestion.orchestrator import run_extraction_only, run_pipeline_from_results
from src.ingestion.source_collector import collect_source_files

logger = logging.getLogger("ingest_parallel")


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _worker_init(cache_dir: str) -> None:
    """Worker 初始化：设置独立的 clangd 缓存目录。"""
    os.environ["XDG_CACHE_HOME"] = cache_dir


def _worker_extract(args) -> list:
    """
    子进程工作函数：启动 clangd，提取符号，返回 FileResult 列表。

    Args:
        args: (repo_root, compile_commands_dir, file_subset, worker_id, cache_dir)
    """
    repo_root, compile_commands_dir, file_subset, worker_id, cache_dir = args
    _worker_init(cache_dir)

    worker_logger = logging.getLogger(f"worker-{worker_id}")
    worker_logger.info("Worker %d starting with %d files", worker_id, len(file_subset))
    t0 = time.perf_counter()

    try:
        results = run_extraction_only(
            repo_root=Path(repo_root),
            compile_commands_dir=Path(compile_commands_dir),
            files=file_subset,
            collect_calls=True,
            collect_var_refs=True,
        )
        elapsed = time.perf_counter() - t0
        worker_logger.info(
            "Worker %d done in %.1fs: %d file results",
            worker_id, elapsed, len(results),
        )
        return results
    except Exception as e:
        worker_logger.error("Worker %d failed: %s", worker_id, e)
        raise


def main() -> int:
    setup_logging()

    parser = argparse.ArgumentParser(description="Parallel code ingestion")
    parser.add_argument(
        "--workers", type=int, default=0,
        help="并行 worker 数量（默认 auto：min(8, CPU_COUNT, file_count//10)）",
    )
    parser.add_argument(
        "--include-dirs", nargs="+", default=None,
        help="只处理指定顶层目录下的文件",
    )
    args = parser.parse_args()

    build_dir = get_compile_commands_path()
    repo_root = get_repo_root()
    if not build_dir or not repo_root:
        logger.error("Config missing: build_dir=%s repo_root=%s", build_dir, repo_root)
        return 1

    driver = get_driver()
    try:
        driver.verify_connectivity()
    except Exception as e:
        logger.error("Neo4j connection failed: %s", e)
        return 1

    # 收集文件
    files = collect_source_files(build_dir, repo_root, include_dirs=args.include_dirs)
    if not files:
        logger.error("No source files found")
        return 1

    n_files = len(files)
    cpu_count = os.cpu_count() or 4
    if args.workers <= 0:
        # 自动：最多 8 个，至少 10 文件/worker，不超过 CPU 核心数
        n_workers = min(8, cpu_count, max(1, n_files // 10))
    else:
        n_workers = min(args.workers, n_files)

    logger.info(
        "Parallel ingestion: %d files, %d workers (cpu=%d)",
        n_files, n_workers, cpu_count,
    )

    # 分片：按数量均匀分配（非轮询，保证每个 worker 的文件数量相近）
    chunk_size = max(1, n_files // n_workers)
    chunks = []
    start = 0
    for i in range(n_workers):
        if i == n_workers - 1:
            end = n_files
        else:
            end = start + chunk_size
        chunks.append(files[start:end])
        start = end
    # 过滤空 chunk（可能因整除不均）
    chunks = [c for c in chunks if c]
    n_workers = len(chunks)

    # 为每个 worker 创建独立缓存目录
    cache_dirs = [
        tempfile.mkdtemp(prefix=f"clangd_cache_w{i}_")
        for i in range(n_workers)
    ]

    # 并行提取
    t0 = time.perf_counter()
    args_list = [
        (str(repo_root), str(build_dir), chunk, i, cache_dirs[i])
        for i, chunk in enumerate(chunks)
    ]

    all_file_results = []
    try:
        with Pool(processes=n_workers) as pool:
            for results in pool.imap_unordered(_worker_extract, args_list):
                all_file_results.extend(results)
                logger.info(
                    "Collected %d/%d file results so far...",
                    len(all_file_results), n_files,
                )
    finally:
        # 清理缓存目录
        for d in cache_dirs:
            try:
                import shutil
                shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass

    extract_elapsed = time.perf_counter() - t0
    logger.info(
        "All workers done in %.1fs: %d file results total",
        extract_elapsed, len(all_file_results),
    )

    # 统一跑后续阶段
    stats = run_pipeline_from_results(
        file_results=all_file_results,
        repo_root=Path(repo_root),
        compile_commands_dir=Path(build_dir),
        driver=driver,
        database=NEO4J_DATABASE,
        collect_calls=True,
        collect_var_refs=True,
        clear_existing=True,
        extract_elapsed=extract_elapsed,
    )

    # 更新 commit
    driver2 = get_driver()
    try:
        sha = get_head_commit(repo_root)
        if sha:
            with driver2.session(database=NEO4J_DATABASE) as session:
                result = session.run("MATCH (r:Repository) RETURN r.id AS id LIMIT 1")
                record = result.single()
                if record:
                    update_repository_commit(driver2, record["id"], sha, NEO4J_DATABASE)
                    logger.info("Updated last_processed_commit = %s", sha[:8])
    finally:
        driver2.close()

    logger.info(
        "=== Parallel Ingestion Complete ===\n"
        "  Files: %(files)d\n"
        "  Functions: %(functions)d\n"
        "  Classes: %(classes)d\n"
        "  Variables: %(variables)d\n"
        "  Attributes: %(attributes)d\n"
        "  Calls: %(calls)d\n"
        "  Ambiguous: %(ambiguous)d\n"
        "  Unresolved: %(unresolved)d\n"
        "  External: %(external_calls)d\n"
        "  ControlFlow: %(control_flow)d\n"
        "  ResourceOps: %(resource_ops)d\n"
        "  Total Elapsed: %(elapsed_total).1fs",
        stats,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
