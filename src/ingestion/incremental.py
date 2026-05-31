"""
代码增量摄取：基于 git diff 只更新变更的文件。

修复现有 incremental_updater.py 的问题：
1. parse_files_incremental 缺失
2. 重复导入
3. 头文件变更未处理（过滤后缀未包含 .h/.hpp）
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
from .neo4j_writer import write_graph
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


def delete_file_nodes(driver: Any, file_path: str, database: str):
    """删除指定 file_path 相关的所有节点和边。

    补全逻辑：
    1. 删除 Function 关联的 ControlFlowBlock、ResourceOperation、ExternalCall、AmbiguousCall
    2. 删除 Function / Class / Variable / Attribute / File 节点（DETACH DELETE 自动清理关系）
    """
    with driver.session(database=database) as session:
        # 1. 获取该文件的所有 Function ID
        result = session.run(
            "MATCH (f:Function {file_path: $fp}) RETURN f.id AS id", fp=file_path
        )
        func_ids = [r["id"] for r in result]

        if func_ids:
            # 2. 删除与这些 Function 关联的 ControlFlowBlock（通过 CONTROL_FLOW 边）
            session.run(
                "MATCH (cf:ControlFlowBlock)<-[:CONTROL_FLOW]-(f:Function) "
                "WHERE f.id IN $ids DETACH DELETE cf",
                ids=func_ids,
            )

            # 3. 删除与这些 Function 关联的 ResourceOperation（通过 MANAGES 边）
            session.run(
                "MATCH (ro:ResourceOperation)<-[:MANAGES]-(f:Function) "
                "WHERE f.id IN $ids DETACH DELETE ro",
                ids=func_ids,
            )

            # 4. 删除与这些 Function 关联的 ExternalCall（通过 EXTERNAL_CALLS 边）
            session.run(
                "MATCH (ec:ExternalCall)<-[:EXTERNAL_CALLS]-(f:Function) "
                "WHERE f.id IN $ids DETACH DELETE ec",
                ids=func_ids,
            )

            # 5. 删除与这些 Function 关联的 AmbiguousCall（通过 CALLS_AMBIGUOUS 边）
            session.run(
                "MATCH (ac:AmbiguousCall)<-[:CALLS_AMBIGUOUS]-(f:Function) "
                "WHERE f.id IN $ids DETACH DELETE ac",
                ids=func_ids,
            )

            # 6. 删除 Function 及其所有剩余关系
            # DETACH DELETE 自动处理 BELONGS_TO, HAS_METHOD, CALLS, REFERENCES_VAR 等
            session.run("MATCH (f:Function {file_path: $fp}) DETACH DELETE f", fp=file_path)

        # 7. 删除 Class 及其关系（HAS_MEMBER 等由 DETACH DELETE 自动处理）
        session.run("MATCH (c:Class {file_path: $fp}) DETACH DELETE c", fp=file_path)

        # 8. 删除 Variable 及其关系
        session.run("MATCH (v:Variable {file_path: $fp}) DETACH DELETE v", fp=file_path)

        # 9. 删除 Attribute 及其关系
        session.run("MATCH (a:Attribute {file_path: $fp}) DETACH DELETE a", fp=file_path)

        # 10. 删除 File 节点
        session.run("MATCH (f:File {path: $fp}) DETACH DELETE f", fp=file_path)


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
        delete_file_nodes(driver, fp, database)
        logger.debug("Deleted old nodes for %s", fp)

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
                    raise

            # 解析字段和调用关系
            file_results = enrich_file_results(file_results)
            all_functions: list[FunctionSymbol] = []
            all_raw_calls: list[RawCall] = []
            all_var_refs: list[tuple[str, str, int]] = []
            for fr in file_results:
                all_functions.extend(fr.functions)
                all_raw_calls.extend(fr.calls)
                all_var_refs.extend(fr.raw.get("var_refs_global", []))

            resolved = resolve_all_calls(all_functions, all_raw_calls)
            graph = assemble_graph(
                file_results,
                resolved,
                repo_root=str(repo_root),
                var_refs_global=all_var_refs,
            )
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
