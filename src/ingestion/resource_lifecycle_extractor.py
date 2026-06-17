"""
资源生命周期提取器：从 C++ 函数体中提取 new/delete, malloc/free, RAII, throw 等资源操作。

用于覆盖 resource_lifecycle 类证据。
策略：基于正则启发式提取，无需 AST 解析器。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ResourceOperation:
    """资源操作节点。"""
    id: str
    function_id: str
    file_path: str
    line: int
    type: str  # "allocate" | "release" | "raii_guard" | "throw"
    resource_type: str  # "memory" | "mutex" | "gpu_context" | "exception" 等
    variable_name: str = ""  # 关联的变量名（用于配对）
    paired_operation_id: str = ""  # 配对的 operation id


# 分配模式
_ALLOCATE_PATTERNS = [
    # new / new[]
    (re.compile(r'\b(new)\s+(?:const\s+)?\w+'), "memory", "new"),
    # malloc / calloc / realloc
    (re.compile(r'\b(malloc|calloc|realloc)\s*\('), "memory", "malloc"),
    # std::make_unique / std::make_shared
    (re.compile(r'\b(std::make_unique|std::make_shared)\s*<'), "memory", "smart_ptr"),
    # 外部创建 API（CreateInstance, CreateBuffer, InitContext 等）
    (re.compile(r'\b(\w*(?:Create|Init|Acquire|Alloc|Open)\w*)\s*\('), "external", "api_create"),
    # 工厂函数模式：xxx_create(...)
    (re.compile(r'\b(\w+_create)\s*\('), "external", "factory"),
]

# 释放模式
_RELEASE_PATTERNS = [
    # delete / delete[]
    (re.compile(r'\b(delete)(?:\s*\[\s*\])?\s+'), "memory", "delete"),
    # free
    (re.compile(r'\b(free)\s*\('), "memory", "free"),
    # 外部释放 API（Destroy, Release, Close, Cleanup 等）
    (re.compile(r'\b(\w*(?:Destroy|Release|Close|Cleanup|Deinit|Free)\w*)\s*\('), "external", "api_release"),
    # 句柄重置
    (re.compile(r'\b(\w+)\s*=\s*(?:nullptr|NULL|0)\s*;'), "handle_reset", "reset"),
]

# RAII 模式
_RAII_PATTERNS = [
    (re.compile(r'\b(std::lock_guard|std::unique_lock|std::scoped_lock)\s*<'), "mutex", "lock"),
    (re.compile(r'\b(std::unique_ptr|std::shared_ptr|std::weak_ptr)\s*<'), "memory", "smart_ptr"),
]

# 异常模式
_THROW_PATTERN = re.compile(r'\b(throw)\s+')

# 变量名提取（简单启发式）
# 从 "Type * var = new Type(...)" 或 "auto var = ..." 或 "Type var(args);" 中提取 var
_VAR_ASSIGN_RE = re.compile(
    r'(?:\w+\s*(?:<[^>]+>)?\s*[*&]*\s+)?'  # 类型（可选）
    r'(\w+)\s*'  # 变量名
    r'(?:=|\{|\()',
)


def _extract_variable_name(line: str, default: str = "") -> str:
    """从赋值语句中提取变量名。"""
    m = _VAR_ASSIGN_RE.search(line)
    if m:
        return m.group(1)
    return default


def _detect_resource_type(line: str, operation_type: str) -> str:
    """根据上下文推断资源类型。"""
    line_lower = line.lower()
    
    if operation_type == "raii_guard":
        if "lock" in line_lower:
            return "mutex"
        if "ptr" in line_lower:
            return "memory"
        return "raii"
    
    if operation_type == "throw":
        return "exception"
    
    # GPU / 设备相关
    if any(kw in line_lower for kw in ['gpu', 'cuda', 'vulkan', 'webgpu', 'sycl', 'device', 'context', 'ctx']):
        return "gpu_context"
    
    # 文件 / 句柄
    if any(kw in line_lower for kw in ['file', 'fd', 'socket', 'sock', 'handle']):
        return "io_handle"
    
    # 内存
    if operation_type in ("allocate", "release"):
        if any(kw in line_lower for kw in ['malloc', 'free', 'new', 'delete', 'mem', 'make_unique', 'make_shared']):
            return "memory"
    
    return "generic"


def extract_resource_lifecycle_for_function(
    function_id: str,
    file_path: str,
    start_line: int,
    end_line: int,
    file_lines: list[str],
) -> list[ResourceOperation]:
    """
    从单个函数体中提取资源生命周期操作。

    Args:
        function_id: 函数节点 ID
        file_path: 相对路径
        start_line: 函数开始行（1-based，含）
        end_line: 函数结束行（1-based，含）
        file_lines: 整个文件的所有行（0-based 索引）

    Returns:
        ResourceOperation 列表
    """
    operations: list[ResourceOperation] = []
    start_idx = max(0, start_line - 1)
    end_idx = min(len(file_lines), end_line)

    for line_idx in range(start_idx, end_idx):
        line = file_lines[line_idx]
        line_no = line_idx + 1

        # 跳过空行和注释行
        stripped = line.strip()
        if not stripped or stripped.startswith('//') or stripped.startswith('*'):
            continue

        # 去掉行内注释
        code_only = re.sub(r'//.*$', '', line)

        # 1. 检测分配
        for pattern, default_type, op_name in _ALLOCATE_PATTERNS:
            m = pattern.search(code_only)
            if m:
                var_name = _extract_variable_name(code_only)
                res_type = _detect_resource_type(code_only, "allocate")
                operations.append(ResourceOperation(
                    id=f"{file_path}:{start_line}:{line_no}:alloc:{op_name}",
                    function_id=function_id,
                    file_path=file_path,
                    line=line_no,
                    type="allocate",
                    resource_type=res_type,
                    variable_name=var_name,
                ))
                break  # 一行只匹配一个模式
        else:
            # 2. 检测释放（如果分配没匹配）
            for pattern, default_type, op_name in _RELEASE_PATTERNS:
                m = pattern.search(code_only)
                if m:
                    var_name = ""
                    if op_name == "reset":
                        var_name = m.group(1)
                    else:
                        # 尝试从 "delete var;" 或 "free(var);" 中提取变量名
                        after_op = code_only[m.end():].strip()
                        # delete var;
                        del_m = re.match(r'(\w+)', after_op)
                        if del_m:
                            var_name = del_m.group(1)
                    res_type = _detect_resource_type(code_only, "release")
                    operations.append(ResourceOperation(
                        id=f"{file_path}:{start_line}:{line_no}:release:{op_name}",
                        function_id=function_id,
                        file_path=file_path,
                        line=line_no,
                        type="release",
                        resource_type=res_type,
                        variable_name=var_name,
                    ))
                    break
            else:
                # 3. 检测 RAII
                for pattern, default_type, op_name in _RAII_PATTERNS:
                    m = pattern.search(code_only)
                    if m:
                        var_name = _extract_variable_name(code_only)
                        operations.append(ResourceOperation(
                            id=f"{file_path}:{start_line}:{line_no}:raii:{op_name}",
                            function_id=function_id,
                            file_path=file_path,
                            line=line_no,
                            type="raii_guard",
                            resource_type=default_type,
                            variable_name=var_name,
                        ))
                        break
                else:
                    # 4. 检测 throw
                    if _THROW_PATTERN.search(code_only):
                        operations.append(ResourceOperation(
                            id=f"{file_path}:{start_line}:{line_no}:throw",
                            function_id=function_id,
                            file_path=file_path,
                            line=line_no,
                            type="throw",
                            resource_type="exception",
                        ))

    # 配对逻辑：在同一函数内，按变量名匹配分配和释放
    allocates = [op for op in operations if op.type == "allocate"]
    releases = [op for op in operations if op.type == "release"]
    
    for alloc_op in allocates:
        if alloc_op.variable_name:
            # 查找同名的释放操作（在分配之后）
            for rel_op in releases:
                if rel_op.variable_name == alloc_op.variable_name and rel_op.line > alloc_op.line:
                    alloc_op.paired_operation_id = rel_op.id
                    rel_op.paired_operation_id = alloc_op.id
                    break

    return operations


def extract_all_resource_lifecycle(
    file_results: list[Any],
    repo_root: str = "",
) -> list[ResourceOperation]:
    """
    从所有文件结果中提取资源生命周期操作。

    Args:
        file_results: FileResult 列表
        repo_root: 仓库根目录（用于读取源码）

    Returns:
        全局 ResourceOperation 列表
    """
    from pathlib import Path

    all_operations: list[ResourceOperation] = []

    for fr in file_results:
        if not fr.functions:
            continue

        abs_path = Path(repo_root) / fr.file_path if repo_root else Path(fr.file_path)
        try:
            lines = abs_path.read_text(encoding='utf-8', errors='replace').splitlines()
        except Exception:
            continue

        for func in fr.functions:
            ops = extract_resource_lifecycle_for_function(
                function_id=func.id or f"{func.file_path}:{func.name}:{func.start_line}",
                file_path=func.file_path,
                start_line=func.start_line,
                end_line=func.end_line,
                file_lines=lines,
            )
            all_operations.extend(ops)

    logger.info("Resource lifecycle extraction: %d operations from %d files",
                len(all_operations), len(file_results))
    return all_operations
