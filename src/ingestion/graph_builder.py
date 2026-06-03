"""
图组装器：将解析结果组装成完整的图结构（nodes + edges）。

核心原则：直接信任 clangd 的符号和调用关系，不在下游做去重或修正。
- clangd 的 documentSymbol 已经返回准确的符号列表（包含重载）
- call_resolver 已经做了精确的位置匹配
- 不需要按 (file_path, name) 去重（这会破坏重载函数）
- 不需要 "兜底" 关联同名类（会导致错误挂载）
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from .models import ClassSymbol, FileResult, FunctionSymbol, ResolvedCalls, VariableSymbol
from .control_flow_extractor import extract_all_control_flow
from .resource_lifecycle_extractor import extract_all_resource_lifecycle
from .param_flow_extractor import extract_all_param_flow

try:
    import community as community_louvain
    _LOUVAIN_AVAILABLE = True
except ImportError:
    _LOUVAIN_AVAILABLE = False

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
    refs_raw: list[tuple[str, str, int]] = []

    for fr in file_results:
        file_paths.add(fr.file_path)

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
                "param_count": f.param_count,
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

        # Variables
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
                if fid in func_by_id:
                    refs_raw.append((fid, var_id, line))

    # 跨文件 var_refs
    if var_refs_global:
        for func_id, var_id, line in var_refs_global:
            if func_id in func_by_id:
                refs_raw.append((func_id, var_id, line))

    # ---- 第二阶段：变量引用统一聚合 ----
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
        "ControlFlowBlock": [],
        "ResourceOperation": [],
    }

    edges: dict[str, list[tuple[str, str, dict[str, Any]]]] = {
        "CONTAINS": [],
        "CALLS": [],
        "CALLS_AMBIGUOUS": [],
        "REFERENCES_VAR": [],
        "HAS_MEMBER": [],
        "HAS_METHOD": [],
        "EXTERNAL_CALLS": [],
        "CONTROL_FLOW": [],
        "MANAGES": [],
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
        suffix = Path(fp).suffix.lstrip(".") or "cpp"
        if suffix == "h":
            suffix = "cpp"
        nodes["File"].append({
            "id": file_id,
            "path": fp,
            "name": _file_name(fp),
            "language": suffix,
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
        parent_class = f.get("parent_class")
        if parent_class:
            # 仅在精确匹配时建立 HAS_METHOD
            # 不再兜底关联全局同名第一个类
            class_candidates = [c for c in classes if c["name"] == parent_class and c["file_path"] == f["file_path"]]
            if class_candidates:
                edges["HAS_METHOD"].append((class_candidates[0]["id"], f["id"], {}))

    for c in classes:
        edges["CONTAINS"].append((c["file_path"], c["id"], {}))

    for v in variables_by_id.values():
        edges["CONTAINS"].append((v["parent_id"], v["id"], {}))
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
    valid_var_ids = set(variables_by_id.keys())
    for (func_id, var_id), lines in refs_agg.items():
        if func_id in func_by_id and var_id in valid_var_ids:
            edges["REFERENCES_VAR"].append((func_id, var_id, {"lines": sorted(set(lines))}))

    # CALLS 边
    seen_calls = set()
    for caller_id, callee_id in resolved_calls.calls:
        if caller_id == callee_id:
            continue
        call_key = (caller_id, callee_id)
        if call_key not in seen_calls:
            edges["CALLS"].append((caller_id, callee_id, {}))
            seen_calls.add(call_key)

    # CALLS_AMBIGUOUS 边
    for caller_id, callee_name, candidate_ids in resolved_calls.ambiguous:
        if len(candidate_ids) == 1:
            # 只剩一个候选，升级为 CALLS
            callee_cid = candidate_ids[0]
            if caller_id != callee_cid:
                call_key = (caller_id, callee_cid)
                if call_key not in seen_calls:
                    edges["CALLS"].append((caller_id, callee_cid, {}))
                    seen_calls.add(call_key)
        else:
            edges["CALLS_AMBIGUOUS"].append((caller_id, f"ambiguous:{callee_name}", {
                "callee_name": callee_name,
                "candidates": candidate_ids,
            }))

    # EXTERNAL_CALLS 边
    seen_external = set()
    external_nodes: list[dict[str, Any]] = []
    for caller_id, callee_name in resolved_calls.external_calls:
        ext_key = (caller_id, callee_name)
        if ext_key not in seen_external:
            ext_id = f"external:{callee_name}"
            external_nodes.append({
                "id": ext_id,
                "name": callee_name,
                "kind": "external",
            })
            edges["EXTERNAL_CALLS"].append((caller_id, ext_id, {
                "callee_name": callee_name,
            }))
            seen_external.add(ext_key)
    nodes["ExternalCall"] = external_nodes

    # CALLS_AMBIGUOUS 的占位节点
    ambiguous_nodes: list[dict[str, Any]] = []
    seen_ambiguous = set()
    for caller_id, callee_name, candidate_ids in resolved_calls.ambiguous:
        amb_id = f"ambiguous:{callee_name}"
        if amb_id not in seen_ambiguous:
            ambiguous_nodes.append({
                "id": amb_id,
                "name": callee_name,
                "kind": "ambiguous",
                "candidates": candidate_ids,
            })
            seen_ambiguous.add(amb_id)
    nodes["AmbiguousCall"] = ambiguous_nodes

    # Module 检测（可选）
    if _LOUVAIN_AVAILABLE:
        _build_module_nodes(nodes, edges, functions, seen_calls)

    # 辅助提取器（可选，数据质量较低，仅供诊断）
    _run_optional_extractors(nodes, edges, func_by_id, file_results, repo_root)

    logger.info(
        "Graph assembled: %d functions, %d classes, %d variables, %d attributes, "
        "%d calls, %d ambiguous, %d unresolved, %d external, %d control_flow, %d resource_ops, %d modules",
        len(functions), len(classes), len(variables_list), len(nodes["Attribute"]),
        len(resolved_calls.calls), len(resolved_calls.ambiguous), len(resolved_calls.unresolved),
        len(resolved_calls.external_calls), len(nodes.get("ControlFlowBlock", [])),
        len(nodes.get("ResourceOperation", [])),
        len(nodes.get("Module", [])),
    )

    return {"nodes": nodes, "edges": edges}


def _run_optional_extractors(
    nodes: dict[str, list[dict[str, Any]]],
    edges: dict[str, list[tuple[str, str, dict[str, Any]]]],
    func_by_id: dict[str, dict[str, Any]],
    file_results: list[FileResult],
    repo_root: str,
) -> None:
    """
    运行可选的辅助提取器（控制流、参数流、资源生命周期）。

    这些提取器基于正则，数据质量低于 clangd 直接提供的信息，
    仅作为补充，不用于核心调用图构建。
    """
    try:
        cf_blocks = extract_all_control_flow(file_results, repo_root=repo_root)
        existing_cf_ids: set[str] = set()
        for block in cf_blocks:
            cf_id = block.id
            if cf_id in existing_cf_ids:
                continue
            existing_cf_ids.add(cf_id)
            cf_node = {
                "id": cf_id,
                "type": block.type,
                "condition": block.condition,
                "file_path": block.file_path,
                "line": block.line,
                "is_error_path": block.is_error_path,
                "semantic_type": block.semantic_type,
                "multi_line": block.multi_line,
                "full_condition": block.full_condition,
            }
            nodes["ControlFlowBlock"].append(cf_node)
            edges["CONTROL_FLOW"].append((block.function_id, cf_id, {
                "type": block.type,
                "line": block.line,
            }))
    except Exception as e:
        logger.warning("Control flow extraction skipped: %s", e)

    try:
        param_flows = extract_all_param_flow(file_results, repo_root=repo_root)
        for func_id, flow in param_flows.items():
            if func_id in func_by_id:
                func_by_id[func_id]["param_usage_json"] = json.dumps(flow, ensure_ascii=False)
    except Exception as e:
        logger.warning("Param flow extraction skipped: %s", e)

    try:
        resource_ops = extract_all_resource_lifecycle(file_results, repo_root=repo_root)
        for op in resource_ops:
            op_node = {
                "id": op.id,
                "type": op.type,
                "resource_type": op.resource_type,
                "file_path": op.file_path,
                "line": op.line,
                "variable_name": op.variable_name,
                "paired_operation_id": op.paired_operation_id,
            }
            nodes["ResourceOperation"].append(op_node)
            edges["MANAGES"].append((op.function_id, op.id, {
                "operation": op.type,
                "line": op.line,
            }))
    except Exception as e:
        logger.warning("Resource lifecycle extraction skipped: %s", e)


def _build_module_nodes(
    nodes: dict[str, list[dict[str, Any]]],
    edges: dict[str, list[tuple[str, str, dict[str, Any]]]],
    functions: list[dict[str, Any]],
    seen_calls: set[tuple[str, str]],
) -> None:
    """
    使用 Louvain 算法对函数调用图进行社区发现，构建 Module 节点。
    """
    import networkx as nx

    func_ids = {f["id"] for f in functions}
    if len(func_ids) < 10:
        return

    G = nx.Graph()
    for f in functions:
        G.add_node(f["id"], name=f["name"], file_path=f.get("file_path", ""))

    for caller_id, callee_id, _ in edges.get("CALLS", []):
        if caller_id in func_ids and callee_id in func_ids and caller_id != callee_id:
            if G.has_edge(caller_id, callee_id):
                G[caller_id][callee_id]["weight"] += 1.0
            else:
                G.add_edge(caller_id, callee_id, weight=1.0)

    file_funcs: dict[str, list[str]] = {}
    for f in functions:
        file_funcs.setdefault(f.get("file_path", ""), []).append(f["id"])
    for fp, fids in file_funcs.items():
        if len(fids) < 2:
            continue
        for i in range(len(fids)):
            for j in range(i + 1, len(fids)):
                a, b = fids[i], fids[j]
                if G.has_edge(a, b):
                    G[a][b]["weight"] += 0.5
                else:
                    G.add_edge(a, b, weight=0.5)

    if G.number_of_nodes() < 10 or G.number_of_edges() < 5:
        logger.info("Module detection skipped: graph too small (%d nodes, %d edges)",
                     G.number_of_nodes(), G.number_of_edges())
        return

    try:
        partition = community_louvain.best_partition(G, weight="weight", resolution=0.3)
    except Exception as e:
        logger.warning("Louvain community detection failed: %s", e)
        return

    communities: dict[int, list[str]] = {}
    for func_id, comm_id in partition.items():
        communities.setdefault(comm_id, []).append(func_id)

    large_communities = {cid: members for cid, members in communities.items() if len(members) >= 10}
    small_communities = {cid: members for cid, members in communities.items() if len(members) < 10}

    if large_communities:
        for small_cid, small_members in small_communities.items():
            best_large_cid = None
            best_score = -1
            for large_cid, large_members in large_communities.items():
                score = sum(1 for m in small_members for lm in large_members if G.has_edge(m, lm))
                if score > best_score:
                    best_score = score
                    best_large_cid = large_cid
            if best_large_cid is None or best_score == 0:
                best_large_cid = max(large_communities, key=lambda c: len(large_communities[c]))
            large_communities[best_large_cid].extend(small_members)
        communities = large_communities

    modules: list[dict[str, Any]] = []
    func_to_module: dict[str, str] = {}

    for idx, (comm_id, func_ids_in_comm) in enumerate(communities.items()):
        module_id = f"module:{idx}"
        module_name = _infer_module_name(func_ids_in_comm, G, functions)
        existing_names = {m["name"] for m in modules}
        if module_name in existing_names:
            module_name = f"{module_name}_{idx}"
        file_paths = list(dict.fromkeys(
            G.nodes[fid].get("file_path", "") for fid in func_ids_in_comm
            if G.nodes[fid].get("file_path")
        ))

        modules.append({
            "id": module_id,
            "name": module_name,
            "function_count": len(func_ids_in_comm),
            "files": file_paths,
        })

        for fid in func_ids_in_comm:
            func_to_module[fid] = module_id

    nodes.setdefault("Module", []).extend(modules)

    edges.setdefault("BELONGS_TO", [])
    for fid, module_id in func_to_module.items():
        edges["BELONGS_TO"].append((fid, module_id, {}))

    module_calls: dict[tuple[str, str], int] = {}
    for caller_id, callee_id, _ in edges.get("CALLS", []):
        caller_mod = func_to_module.get(caller_id)
        callee_mod = func_to_module.get(callee_id)
        if caller_mod and callee_mod and caller_mod != callee_mod:
            key = (caller_mod, callee_mod)
            module_calls[key] = module_calls.get(key, 0) + 1

    edges.setdefault("MODULE_CALLS", [])
    for (m1, m2), weight in module_calls.items():
        edges["MODULE_CALLS"].append((m1, m2, {"weight": weight}))

    logger.info(
        "Module detection: %d modules, %d functions assigned, %d inter-module calls",
        len(modules), len(func_to_module), len(module_calls)
    )


def _infer_module_name(func_ids: list[str], G: Any, functions: list[dict[str, Any]]) -> str:
    """推断模块名称。"""
    file_paths = [G.nodes[fid].get("file_path", "") for fid in func_ids if G.nodes[fid].get("file_path")]

    if file_paths:
        dirs = [os.path.dirname(fp).replace("\\", "/") for fp in file_paths]
        common_dir = os.path.commonprefix(dirs)
        if common_dir:
            common_dir = common_dir.rstrip("/")
        if not common_dir:
            from collections import Counter
            common_dir = Counter(dirs).most_common(1)[0][0]

        if common_dir:
            parts = common_dir.split("/")
            if parts and parts[-1]:
                return f"mod_{parts[-1]}"

    degrees = {fid: G.degree(fid) for fid in func_ids if fid in G}
    if degrees:
        best_fid = max(degrees, key=degrees.get)
        best_name = G.nodes[best_fid].get("name", "")
        if best_name:
            return f"mod_{best_name}"

    return f"module_{func_ids[0].split(':')[-1] if func_ids else 'unknown'}"
