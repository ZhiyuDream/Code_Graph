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
from .lsp_client import LSPClient, LSPTimeoutError
from .models import FileResult, FunctionSymbol, RawCall, ResolvedCalls
from .neo4j_batch_writer import clear_code_graph, ensure_constraints, write_graph
from .symbol_extractor import process_file, extract_incoming_calls_for_function

logger = logging.getLogger(__name__)


def _collect_source_files(
    compile_commands_dir: Path,
    repo_root: Path,
    include_dirs: list[str] | None = None,
) -> list[str]:
    """
    收集源文件。

    策略：
    1. 从 compile_commands.json 收集（clangd 有编译命令的文件）
    2. os.walk 补充缺失的文件（包括头文件）
    3. 按文件类型排序：先 .cpp/.c/.cc/.cxx（实现文件），后 .h/.hpp（头文件）
       这样 clangd 的 background-index 会先索引实现文件及其包含的头文件，
       后续处理头文件时 documentSymbol 请求不会超时卡住。

    Args:
        include_dirs: 若指定，只收集这些目录下的文件（相对于 repo_root 的顶层目录名）
    """
    import json
    import os

    source_exts = {".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hh", ".hxx"}
    impl_exts = {".c", ".cpp", ".cc", ".cxx"}  # 实现文件，可以补充

    def _should_include(rel_path: str) -> bool:
        if include_dirs is None:
            return True
        top = rel_path.split(os.sep)[0]
        return top in include_dirs

    # 从 compile_commands.json 收集（所有文件类型）
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

    # 扫描所有源文件（补充 compile_commands.json 中缺失的文件）
    # 头文件也补充，但按类型排序：先 .cpp/.c 让 clangd background-index 索引头文件，
    # 再处理头文件时就不会超时卡住
    skip_dirs = {'.git', 'build', 'bin', 'obj', '.cache', 'node_modules', 'venv', '.venv', '__pycache__'}
    if repo_root.exists():
        for root, dirs, files in os.walk(repo_root):
            dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith('.')]
            for f in files:
                if Path(f).suffix.lower() in source_exts:
                    full_path = os.path.normpath(os.path.join(root, f))
                    rel = os.path.relpath(full_path, repo_root)
                    if rel.startswith("build") and os.sep in rel:
                        continue
                    if not _should_include(rel):
                        continue
                    source_files.add(full_path)

    # 按文件类型排序：先实现文件(.cpp/.c/.cc/.cxx)，后头文件(.h/.hpp)
    # 这样 clangd 的 background-index 会先索引实现文件及其包含的头文件，
    # 后续处理头文件时 documentSymbol 请求不会超时
    def _sort_key(path: str) -> tuple:
        ext = Path(path).suffix.lower()
        # 实现文件排前面 (0)，头文件排后面 (1)
        is_header = 1 if ext in {'.h', '.hpp', '.hh', '.hxx'} else 0
        return (is_header, path)

    sorted_paths = sorted(source_files, key=_sort_key)

    # 转换为相对路径
    result = []
    for abs_path in sorted_paths:
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
    extract_macros: bool = True,
    skip_vendor_calls: bool = True,
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
        extract_macros: 是否基于正则提取宏调用（补充 clangd 遗漏）
        skip_vendor_calls: 是否跳过 vendor 目录函数的 outgoingCalls

    Returns:
        统计信息 dict
    """
    t0_total = time.perf_counter()
    incoming_raw_calls: list[RawCall] = []

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
                    extract_macros=extract_macros,
                    skip_vendor_calls=skip_vendor_calls,
                )
                file_results.append(result)
            except LSPTimeoutError as e:
                # clangd 处理某些文件时可能超时（如复杂模板），跳过该文件
                logger.warning("Timeout processing %s: %s", file_path, e)
                # 不 re-raise，继续下一个文件
            except Exception as e:
                logger.warning("Failed to process %s: %s", file_path, e)
                raise  # 其他异常向上传播

            if (i + 1) % 50 == 0:
                logger.info("  Processed %d/%d files...", i + 1, len(files))

        extract_elapsed = time.perf_counter() - t0
        logger.info("Symbol extraction done in %.1fs", extract_elapsed)

        # 3b. incomingCalls 补充（捕获 lambda 内的调用关系）
        # 优化：只对有 outgoingCalls 的函数执行 incomingCalls
        if collect_calls:
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
            existing_edges: set[tuple[str, int, str]] = set()  # (caller_file, caller_line, callee_name)
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
                    content = Path(abs_path).read_text(encoding='utf-8', errors='replace')
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
                        # 记录超时的函数，后续分析用
                        logger.warning("Incoming calls timeout for %s:%s:%d: %s",
                                       func.file_path, func.name, func.start_line, e)

                if (i + 1) % 50 == 0:
                    logger.info("  Incoming calls: %d/%d files...", i + 1, len(funcs_by_file))

            incoming_elapsed = time.perf_counter() - t_incoming
            logger.info("Incoming calls done in %.1fs, added %d new edges",
                        incoming_elapsed, len(incoming_raw_calls))

    # 4. 解析字段
    logger.info("Resolving fields...")
    file_results = enrich_file_results(file_results)

    # 5. 解析调用关系
    logger.info("Resolving calls...")
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

    # 合并 incomingCalls 补充的调用关系
    all_raw_calls.extend(incoming_raw_calls)

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
        "external_calls": len(e.get("EXTERNAL_CALLS", [])),
        "control_flow": len(n.get("ControlFlowBlock", [])),
        "elapsed_total": total_elapsed,
        "elapsed_extract": extract_elapsed,
    }
    logger.info(
        "Pipeline complete in %.1fs: %d files, %d functions, %d classes, "
        "%d variables, %d attributes, %d calls, %d ambiguous, %d unresolved, "
        "%d external, %d control_flow",
        total_elapsed, stats["files"], stats["functions"], stats["classes"],
        stats["variables"], stats["attributes"], stats["calls"],
        stats["ambiguous"], stats["unresolved"],
        stats["external_calls"], stats["control_flow"],
    )
    return stats
