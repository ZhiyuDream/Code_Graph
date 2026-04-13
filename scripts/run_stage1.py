#!/usr/bin/env python3
"""
阶段 1：代码图采集。
从 compile_commands.json 解析 C/C++ 仓库，构建 Repository、Directory、File、Function、Class 及 CONTAINS、CALLS，
写入 Neo4j，并更新 last_processed_commit。

使用 clangd LSP（20+）进行解析，提供准确的跨文件调用关系。

使用前请：
  1. 安装 clangd 20+（apt install clangd 或从 LLVM 官网下载）
  2. 在目标仓库（如 llama.cpp）下生成 compile_commands.json（例如 mkdir build && cd build && cmake ..）
  3. 在 .env 中配置 REPO_ROOT（仓库根目录）和/或 COMPILE_COMMANDS_DIR（含 compile_commands.json 的目录，默认 REPO_ROOT/build）
  4. 配置 NEO4J_* 等（见 .env）

可选：创建 conda 环境后安装依赖：
  conda create -n code_graph python=3.11
  conda activate code_graph
  pip install -r requirements.txt
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

# 确保 Code_Graph 目录在 path 中
_CODE_GRAPH = Path(__file__).resolve().parent.parent
if str(_CODE_GRAPH) not in sys.path:
    sys.path.insert(0, str(_CODE_GRAPH))

from config import get_compile_commands_path, get_repo_root
from src.clangd_parser import collect_all_via_clangd
from src.graph_builder import build_graph
from src.neo4j_writer import (
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
        print("未找到 compile_commands.json。请设置 REPO_ROOT 或 COMPILE_COMMANDS_DIR，并在仓库 build 下执行 cmake。")
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
        print("没有解析到任何文件，请检查 compile_commands.json 与仓库路径。")
        return 1

    print("构建图结构…")
    graph = build_graph(tu_results, repo_root_str, var_refs_global=var_refs_global)
    n = graph["nodes"]
    n_repo = len(n.get("Repository", []))
    n_dir = len(n.get("Directory", []))
    n_file = len(n.get("File", []))
    n_func = len(n.get("Function", []))
    n_class = len(n.get("Class", []))
    n_var = len(n.get("Variable", []))
    n_contains = len(graph["edges"].get("CONTAINS", []))
    n_calls = len(graph["edges"].get("CALLS", []))
    n_refs_var = len(graph["edges"].get("REFERENCES_VAR", []))
    print(f"  节点: Repository={n_repo}, Directory={n_dir}, File={n_file}, Function={n_func}, Class={n_class}, Variable={n_var}")
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
    print("阶段 1 完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
