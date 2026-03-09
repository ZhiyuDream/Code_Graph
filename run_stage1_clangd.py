#!/usr/bin/env python3
"""
阶段 1（clangd 版）：通过 clangd LSP 采集代码图并写入 Neo4j。
与 run_stage1.py 目标相同，但使用 clangd 的 documentSymbol 与 call hierarchy，
便于与 IDE 行为一致，且可能得到更好的跨文件调用解析。

前置：本机已安装 clangd（PATH 可用）；REPO_ROOT、COMPILE_COMMANDS_DIR（或 build）已配置；
      Neo4j 已配置。首次运行 clangd 会做背景索引，llama.cpp 规模约需数分钟到十几分钟。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

_CODE_GRAPH = Path(__file__).resolve().parent
if str(_CODE_GRAPH) not in sys.path:
    sys.path.insert(0, str(_CODE_GRAPH))

from config import get_compile_commands_path, get_repo_root
from clangd_parser import collect_all_via_clangd
from graph_builder import build_graph
from neo4j_writer import (
    get_driver,
    ensure_constraints,
    clear_code_graph,
    write_graph,
    get_head_commit,
    update_repository_commit,
)


def main() -> int:
    build_dir = get_compile_commands_path()
    if not build_dir:
        print("未找到 compile_commands.json。请设置 REPO_ROOT 或 COMPILE_COMMANDS_DIR。")
        return 1
    repo_root = get_repo_root()
    repo_root_str = str(repo_root) if repo_root else ""

    print("使用 clangd LSP 采集代码图（首次可能较慢，需等待 clangd 索引）。")
    print(f"compile_commands 目录: {build_dir}")
    print(f"仓库根: {repo_root_str or '(未设置 REPO_ROOT)'}")
    t0 = time.perf_counter()
    tu_results, var_refs_global = collect_all_via_clangd(
        build_dir, repo_root, delay_after_init=3.0, delay_between_files=0.03, collect_var_refs=True
    )
    elapsed = time.perf_counter() - t0
    print(f"clangd 解析完成，共 {len(tu_results)} 个文件，耗时 {elapsed:.1f}s。")
    if not tu_results:
        print("没有解析到任何文件。")
        return 1

    print("构建图结构…")
    graph = build_graph(tu_results, repo_root_str, var_refs_global=var_refs_global)
    n = graph["nodes"]
    n_dir = len(n.get("Directory", []))
    n_file = len(n.get("File", []))
    n_func = len(n.get("Function", []))
    n_class = len(n.get("Class", []))
    n_var = len(n.get("Variable", []))
    n_contains = len(graph["edges"].get("CONTAINS", []))
    n_calls = len(graph["edges"].get("CALLS", []))
    n_refs_var = len(graph["edges"].get("REFERENCES_VAR", []))
    print(f"  节点: Repository=1, Directory={n_dir}, File={n_file}, Function={n_func}, Class={n_class}, Variable={n_var}")
    print(f"  边: CONTAINS={n_contains}, CALLS={n_calls}, REFERENCES_VAR={n_refs_var}")

    driver = get_driver()
    try:
        driver.verify_connectivity()
    except Exception as e:
        print(f"Neo4j 连接失败: {e}")
        return 1
    ensure_constraints(driver)
    print("清空现有代码图…")
    clear_code_graph(driver)
    print("写入 Neo4j…")
    write_graph(driver, graph)
    if repo_root and graph["nodes"].get("Repository"):
        repo_id = graph["nodes"]["Repository"][0]["id"]
        sha = get_head_commit(repo_root)
        if sha:
            update_repository_commit(driver, repo_id, sha)
            print(f"已更新 last_processed_commit = {sha[:8]}…")
    driver.close()
    print("阶段 1（clangd）完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
