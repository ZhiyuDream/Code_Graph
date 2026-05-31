"""
代码全量摄取（clangd LSP → Neo4j）主入口。

编排 symbol_extractor -> field_resolver -> call_resolver -> graph_builder -> neo4j_writer。

此模块只负责**编排**各步骤的执行顺序，不包含具体的业务逻辑。
具体实现分布在各子模块中：
- source_collector: 源文件收集
- symbol_extractor: 符号提取
- field_resolver: 字段归属修正
- call_resolver: 调用关系解析
- graph_builder: 图结构组装
- neo4j_writer: 批量写入 Neo4j
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from .call_resolver import resolve_all_calls
from .field_resolver import enrich_file_results
from .graph_builder import assemble_graph
from .index_waiter import wait_for_index
from .lsp_client import LSPClient, LSPTimeoutError
from .models import FileResult, FunctionSymbol, RawCall, ResolvedCalls
from .neo4j_writer import clear_code_graph, ensure_constraints, write_graph
from .source_collector import collect_source_files
from .symbol_extractor import process_file, extract_incoming_calls_for_function

logger = logging.getLogger(__name__)


def _extract_incoming_calls(
    client: LSPClient,
    file_results: list[FileResult],
    repo_root: Path,
) -> list[RawCall]:
    """
    补充 incomingCalls（捕获 lambda 内的调用关系）。

    策略：只对有 outgoingCalls 为 0 的函数执行 incomingCalls（通常是 lambda/回调）。
    按文件分组批量处理，减少 didOpen/didClose 次数。
    """
    # 统计每个函数有多少 outgoingCalls
    func_outgoing_count: dict[str, int] = {}
    for fr in file_results:
        for rc in fr.calls:
            caller_id = f"{rc.file_path}:{rc.line}"
            func_outgoing_count[caller_id] = func_outgoing_count.get(caller_id, 0) + 1

    # 筛选出 outgoingCalls 为 0 的函数（通常是 lambda/回调）
    funcs_with_zero_outgoing = []
    for fr in file_results:
        for func in fr.functions:
            func_id = f"{func.file_path}:{func.start_line}"
            if func_outgoing_count.get(func_id, 0) == 0:
                funcs_with_zero_outgoing.append((fr, func))

    logger.info("Extracting incoming calls (lambda-aware, selective: %d/%d funcs)...",
                len(funcs_with_zero_outgoing),
                sum(len(fr.functions) for fr in file_results))
    t_incoming = time.perf_counter()
    incoming_raw_calls: list[RawCall] = []

    # 建立全局函数索引（用于去重已有的 outgoing 调用）
    existing_edges: set[tuple[str, int, str]] = set()
    for fr in file_results:
        for rc in fr.calls:
            existing_edges.add((rc.file_path, rc.line, rc.callee_name))

    # 按文件分组，减少 didOpen/didClose 次数
    funcs_by_file: dict[str, list[tuple[Any, FunctionSymbol]]] = {}
    for fr, func in funcs_with_zero_outgoing:
        funcs_by_file.setdefault(fr.file_path, []).append((fr, func))

    for i, (file_path, func_list) in enumerate(funcs_by_file.items()):
        abs_path = str(repo_root / file_path)
        file_uri = Path(abs_path).resolve().as_uri()
        # 确保文件已 didOpen
        try:
            content = Path(abs_path).read_text(encoding="utf-8", errors="replace")
            client.notify("textDocument/didOpen", {
                "textDocument": {"uri": file_uri, "languageId": "cpp", "version": 1, "text": content}
            })
        except Exception:
            pass

        for fr, func in func_list:
            try:
                inc_calls = extract_incoming_calls_for_function(
                    client.request, file_uri, func,
                    repo_root=repo_root, sleep_after=0.01, timeout=30.0,
                )
                for rc in inc_calls:
                    # 去重：如果 outgoingCalls 已经捕获了这条边，跳过
                    edge_key = (rc.file_path, rc.line, rc.callee_name)
                    if edge_key not in existing_edges:
                        incoming_raw_calls.append(rc)
                        existing_edges.add(edge_key)
            except Exception as e:
                logger.warning("Incoming calls timeout for %s:%s:%d: %s",
                               func.file_path, func.name, func.start_line, e)

        if (i + 1) % 50 == 0:
            logger.info("  Incoming calls: %d/%d files...", i + 1, len(funcs_by_file))

    incoming_elapsed = time.perf_counter() - t_incoming
    logger.info("Incoming calls done in %.1fs, added %d new edges",
                incoming_elapsed, len(incoming_raw_calls))
    return incoming_raw_calls


def _convert_calls_to_global(
    file_results: list[FileResult],
) -> tuple[list[FunctionSymbol], list[RawCall], list[tuple[str, str, int]]]:
    """
    将文件内局部 caller_index 转换为全局索引，并收集跨文件变量引用。

    Returns:
        (all_functions, all_raw_calls, all_var_refs)
    """
    all_functions: list[FunctionSymbol] = []
    all_raw_calls: list[RawCall] = []
    all_var_refs: list[tuple[str, str, int]] = []

    offset = 0
    for fr in file_results:
        for f in fr.functions:
            all_functions.append(f)
        # caller_index 是文件内局部索引，需要转换为全局索引
        for rc in fr.calls:
            all_raw_calls.append(RawCall(
                caller_index=rc.caller_index + offset,
                callee_name=rc.callee_name,
                file_path=rc.file_path,
                line=rc.line,
                callee_file_path=rc.callee_file_path,
                callee_line=rc.callee_line,
            ))
        all_var_refs.extend(fr.raw.get("var_refs_global", []))
        offset += len(fr.functions)

    return all_functions, all_raw_calls, all_var_refs


def _build_stats(
    file_results: list[FileResult],
    graph: dict[str, Any],
    resolved: ResolvedCalls,
    extract_elapsed: float,
    total_elapsed: float,
) -> dict[str, Any]:
    """构建统计信息 dict。"""
    n = graph["nodes"]
    e = graph["edges"]
    return {
        "files": len(file_results),
        "functions": len(n.get("Function", [])),
        "classes": len(n.get("Class", [])),
        "variables": len(n.get("Variable", [])),
        "attributes": len(n.get("Attribute", [])),
        "calls": len(e.get("CALLS", [])),
        "ambiguous": len(e.get("CALLS_AMBIGUOUS", [])),
        "unresolved": len(resolved.unresolved),
        "external_calls": len(e.get("EXTERNAL_CALLS", [])),
        "control_flow": len(n.get("ControlFlowBlock", [])),
        "resource_ops": len(n.get("ResourceOperation", [])),
        "elapsed_total": total_elapsed,
        "elapsed_extract": extract_elapsed,
    }


def run_extraction_only(
    repo_root: Path,
    compile_commands_dir: Path,
    files: list[str],
    collect_calls: bool = True,
    collect_var_refs: bool = True,
    extract_macros: bool = True,
    skip_vendor_calls: bool = True,
) -> list[FileResult]:
    """
    只执行符号提取阶段（LSP extraction），返回 FileResult 列表。

    用于并行 ingestion：每个 worker 进程独立启动 clangd，处理分配到的文件子集。
    """
    logger.info("[Worker] Starting clangd for %d files...", len(files))
    with LSPClient.start(repo_root, compile_commands_dir) as client:
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
            logger.warning("[Worker] clangd index may not be fully ready, continuing anyway")

        logger.info("[Worker] Extracting symbols from %d files...", len(files))
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
                    delay_between_calls=0.0,
                    lsp_notify=client.notify,
                    extract_macros=extract_macros,
                    skip_vendor_calls=skip_vendor_calls,
                )
                file_results.append(result)
            except LSPTimeoutError as e:
                logger.warning("[Worker] Timeout processing %s: %s", file_path, e)
            except Exception as e:
                logger.warning("[Worker] Failed to process %s: %s", file_path, e)
                raise

            if (i + 1) % 50 == 0:
                logger.info("[Worker]   Processed %d/%d files...", i + 1, len(files))

        elapsed = time.perf_counter() - t0
        logger.info("[Worker] Symbol extraction done in %.1fs", elapsed)
    return file_results


def run_pipeline_from_results(
    file_results: list[FileResult],
    repo_root: Path,
    compile_commands_dir: Path,
    driver: Any,
    database: str = "neo4j",
    collect_calls: bool = True,
    collect_var_refs: bool = True,
    clear_existing: bool = True,
    extract_elapsed: float = 0.0,
) -> dict[str, Any]:
    """
    从已提取的 FileResult 出发，完成后续所有阶段（incomingCalls → resolver → graph → Neo4j）。

    用于并行 ingestion 的主进程：收集所有 worker 的结果后统一执行。
    """
    t0_total = time.perf_counter()

    # incomingCalls 补充（需要单实例 LSPClient）
    incoming_raw_calls: list[RawCall] = []
    if collect_calls:
        logger.info("Starting clangd for incoming calls...")
        with LSPClient.start(repo_root, compile_commands_dir) as client:
            incoming_raw_calls = _extract_incoming_calls(client, file_results, repo_root)

    # 解析字段
    logger.info("Resolving fields...")
    file_results = enrich_file_results(file_results)

    # 解析调用关系
    logger.info("Resolving calls...")
    all_functions, all_raw_calls, all_var_refs = _convert_calls_to_global(file_results)
    all_raw_calls.extend(incoming_raw_calls)

    resolved = resolve_all_calls(all_functions, all_raw_calls, repo_root=repo_root)

    # 组装图
    logger.info("Assembling graph...")
    graph = assemble_graph(
        file_results,
        resolved,
        repo_root=str(repo_root),
        var_refs_global=all_var_refs if collect_var_refs else None,
    )

    # 写入 Neo4j
    logger.info("Writing to Neo4j...")
    ensure_constraints(driver, database)
    if clear_existing:
        clear_code_graph(driver, database)
    write_graph(driver, graph, database)

    total_elapsed = time.perf_counter() - t0_total

    # 统计
    stats = _build_stats(file_results, graph, resolved, extract_elapsed, total_elapsed)
    logger.info(
        "Pipeline complete in %.1fs: %d files, %d functions, %d classes, "
        "%d variables, %d attributes, %d calls, %d ambiguous, %d unresolved, "
        "%d external, %d control_flow, %d resource_ops",
        total_elapsed, stats["files"], stats["functions"], stats["classes"],
        stats["variables"], stats["attributes"], stats["calls"],
        stats["ambiguous"], stats["unresolved"],
        stats["external_calls"], stats["control_flow"], stats["resource_ops"],
    )
    return stats


def run_full_pipeline(
    repo_root: Path,
    compile_commands_dir: Path,
    driver: Any,
    database: str = "neo4j",
    collect_calls: bool = True,
    collect_var_refs: bool = True,
    include_dirs: list[str] | None = None,
    extract_macros: bool = True,
    skip_vendor_calls: bool = True,
    clear_existing: bool = True,
) -> dict[str, Any]:
    """
    执行全量建图 pipeline（单实例模式，向后兼容）。

    Args:
        repo_root: 仓库根目录
        compile_commands_dir: compile_commands.json 所在目录
        driver: Neo4j driver
        database: Neo4j 数据库名
        collect_calls: 是否收集 outgoingCalls
        collect_var_refs: 是否收集变量引用
        include_dirs: 若指定，只处理这些顶层目录下的文件
        extract_macros: 是否基于正则提取宏调用（补充 clangd 遗漏）
        skip_vendor_calls: 是否跳过 vendor 目录函数的 outgoingCalls
        clear_existing: 是否先清空现有代码图（增量更新时应设为 False）

    Returns:
        统计信息 dict
    """
    t0_total = time.perf_counter()

    files = collect_source_files(compile_commands_dir, repo_root, include_dirs=include_dirs)
    if not files:
        raise RuntimeError("No source files found")

    # 单实例提取
    file_results = run_extraction_only(
        repo_root=repo_root,
        compile_commands_dir=compile_commands_dir,
        files=files,
        collect_calls=collect_calls,
        collect_var_refs=collect_var_refs,
        extract_macros=extract_macros,
        skip_vendor_calls=skip_vendor_calls,
    )
    extract_elapsed = time.perf_counter() - t0_total

    # 后续阶段
    stats = run_pipeline_from_results(
        file_results=file_results,
        repo_root=repo_root,
        compile_commands_dir=compile_commands_dir,
        driver=driver,
        database=database,
        collect_calls=collect_calls,
        collect_var_refs=collect_var_refs,
        clear_existing=clear_existing,
        extract_elapsed=extract_elapsed,
    )

    # 修正总时间（包含提取）
    stats["elapsed_total"] = time.perf_counter() - t0_total
    return stats
