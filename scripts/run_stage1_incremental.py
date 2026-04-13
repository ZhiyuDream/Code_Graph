#!/usr/bin/env python3
"""
阶段 1（增量更新）：基于 git diff 只更新变更的文件。

对比全量更新：
- 全量更新：删除所有节点，重新解析所有文件
- 增量更新：只删除变更文件的节点，重新解析这些文件

使用场景：
- 仓库已有代码图，且上次处理过 commit 已知
- 只有少量文件变更（建议 <30% 总文件数）
- 需要快速更新（比全量节省大量时间）

限制：
- 如果变更文件过多（>50%），建议全量更新
- 如果 compile_commands.json 结构大变（如新增 CMake 目标），建议全量更新
- 跨文件调用：删除文件的调用边会被删除，但外部文件指向新函数的边需要重新解析外部文件才能建立
  （当前实现只保证变更文件自身的调用边准确）

用法：
  python run_stage1_incremental.py [--force-full]
  
  --force-full: 强制全量更新（即使有历史记录）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 确保 Code_Graph 目录在 path 中
_CODE_GRAPH = Path(__file__).resolve().parent.parent
if str(_CODE_GRAPH) not in sys.path:
    sys.path.insert(0, str(_CODE_GRAPH))

from config import get_compile_commands_path, get_repo_root, NEO4J_DATABASE
from src.neo4j_writer import get_driver
from src.incremental_updater import (
    incremental_update,
    full_rebuild_required,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="增量更新代码图（只处理变更文件）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 增量更新（默认）
  python run_stage1_incremental.py
  
  # 强制全量更新
  python run_stage1_incremental.py --force-full
        """
    )
    parser.add_argument(
        "--force-full",
        action="store_true",
        help="强制全量更新（忽略历史记录）"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只检查是否需要更新，不执行"
    )
    args = parser.parse_args()
    
    # 获取配置
    build_dir = get_compile_commands_path()
    if not build_dir:
        print("错误: 未找到 compile_commands.json。请设置 REPO_ROOT 或 COMPILE_COMMANDS_DIR。")
        return 1
    
    repo_root = get_repo_root()
    if not repo_root:
        print("错误: 未设置 REPO_ROOT")
        return 1
    
    # 连接 Neo4j
    driver = get_driver()
    try:
        driver.verify_connectivity()
    except Exception as e:
        print(f"错误: Neo4j 连接失败: {e}")
        return 1
    
    # 检查是否需要全量重建
    if not args.force_full:
        if full_rebuild_required(driver, build_dir, repo_root, NEO4J_DATABASE):
            print("\n检测到需要全量重建的情况。建议运行:")
            print("  python run_stage1.py")
            print("\n或者使用 --force-full 强制全量更新（会先清空现有数据）")
            driver.close()
            return 1
    
    if args.check:
        from src.incremental_updater import should_update_graph
        need_update, last_commit, current_commit = should_update_graph(
            repo_root, driver, NEO4J_DATABASE
        )
        if need_update:
            print(f"需要更新: {last_commit[:8] if last_commit else 'N/A'} -> {current_commit[:8]}")
        else:
            print("无需更新（已是最新）")
        driver.close()
        return 0
    
    # 执行增量更新
    try:
        if args.force_full:
            print("=== 强制全量更新 ===")
            print("注意：这将清空现有数据并重新解析所有文件\n")
            # 调用全量更新
            import subprocess
            result = subprocess.run(
                [sys.executable, str(_CODE_GRAPH / "scripts" / "run_stage1.py")],
                cwd=_CODE_GRAPH
            )
            return result.returncode
        
        success = incremental_update(driver, build_dir, repo_root, NEO4J_DATABASE)
        if success:
            print("\n✓ 增量更新完成")
            return 0
        else:
            print("\n✗ 增量更新失败，建议执行全量更新:")
            print("  python run_stage1.py")
            return 1
            
    finally:
        driver.close()


if __name__ == "__main__":
    sys.exit(main())
