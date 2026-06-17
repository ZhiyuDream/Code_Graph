"""
增量更新模块：基于 git diff 只更新变更的文件，避免全量重建。

核心策略：
1. 获取 git diff 变更文件列表
2. 删除变更文件相关的所有节点和边（包括作为调用者/被调用者的边）
3. 重新解析变更文件
4. 写入新节点和边
5. 更新 last_processed_commit

处理跨文件调用边：
- 文件 A 变更时，删除所有 CALLS(A→X) 和 CALLS(X→A)
- 重新解析 A 后，CALLS(A→X) 会重建，CALLS(X→A) 需要 X 重新解析才能重建
- 简化：对于指向 A 的外部调用，在 A 重新解析后，通过变量引用或其他方式处理
- 实际策略：只保证变更文件自身的边准确，外部文件的入边暂时忽略（因为函数签名可能已变）
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Set

from neo4j import GraphDatabase

from config import NEO4J_DATABASE


def get_changed_files(repo_root: Path, since_commit: str) -> Set[str]:
    """
    获取自 since_commit 以来变更的文件列表（相对于仓库根的路径）。
    包括：修改、新增、重命名的文件。
    """
    if not since_commit:
        # 如果没有记录，返回所有文件（全量更新）
        return set()
    
    try:
        # 获取变更、新增、重命名的文件
        result = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=AMR", 
             f"{since_commit}..HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            print(f"git diff 失败: {result.stderr}")
            return set()
        
        files = set()
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line:
                files.add(line)
        return files
    except Exception as e:
        print(f"获取变更文件失败: {e}")
        return set()


def get_deleted_files(repo_root: Path, since_commit: str) -> Set[str]:
    """
    获取自 since_commit 以来删除的文件列表。
    """
    if not since_commit:
        return set()
    
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=D",
             f"{since_commit}..HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return set()
        
        files = set()
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line:
                files.add(line)
        return files
    except Exception as e:
        print(f"获取删除文件失败: {e}")
        return set()


def delete_file_nodes_and_edges(
    driver: GraphDatabase.driver,
    file_path: str,
    database: str = NEO4J_DATABASE,
) -> dict[str, int]:
    """
    删除指定 file_path 相关的所有节点和边。
    
    包括：
    - 该文件中的所有 Function、Class、Variable 节点
    - 这些节点作为源或目标的所有边（CALLS、REFERENCES_VAR、CONTAINS 等）
    - File 节点本身
    
    返回删除的节点/边统计。
    """
    stats = {"functions": 0, "classes": 0, "variables": 0, 
             "calls_deleted": 0, "file_deleted": 0}
    
    with driver.session(database=database) as session:
        # 1. 找出该文件中的所有函数 ID
        result = session.run(
            """
            MATCH (f:Function {file_path: $file_path})
            RETURN f.id AS func_id
            """,
            file_path=file_path
        )
        func_ids = [r["func_id"] for r in result]
        stats["functions"] = len(func_ids)
        
        # 2. 删除这些函数作为 caller 或 callee 的所有 CALLS 边
        if func_ids:
            session.run(
                """
                MATCH (a:Function)-[r:CALLS]->(b:Function)
                WHERE a.id IN $func_ids OR b.id IN $func_ids
                DELETE r
                """,
                func_ids=func_ids
            )
        
        # 3. 删除这些函数的 REFERENCES_VAR 边
        if func_ids:
            session.run(
                """
                MATCH (f:Function)-[r:REFERENCES_VAR]->(v:Variable)
                WHERE f.id IN $func_ids
                DELETE r
                """,
                func_ids=func_ids
            )
        
        # 4. 删除该文件中的 Function 节点
        session.run(
            """
            MATCH (f:Function {file_path: $file_path})
            DETACH DELETE f
            """,
            file_path=file_path
        )
        
        # 5. 找出并删除 Class 节点及其边
        result = session.run(
            """
            MATCH (c:Class {file_path: $file_path})
            RETURN c.id AS class_id
            """,
            file_path=file_path
        )
        class_ids = [r["class_id"] for r in result]
        stats["classes"] = len(class_ids)
        
        if class_ids:
            # 删除 Class 相关的边
            session.run(
                """
                MATCH (c:Class)-[r]->()
                WHERE c.id IN $class_ids
                DELETE r
                """,
                class_ids=class_ids
            )
            # 删除 Class 节点
            session.run(
                """
                MATCH (c:Class {file_path: $file_path})
                DETACH DELETE c
                """,
                file_path=file_path
            )
        
        # 6. 删除 Variable 节点
        result = session.run(
            """
            MATCH (v:Variable {file_path: $file_path})
            RETURN count(v) AS cnt
            """,
            file_path=file_path
        )
        stats["variables"] = result.single()["cnt"]
        
        session.run(
            """
            MATCH (v:Variable {file_path: $file_path})
            DETACH DELETE v
            """,
            file_path=file_path
        )
        
        # 7. 删除 Attribute 节点
        session.run(
            """
            MATCH (a:Attribute {file_path: $file_path})
            DETACH DELETE a
            """,
            file_path=file_path
        )
        
        # 8. 删除 File 节点及其 CONTAINS 边
        session.run(
            """
            MATCH (f:File {path: $file_path})
            DETACH DELETE f
            """,
            file_path=file_path
        )
        stats["file_deleted"] = 1
    
    return stats


def should_update_graph(repo_root: Path, driver: GraphDatabase.driver, database: str = NEO4J_DATABASE) -> tuple[bool, str, str]:
    """
    检查是否需要更新图。
    
    返回: (need_update, last_commit, current_commit)
    - need_update: 是否需要更新
    - last_commit: 上次处理的 commit
    - current_commit: 当前 HEAD commit
    """
    from src.neo4j_writer import get_head_commit
    
    from src.neo4j_writer import get_head_commit
    current_commit = get_head_commit(repo_root)
    if not current_commit:
        print("无法获取当前 commit")
        return False, "", ""
    
    # 从 Neo4j 获取上次处理的 commit
    with driver.session(database=database) as session:
        result = session.run(
            "MATCH (r:Repository) RETURN r.last_processed_commit AS commit LIMIT 1"
        )
        record = result.single()
        last_commit = record["commit"] if record else ""
    
    if not last_commit:
        print("未找到上次处理的 commit，需要全量更新")
        return True, "", current_commit
    
    if last_commit == current_commit:
        print(f"已经是最新 commit ({current_commit[:8]})，无需更新")
        return False, last_commit, current_commit
    
    print(f"上次处理: {last_commit[:8]}, 当前: {current_commit[:8]}")
    return True, last_commit, current_commit


def incremental_update(
    driver: GraphDatabase.driver,
    build_dir: Path,
    repo_root: Path,
    database: str = NEO4J_DATABASE,
) -> bool:
    """
    执行增量更新。
    
    流程:
    1. 检查是否需要更新
    2. 获取变更文件列表
    3. 删除变更文件相关的旧节点和边
    4. 重新解析变更文件
    5. 写入新节点和边
    6. 更新 last_processed_commit
    
    返回是否成功。
    """
    from graph_builder import build_graph
    from neo4j_writer import update_repository_commit, write_graph
    from clangd_parser import parse_files_incremental
    
    print("=== 增量更新 ===")
    
    # 1. 检查是否需要更新
    need_update, last_commit, current_commit = should_update_graph(
        repo_root, driver, database
    )
    if not need_update:
        return True
    
    if not last_commit:
        print("无历史记录，建议先执行全量更新")
        return False
    
    # 2. 获取变更文件
    print(f"\n获取变更文件 (since {last_commit[:8]})...")
    changed_files = get_changed_files(repo_root, last_commit)
    deleted_files = get_deleted_files(repo_root, last_commit)
    
    # 过滤出源文件
    source_exts = {".c", ".cpp", ".cc", ".cxx"}
    changed_sources = {f for f in changed_files 
                      if Path(f).suffix.lower() in source_exts}
    deleted_sources = {f for f in deleted_files 
                      if Path(f).suffix.lower() in source_exts}
    
    print(f"  变更文件: {len(changed_sources)} 个")
    print(f"  删除文件: {len(deleted_sources)} 个")
    
    if not changed_sources and not deleted_sources:
        print("无 C/C++ 源文件变更")
        # 仍然更新 commit 记录
        with driver.session(database=database) as session:
            result = session.run(
                "MATCH (r:Repository) RETURN r.id AS id LIMIT 1"
            )
            record = result.single()
            if record:
                update_repository_commit(driver, record["id"], current_commit, database)
        return True
    
    # 3. 删除变更和删除文件的旧节点
    print("\n删除旧节点和边...")
    all_affected_files = changed_sources | deleted_sources
    total_stats = {"functions": 0, "classes": 0, "variables": 0, "file_deleted": 0}
    
    for file_path in all_affected_files:
        stats = delete_file_nodes_and_edges(driver, file_path, database)
        for key in total_stats:
            total_stats[key] += stats.get(key, 0)
    
    print(f"  删除: {total_stats['functions']} 函数, "
          f"{total_stats['classes']} 类, "
          f"{total_stats['variables']} 变量, "
          f"{total_stats['file_deleted']} 文件")
    
    # 4. 重新解析变更文件（不包括已删除的）
    if not changed_sources:
        print("无文件需要重新解析")
    else:
        print(f"\n重新解析 {len(changed_sources)} 个文件...")
        
        # 使用增量解析函数
        tu_results, var_refs_global = parse_files_incremental(
            build_dir,
            list(changed_sources),
            repo_root,
            delay_after_init=2.0,
            delay_between_files=0.05,
            collect_var_refs=True,
        )
        
        print(f"  解析完成，获得 {len(tu_results)} 个结果")
        
        if tu_results:
            # 5. 构建图并写入
            print("\n构建增量图...")
            # 注意：这里 build_graph 需要处理增量情况
            # 对于增量更新，我们只构建变更文件的图
            graph = build_graph(tu_results, str(repo_root), var_refs_global=var_refs_global)
            
            n_func = len(graph["nodes"].get("Function", []))
            n_class = len(graph["nodes"].get("Class", []))
            n_calls = len(graph["edges"].get("CALLS", []))
            print(f"  新增: {n_func} 函数, {n_class} 类, {n_calls} 调用边")
            
            print("\n写入 Neo4j...")
            write_graph(driver, graph)
            print("  写入完成")
    
    # 6. 更新 commit 记录
    with driver.session(database=database) as session:
        result = session.run(
            "MATCH (r:Repository) RETURN r.id AS id LIMIT 1"
        )
        record = result.single()
        if record:
            update_repository_commit(driver, record["id"], current_commit, database)
            print(f"\n已更新 last_processed_commit = {current_commit[:8]}")
    
    return True


def full_rebuild_required(
    driver: GraphDatabase.driver,
    build_dir: Path,
    repo_root: Path,
    database: str = NEO4J_DATABASE,
) -> bool:
    """
    检查是否需要全量重建（而非增量更新）。
    
    以下情况需要全量重建：
    1. Neo4j 中没有 Repository 节点
    2. compile_commands.json 结构发生大变化（如 CMake 目标增减）
    3. 上次更新是 long time ago，变更文件过多（如 >50%）
    """
    with driver.session(database=database) as session:
        # 检查是否有 Repository 节点
        result = session.run("MATCH (r:Repository) RETURN count(r) AS cnt")
        if result.single()["cnt"] == 0:
            print("无 Repository 节点，需要全量重建")
            return True
        
        # 检查是否有 Function 节点
        result = session.run("MATCH (f:Function) RETURN count(f) AS cnt")
        if result.single()["cnt"] == 0:
            print("无 Function 节点，需要全量重建")
            return True
    
    # 检查变更文件比例
    from src.neo4j_writer import get_head_commit
    current_commit = get_head_commit(repo_root)
    
    with driver.session(database=database) as session:
        result = session.run(
            "MATCH (r:Repository) RETURN r.last_processed_commit AS commit LIMIT 1"
        )
        record = result.single()
        last_commit = record["commit"] if record else ""
    
    if last_commit and current_commit:
        changed = get_changed_files(repo_root, last_commit)
        source_exts = {".c", ".cpp", ".cc", ".cxx"}
        changed_sources = {f for f in changed if Path(f).suffix.lower() in source_exts}
        
        # 获取总文件数
        with driver.session(database=database) as session:
            result = session.run("MATCH (f:File) RETURN count(f) AS cnt")
            total_files = result.single()["cnt"]
        
        if total_files > 0 and len(changed_sources) > total_files * 0.5:
            print(f"变更文件比例过高 ({len(changed_sources)}/{total_files})，建议全量重建")
            return True
    
    return False
