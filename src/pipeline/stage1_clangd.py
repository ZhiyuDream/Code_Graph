"""
阶段 1（clangd 版）全量建图主入口。

编排 symbol_extractor -> field_resolver -> call_resolver -> graph_assembler -> neo4j_batch_writer。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from .call_resolver import resolve_all_calls
from .field_resolver import enrich_file_results
from .graph_assembler import assemble_graph
from .index_waiter import wait_for_index
from .lsp_client import LSPClient
from .models import FileResult, FunctionSymbol, RawCall, ResolvedCalls
from .neo4j_batch_writer import clear_code_graph, ensure_constraints, write_graph
from .symbol_extractor import process_file

logger = logging.getLogger(__name__)


def _collect_source_files(
    compile_commands_dir: Path,
    repo_root: Path,
    include_dirs: list[str] | None = None,
) -> list[str]:
    """
    收集所有源文件（含头文件）。
    复用 clangd_parser 的逻辑。

    Args:
        include_dirs: 若指定，只收集这些目录下的文件（相对于 repo_root 的顶层目录名）
    """
    import json
    import os

    source_exts = {".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hh", ".hxx"}

    def _should_include(rel_path: str) -> bool:
        if include_dirs is None:
            return True
        top = rel_path.split(os.sep)[0]
        return top in include_dirs

    # 从 compile_commands.json 收集
    cc_path = compile_commands_dir / "compile_commands.json"
    source_files: set[str] = set()
    if cc_path.exists():
        with open(cc_path, encoding="utf-8") as f:
            data = json.load(f)
        for entry in data:
            fpath = entry.get("file", "")
            if not fpath or Path(fpath).suffix.lower() not in source_exts:
                continue
            if not os.path.isabs(fpath):
                cwd = entry.get("directory", "")
                fpath = os.path.normpath(os.path.join(cwd, fpath))
            # 排除 build/ 目录下的生成文件
            rel = os.path.relpath(fpath, repo_root)
            if rel.startswith("build") and os.sep in rel:
                continue
            if not _should_include(rel):
                continue
            source_files.add(fpath)

    # 扫描头文件
    header_exts = {".h", ".hpp", ".hh", ".hxx"}
    skip_dirs = {'.git', 'build', 'bin', 'obj', '.cache', 'node_modules', 'venv', '.venv', '__pycache__'}
    if repo_root.exists():
        for root, dirs, files in os.walk(repo_root):
            dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith('.')]
            for f in files:
                if Path(f).suffix.lower() in header_exts:
                    full_path = os.path.normpath(os.path.join(root, f))
                    rel = os.path.relpath(full_path, repo_root)
                    if not _should_include(rel):
                        continue
                    source_files.add(full_path)

    # 转换为相对路径
    result = []
    for abs_path in sorted(source_files):
        if abs_path.startswith(str(repo_root)):
            result.append(os.path.relpath(abs_path, repo_root))
        else:
            result.append(abs_path)
    return result


def run_full_pipeline(
    repo_root: Path,
    compile_commands_dir: Path,
    driver: Any,
    database: str = "neo4j",
    collect_calls: bool = True,
    collect_var_refs: bool = True,
    include_dirs: list[str] | None = None,
) -> dict[str, Any]:
    """
    执行全量建图 pipeline。

    Args:
        repo_root: 仓库根目录
        compile_commands_dir: compile_commands.json 所在目录
        driver: Neo4j driver
        database: Neo4j 数据库名
        collect_calls: 是否收集 outgoingCalls
        collect_var_refs: 是否收集变量引用
        include_dirs: 若指定，只处理这些顶层目录下的文件

    Returns:
        统计信息 dict
    """
    t0_total = time.perf_counter()

    # 1. 启动 clangd
    logger.info("Starting clangd...")
    with LSPClient.start(repo_root, compile_commands_dir) as client:
        # 2. 等待索引就绪
        files = _collect_source_files(compile_commands_dir, repo_root, include_dirs=include_dirs)
        if not files:
            raise RuntimeError("No source files found")

        sample_path = repo_root / files[0]
        sample_uri = sample_path.resolve().as_uri()
        sample_content = sample_path.read_text(encoding="utf-8", errors="replace")
        ready = wait_for_index(
            client.request,
            sample_uri,
            initial_delay=2.0,
            poll_interval=2.0,
            max_wait=120.0,
            lsp_notify=client.notify,
            file_content=sample_content,
        )
        if not ready:
            logger.warning("clangd index may not be fully ready, continuing anyway")

        # 3. 提取符号（逐文件）
        logger.info("Extracting symbols from %d files...", len(files))
        t0 = time.perf_counter()
        file_results: list[FileResult] = []

        for i, file_path in enumerate(files):
            abs_path = repo_root / file_path
            try:
                result = process_file(
                    client.request,
                    str(abs_path),
                    file_path,
                    repo_root=repo_root,
                    collect_calls=collect_calls,
                    collect_var_refs=collect_var_refs,
                    delay_between_calls=0.0 if not collect_calls else 0.02,
                    lsp_notify=client.notify,
                )
                file_results.append(result)
            except Exception as e:
                logger.warning("Failed to process %s: %s", file_path, e)
                raise  # 向上传播，不再静默吞掉

            if (i + 1) % 50 == 0:
                logger.info("  Processed %d/%d files...", i + 1, len(files))

        extract_elapsed = time.perf_counter() - t0
        logger.info("Symbol extraction done in %.1fs", extract_elapsed)

    # 4. 解析字段
    logger.info("Resolving fields...")
    file_results = enrich_file_results(file_results)

    # 5. 解析调用关系
    logger.info("Resolving calls...")
    all_functions: list[FunctionSymbol] = []
    all_raw_calls: list[RawCall] = []
    all_var_refs: list[tuple[str, str, int]] = []

    for fr in file_results:
        for f in fr.functions:
            all_functions.append(f)
        all_raw_calls.extend(fr.calls)
        all_var_refs.extend(fr.raw.get("var_refs_global", []))

    resolved = resolve_all_calls(all_functions, all_raw_calls)

    # 6. 组装图
    logger.info("Assembling graph...")
    graph = assemble_graph(
        file_results,
        resolved,
        repo_root=str(repo_root),
        var_refs_global=all_var_refs if collect_var_refs else None,
    )

    # 7. 写入 Neo4j
    logger.info("Writing to Neo4j...")
    ensure_constraints(driver, database)
    clear_code_graph(driver, database)
    write_graph(driver, graph, database)

    total_elapsed = time.perf_counter() - t0_total

    # 统计
    n = graph["nodes"]
    e = graph["edges"]
    stats = {
        "files": len(file_results),
        "functions": len(n.get("Function", [])),
        "classes": len(n.get("Class", [])),
        "variables": len(n.get("Variable", [])),
        "attributes": len(n.get("Attribute", [])),
        "calls": len(e.get("CALLS", [])),
        "ambiguous": len(e.get("CALLS_AMBIGUOUS", [])),
        "unresolved": len(resolved.unresolved),
        "elapsed_total": total_elapsed,
        "elapsed_extract": extract_elapsed,
    }
    logger.info(
        "Pipeline complete in %.1fs: %d files, %d functions, %d classes, "
        "%d variables, %d attributes, %d calls, %d ambiguous, %d unresolved",
        total_elapsed, stats["files"], stats["functions"], stats["classes"],
        stats["variables"], stats["attributes"], stats["calls"],
        stats["ambiguous"], stats["unresolved"],
    )
    return stats
