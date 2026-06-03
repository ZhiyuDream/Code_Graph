"""
代码增量摄取：基于 git diff 只更新变更的文件。

异常处理策略（与全量一致）：
- 单个文件解析失败：记录 warning，跳过，继续处理其余文件
- 阶段级错误：中断 pipeline
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

from .call_resolver import resolve_all_calls
from .field_resolver import enrich_file_results
from .graph_builder import assemble_graph
from .lsp_client import LSPClient
from .models import FileResult, FunctionSymbol, RawCall
from .neo4j_writer import ensure_constraints, write_graph
from .symbol_extractor import process_file

logger = logging.getLogger(__name__)

SOURCE_EXTS = {".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hh", ".hxx"}


def get_changed_files(repo_root: Path, since_commit: str) -> set[str]:
    """获取自 since_commit 以来变更的文件（含修改、新增、重命名）。"""
    if not since_commit:
        return set()
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=AMR", f"{since_commit}..HEAD"],
            cwd=repo_root, capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return set()
        return {line.strip() for line in result.stdout.strip().split("\n") if line.strip()}
    except Exception as e:
        logger.warning("Failed to get changed files: %s", e)
        return set()


def get_deleted_files(repo_root: Path, since_commit: str) -> set[str]:
    """获取自 since_commit 以来删除的文件。"""
    if not since_commit:
        return set()
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=D", f"{since_commit}..HEAD"],
            cwd=repo_root, capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return set()
        return {line.strip() for line in result.stdout.strip().split("\n") if line.strip()}
    except Exception as e:
        logger.warning("Failed to get deleted files: %s", e)
        return set()


def _find_affected_by_headers(repo_root: Path, changed_sources: set[str]) -> set[str]:
    """
    如果变更中包含头文件，找到所有包含该头文件的编译单元。

    策略：用 git grep 查找 #include 该头文件的 .cpp/.c/.cc 文件。
    这确保头文件变更时，依赖它的编译单元也会被重新解析。
    """
    affected: set[str] = set()
    headers = [f for f in changed_sources if f.endswith((".h", ".hpp", ".hh", ".hxx"))]
    if not headers:
        return affected

    for h in headers:
        h_name = Path(h).name
        try:
            result = subprocess.run(
                ["git", "grep", "-l", f"#include.*{h_name}", "--", "*.cpp", "*.c", "*.cc", "*.cxx"],
                cwd=repo_root, capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    if line.strip():
                        affected.add(line.strip())
        except Exception as e:
            logger.warning("Failed to find affected files for %s: %s", h, e)

    return affected


def delete_file_nodes(driver: Any, file_path: str, database: str):
    """删除指定 file_path 相关的所有节点和边。

    使用单条 Cypher 批量删除，减少事务边界。
    """
    with driver.session(database=database) as session:
        # 先删除关联的特殊节点
        session.run(
            """
            MATCH (f:Function {file_path: $fp})
            OPTIONAL MATCH (f)<-[:CONTROL_FLOW]-(cf:ControlFlowBlock)
            OPTIONAL MATCH (f)<-[:MANAGES]-(ro:ResourceOperation)
            OPTIONAL MATCH (f)<-[:EXTERNAL_CALLS]-(ec:ExternalCall)
            OPTIONAL MATCH (f)<-[:CALLS_AMBIGUOUS]-(ac:AmbiguousCall)
            WITH collect(DISTINCT cf) + collect(DISTINCT ro) + collect(DISTINCT ec) + collect(DISTINCT ac) AS to_delete
            UNWIND to_delete AS d
            DETACH DELETE d
            """,
            fp=file_path,
        )
        # 删除主体节点
        session.run(
            """
            MATCH (f:Function {file_path: $fp}) DETACH DELETE f
            """,
            fp=file_path,
        )
        session.run("MATCH (c:Class {file_path: $fp}) DETACH DELETE c", fp=file_path)
        session.run("MATCH (v:Variable {file_path: $fp}) DETACH DELETE v", fp=file_path)
        session.run("MATCH (a:Attribute {file_path: $fp}) DETACH DELETE a", fp=file_path)
        session.run("MATCH (fi:File {path: $fp}) DETACH DELETE fi", fp=file_path)


def run_incremental_pipeline(
    repo_root: Path,
    compile_commands_dir: Path,
    driver: Any,
    database: str = "neo4j",
) -> dict[str, Any]:
    """
    执行增量更新 pipeline。

    Returns:
        统计信息 dict
    """
    from src.neo4j_writer import get_head_commit, update_repository_commit

    # 1. 检查是否需要更新
    current_commit = get_head_commit(repo_root)
    if not current_commit:
        raise RuntimeError("Cannot get current commit")

    with driver.session(database=database) as session:
        result = session.run("MATCH (r:Repository) RETURN r.last_processed_commit AS commit LIMIT 1")
        record = result.single()
        last_commit = record["commit"] if record else ""

    if not last_commit:
        raise RuntimeError("No last_processed_commit found, run full pipeline first")

    if last_commit == current_commit:
        logger.info("Already up to date (%s)", current_commit[:8])
        return {"updated": False}

    # 2. 获取变更文件
    changed_files = get_changed_files(repo_root, last_commit)
    deleted_files = get_deleted_files(repo_root, last_commit)

    changed_sources = {f for f in changed_files if Path(f).suffix.lower() in SOURCE_EXTS}
    deleted_sources = {f for f in deleted_files if Path(f).suffix.lower() in SOURCE_EXTS}

    # 头文件级联：找到受影响的其他文件
    affected = _find_affected_by_headers(repo_root, changed_sources)
    if affected:
        logger.info("Header changes affect %d additional files: %s",
                    len(affected), ", ".join(sorted(affected)[:5]) + ("..." if len(affected) > 5 else ""))
        changed_sources |= affected

    logger.info("Changed files: %d, Deleted files: %d", len(changed_sources), len(deleted_sources))

    if not changed_sources and not deleted_sources:
        logger.info("No source file changes")
        with driver.session(database=database) as session:
            result = session.run("MATCH (r:Repository) RETURN r.id AS id LIMIT 1")
            record = result.single()
            if record:
                update_repository_commit(driver, record["id"], current_commit, database)
        return {"updated": True, "files_changed": 0}

    # 3. 删除旧节点
    for fp in changed_sources | deleted_sources:
        try:
            delete_file_nodes(driver, fp, database)
            logger.debug("Deleted old nodes for %s", fp)
        except Exception as e:
            logger.warning("Failed to delete old nodes for %s: %s", fp, e)

    # 4. 重新解析变更文件
    if changed_sources:
        logger.info("Re-parsing %d changed files...", len(changed_sources))
        with LSPClient.start(repo_root, compile_commands_dir) as client:
            file_results: list[FileResult] = []
            for fp in sorted(changed_sources):
                abs_path = repo_root / fp
                try:
                    result = process_file(
                        client.request,
                        str(abs_path),
                        fp,
                        repo_root=repo_root,
                        collect_calls=True,
                        collect_var_refs=True,
                        delay_between_calls=0.02,
                    )
                    file_results.append(result)
                except Exception as e:
                    logger.warning("Failed to parse %s: %s", fp, e)

            if not file_results:
                logger.warning("No files successfully parsed in incremental update")
            else:
                # 解析字段和调用关系
                file_results = enrich_file_results(file_results)
                all_functions: list[FunctionSymbol] = []
                all_raw_calls: list[RawCall] = []
                all_var_refs: list[tuple[str, str, int]] = []
                for fr in file_results:
                    all_functions.extend(fr.functions)
                    all_raw_calls.extend(fr.calls)
                    all_var_refs.extend(fr.raw.get("var_refs_global", []))

                resolved = resolve_all_calls(
                    all_functions, all_raw_calls, repo_root=repo_root
                )
                graph = assemble_graph(
                    file_results,
                    resolved,
                    repo_root=str(repo_root),
                    var_refs_global=all_var_refs,
                )
                ensure_constraints(driver, database)
                write_graph(driver, graph, database)
                logger.info("Incremental graph written: %d functions, %d calls",
                            len(graph["nodes"].get("Function", [])),
                            len(graph["edges"].get("CALLS", [])))

    # 5. 更新 commit
    with driver.session(database=database) as session:
        result = session.run("MATCH (r:Repository) RETURN r.id AS id LIMIT 1")
        record = result.single()
        if record:
            update_repository_commit(driver, record["id"], current_commit, database)
            logger.info("Updated last_processed_commit to %s", current_commit[:8])

    return {
        "updated": True,
        "files_changed": len(changed_sources),
        "files_deleted": len(deleted_sources),
    }
