"""
图组装器：将解析结果组装成完整的图结构（nodes + edges）。

改进点（相比原 graph_builder.py）：
1. 两阶段变量引用：先收集所有 Function/Class，再统一处理 var_refs，消除时序盲区
2. 直接使用 ResolvedCalls，不再自己做 CALLS 匹配
3. 支持 CALLS_AMBIGUOUS 边（诊断用途）
4. 保持方案 B：Repository/Directory/File/Function/Class/Variable/Attribute 节点
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .models import ClassSymbol, FileResult, FunctionSymbol, ResolvedCalls, VariableSymbol

logger = logging.getLogger(__name__)


def _file_id(path: str) -> str:
    return path


def _func_id(file_path: str, name: str, start_line: int) -> str:
    return f"{file_path}:{name}:{start_line}"


def _class_id(file_path: str, name: str, start_line: int) -> str:
    return f"{file_path}:{name}:{start_line}"


def _dir_paths_from_file_paths(file_paths: list[str]) -> set[str]:
    out: set[str] = set()
    for fp in file_paths:
        parts = Path(fp).parts
        for i in range(len(parts) - 1):
            d = str(Path(*parts[: i + 1]))
            out.add(d)
    return out


def _parent_dir(path: str) -> str | None:
    p = Path(path)
    parent = p.parent
    if str(parent) == "." or not str(parent):
        return None
    return str(parent)


def _file_name(path: str) -> str:
    return Path(path).name


def assemble_graph(
    file_results: list[FileResult],
    resolved_calls: ResolvedCalls,
    repo_root: str = "",
    var_refs_global: list[tuple[str, str, int]] | None = None,
) -> dict[str, Any]:
    """
    组装图结构。

    Args:
        file_results: 所有文件的解析结果
        resolved_calls: call_resolver 解析后的调用关系
        repo_root: 仓库根目录
        var_refs_global: 可选的跨文件变量引用 (func_id, var_id, line)

    Returns:
        {"nodes": {...}, "edges": {...}}
    """
    # ---- 第一阶段：收集所有全局实体 ----
    file_paths: set[str] = set()
    functions: list[dict[str, Any]] = []
    classes: list[dict[str, Any]] = []
    variables_by_id: dict[str, dict[str, Any]] = {}
    refs_raw: list[tuple[str, str, int]] = []  # (func_id, var_id, line)

    # 收集所有文件路径
    for fr in file_results:
        file_paths.add(fr.file_path)

    # 函数和类的全局索引
    func_by_id: dict[str, dict[str, Any]] = {}
    class_by_id: dict[str, dict[str, Any]] = {}

    for fr in file_results:
        fp = fr.file_path

        # Functions
        for f in fr.functions:
            fid = f.id or _func_id(fp, f.name, f.start_line)
            func_node = {
                "id": fid,
                "name": f.name,
                "signature": f.signature,
                "file_path": fp,
                "start_line": f.start_line,
                "end_line": f.end_line,
                "parent_class": f.parent_class,
            }
            functions.append(func_node)
            func_by_id[fid] = func_node

        # Classes
        for c in fr.classes:
            cid = _class_id(fp, c.name, c.start_line)
            class_node = {
                "id": cid,
                "name": c.name,
                "file_path": fp,
                "start_line": c.start_line,
                "end_line": c.end_line,
            }
            classes.append(class_node)
            class_by_id[cid] = class_node

        # Variables（合并到全局，确定 parent）
        for v in fr.variables:
            if v.id in variables_by_id:
                continue
            sf = v.scope_function_index
            sc = v.scope_class_index
            if sf is not None and 0 <= sf < len(fr.functions):
                parent_type = "Function"
                parent_id = fr.functions[sf].id or _func_id(fp, fr.functions[sf].name, fr.functions[sf].start_line)
            elif sc is not None and 0 <= sc < len(fr.classes):
                parent_type = "Class"
                parent_id = _class_id(fp, fr.classes[sc].name, fr.classes[sc].start_line)
            else:
                parent_type = "File"
                parent_id = fp
            variables_by_id[v.id] = {
                **v.__dict__,
                "parent_type": parent_type,
                "parent_id": parent_id,
            }

        # var_refs（同文件内）
        for func_idx, var_id, line in fr.var_refs:
            if 0 <= func_idx < len(fr.functions):
                fid = fr.functions[func_idx].id or _func_id(fp, fr.functions[func_idx].name, fr.functions[func_idx].start_line)
                refs_raw.append((fid, var_id, line))

    # 跨文件 var_refs
    if var_refs_global:
        refs_raw.extend(var_refs_global)

    # ---- 第二阶段：变量引用统一聚合（消除时序盲区）----
    refs_agg: dict[tuple[str, str], list[int]] = {}
    for func_id, var_id, line in refs_raw:
        key = (func_id, var_id)
        if key not in refs_agg:
            refs_agg[key] = []
        refs_agg[key].append(line)

    # ---- 第三阶段：构建目录结构 ----
    all_dirs = _dir_paths_from_file_paths(list(file_paths))
    sorted_dirs = sorted(all_dirs, key=lambda d: (d.count("/"), d))

    # ---- 第四阶段：组装节点 ----
    variables_list: list[dict[str, Any]] = []
    for v in variables_by_id.values():
        variables_list.append({
            "id": v["id"],
            "name": v["name"],
            "file_path": v.get("file_path", ""),
            "start_line": v.get("start_line", 0),
            "kind": v.get("kind", "global"),
        })

    nodes: dict[str, list[dict[str, Any]]] = {
        "Repository": [],
        "Directory": [],
        "File": [],
        "Function": functions,
        "Class": classes,
        "Variable": variables_list,
        "Attribute": [],
    }

    edges: dict[str, list[tuple[str, str, dict[str, Any]]]] = {
        "CONTAINS": [],
        "CALLS": [],
        "CALLS_AMBIGUOUS": [],
        "REFERENCES_VAR": [],
        "HAS_MEMBER": [],
        "HAS_METHOD": [],
    }

    repo_id = "repo:1"
    nodes["Repository"].append({
        "id": repo_id,
        "root_path": repo_root,
        "last_processed_commit": "",
    })

    # Directory 节点和层级 CONTAINS
    dir_ids: dict[str, str] = {}
    for d in sorted_dirs:
        dir_id = f"dir:{d}"
        dir_ids[d] = dir_id
        name = Path(d).name if d else ""
        nodes["Directory"].append({"id": dir_id, "path": d, "name": name or d})
        parent = _parent_dir(d)
        if parent is None:
            edges["CONTAINS"].append((repo_id, dir_id, {}))
        else:
            parent_id = dir_ids.get(parent)
            if parent_id:
                edges["CONTAINS"].append((parent_id, dir_id, {}))

    # File 节点
    for fp in sorted(file_paths):
        file_id = _file_id(fp)
        nodes["File"].append({
            "id": file_id,
            "path": fp,
            "name": _file_name(fp),
            "language": Path(fp).suffix.lstrip(".") or "cpp",
        })
        parent = _parent_dir(fp)
        if parent and parent in dir_ids:
            edges["CONTAINS"].append((dir_ids[parent], file_id, {}))
        else:
            if "." in dir_ids:
                edges["CONTAINS"].append((dir_ids["."], file_id, {}))
            else:
                edges["CONTAINS"].append((repo_id, file_id, {}))

    # Function/Class/Variable 的 CONTAINS 边
    for f in functions:
        edges["CONTAINS"].append((f["file_path"], f["id"], {}))
        # Python: parent_class -> HAS_METHOD
        parent_class = f.get("parent_class")
        if parent_class:
            class_id = None
            for c in classes:
                if c["name"] == parent_class and c["file_path"] == f["file_path"]:
                    class_id = c["id"]
                    break
            if class_id:
                edges["HAS_METHOD"].append((class_id, f["id"], {}))

    for c in classes:
        edges["CONTAINS"].append((c["file_path"], c["id"], {}))

    for v in variables_by_id.values():
        edges["CONTAINS"].append((v["parent_id"], v["id"], {}))
        # Class 成员变量 -> Attribute 节点 + HAS_MEMBER 边
        if v.get("parent_type") == "Class" and v.get("kind") == "member":
            nodes["Attribute"].append({
                "id": v["id"],
                "name": v["name"],
                "file_path": v.get("file_path", ""),
                "start_line": v.get("start_line", 0),
                "member_of_class": v.get("parent_id"),
            })
            edges["HAS_MEMBER"].append((v["parent_id"], v["id"], {}))

    # REFERENCES_VAR 边
    for (func_id, var_id), lines in refs_agg.items():
        edges["REFERENCES_VAR"].append((func_id, var_id, {"lines": sorted(set(lines))}))

    # CALLS 边（使用 call_resolver 的输出）
    for caller_id, callee_id in resolved_calls.calls:
        edges["CALLS"].append((caller_id, callee_id, {}))

    # CALLS_AMBIGUOUS 边
    for caller_id, callee_name, candidate_ids in resolved_calls.ambiguous:
        edges["CALLS_AMBIGUOUS"].append((caller_id, f"ambiguous:{callee_name}", {
            "callee_name": callee_name,
            "candidates": candidate_ids,
        }))

    logger.info(
        "Graph assembled: %d functions, %d classes, %d variables, %d attributes, "
        "%d calls, %d ambiguous, %d unresolved",
        len(functions), len(classes), len(variables_list), len(nodes["Attribute"]),
        len(resolved_calls.calls), len(resolved_calls.ambiguous), len(resolved_calls.unresolved)
    )

    return {"nodes": nodes, "edges": edges}
