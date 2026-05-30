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
import os
from pathlib import Path
from typing import Any

from .models import ClassSymbol, FileResult, FunctionSymbol, ResolvedCalls, VariableSymbol
from .control_flow_extractor import ControlFlowBlock, extract_all_control_flow

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


def _deduplicate_functions(
    file_results: list[FileResult],
) -> tuple[list[FileResult], dict[str, str]]:
    """
    同名同文件函数去重。

    策略：按 (file_path, name) 分组，每组保留 body 最长的（行数最多）
    作为定义节点。如果是声明-only 的组，保留第一个。

    Returns:
        (去重后的 file_results, old_func_id -> new_func_id 映射)
    """
    from collections import defaultdict

    # 按 (file_path, name) 分组
    groups: dict[tuple[str, str], list[FunctionSymbol]] = defaultdict(list)
    for fr in file_results:
        for f in fr.functions:
            groups[(f.file_path, f.name)].append(f)

    # 选择代表 + 构建映射
    old_to_new: dict[str, str] = {}
    representatives: dict[tuple[str, str], FunctionSymbol] = {}

    for key, funcs in groups.items():
        if len(funcs) == 1:
            representatives[key] = funcs[0]
            continue

        # 优先保留 is_definition=True 的
        defs = [f for f in funcs if f.is_definition]
        candidates = defs if defs else funcs

        # 在候选中选行数最多的（body 最长）
        best = max(candidates, key=lambda f: (f.end_line - f.start_line, f.start_line))
        representatives[key] = best

    # 构建 old -> new 映射
    for key, funcs in groups.items():
        rep = representatives[key]
        for f in funcs:
            if f.id != rep.id:
                old_to_new[f.id] = rep.id

    # 更新 file_results：替换函数列表
    new_file_results: list[FileResult] = []
    for fr in file_results:
        new_funcs = []
        seen_ids = set()
        for f in fr.functions:
            key = (f.file_path, f.name)
            rep = representatives[key]
            if rep.id not in seen_ids:
                new_funcs.append(rep)
                seen_ids.add(rep.id)
        new_file_results.append(FileResult(
            file_path=fr.file_path,
            functions=new_funcs,
            classes=fr.classes,
            variables=fr.variables,
            calls=fr.calls,
            var_refs=fr.var_refs,
            raw=fr.raw,
        ))

    return new_file_results, old_to_new


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
    # ---- P0: 去重 ----
    file_results, id_remap = _deduplicate_functions(file_results)

    def remap_id(fid: str) -> str:
        return id_remap.get(fid, fid)

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
                parent_id = remap_id(parent_id)
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
                fid = remap_id(fid)
                if fid in func_by_id:
                    refs_raw.append((fid, var_id, line))

    # 跨文件 var_refs（应用去重映射，过滤无效引用）
    if var_refs_global:
        for func_id, var_id, line in var_refs_global:
            new_fid = remap_id(func_id)
            # 延迟检查：在 func_by_id 构建完成后再验证
            refs_raw.append((new_fid, var_id, line))

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
        "ControlFlowBlock": [],
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

    # REFERENCES_VAR 边（过滤无效引用：函数或变量已被删除）
    valid_var_ids = set(variables_by_id.keys())
    for (func_id, var_id), lines in refs_agg.items():
        if func_id in func_by_id and var_id in valid_var_ids:
            edges["REFERENCES_VAR"].append((func_id, var_id, {"lines": sorted(set(lines))}))

    # CALLS 边（使用 call_resolver 的输出，应用去重映射）
    seen_calls = set()
    for caller_id, callee_id in resolved_calls.calls:
        new_caller = remap_id(caller_id)
        new_callee = remap_id(callee_id)
        if new_caller == new_callee:
            continue
        call_key = (new_caller, new_callee)
        if call_key not in seen_calls:
            edges["CALLS"].append((new_caller, new_callee, {}))
            seen_calls.add(call_key)

    # CALLS_AMBIGUOUS 边（去重后若只剩一个候选，升级为 CALLS）
    for caller_id, callee_name, candidate_ids in resolved_calls.ambiguous:
        new_caller = remap_id(caller_id)
        # 去重映射 + 去重
        seen = set()
        remapped_candidates = []
        for cid in candidate_ids:
            new_cid = remap_id(cid)
            if new_cid not in seen:
                seen.add(new_cid)
                remapped_candidates.append(new_cid)

        if len(remapped_candidates) == 1:
            # 去重后唯一，升级为 CALLS
            callee_cid = remapped_candidates[0]
            if new_caller != callee_cid:
                call_key = (new_caller, callee_cid)
                if call_key not in seen_calls:
                    edges["CALLS"].append((new_caller, callee_cid, {}))
                    seen_calls.add(call_key)
        else:
            edges["CALLS_AMBIGUOUS"].append((new_caller, f"ambiguous:{callee_name}", {
                "callee_name": callee_name,
                "candidates": remapped_candidates,
            }))

    # ---- EXTERNAL_CALLS 边（外部库/系统调用）----
    seen_external = set()
    for caller_id, callee_name in resolved_calls.external_calls:
        new_caller = remap_id(caller_id)
        ext_key = (new_caller, callee_name)
        if ext_key not in seen_external:
            edges["EXTERNAL_CALLS"].append((new_caller, f"external:{callee_name}", {
                "callee_name": callee_name,
            }))
            seen_external.add(ext_key)

    # ---- P2: Louvain 社区发现 + Module 节点 ----
    if _LOUVAIN_AVAILABLE:
        _build_module_nodes(nodes, edges, functions, seen_calls)

    # ---- P3: 控制流提取（state_control, error_path 等证据）----
    cf_blocks = extract_all_control_flow(file_results, repo_root=repo_root)
    for block in cf_blocks:
        cf_id = block.id
        # 去重：同一函数同一行同一类型只保留一个
        cf_node = {
            "id": cf_id,
            "type": block.type,
            "condition": block.condition,
            "file_path": block.file_path,
            "line": block.line,
            "is_error_path": block.is_error_path,
        }
        # 检查是否已存在（同一位置可能多个函数重叠）
        existing_ids = {n["id"] for n in nodes["ControlFlowBlock"]}
        if cf_id not in existing_ids:
            nodes["ControlFlowBlock"].append(cf_node)
            edges["CONTROL_FLOW"].append((block.function_id, cf_id, {
                "type": block.type,
                "line": block.line,
            }))

    logger.info(
        "Graph assembled: %d functions, %d classes, %d variables, %d attributes, "
        "%d calls, %d ambiguous, %d unresolved, %d external, %d control_flow, %d modules",
        len(functions), len(classes), len(variables_list), len(nodes["Attribute"]),
        len(resolved_calls.calls), len(resolved_calls.ambiguous), len(resolved_calls.unresolved),
        len(resolved_calls.external_calls), len(nodes["ControlFlowBlock"]),
        len(nodes.get("Module", [])),
    )

    return {"nodes": nodes, "edges": edges}


def _build_module_nodes(
    nodes: dict[str, list[dict[str, Any]]],
    edges: dict[str, list[tuple[str, str, dict[str, Any]]]],
    functions: list[dict[str, Any]],
    seen_calls: set[tuple[str, str]],
) -> None:
    """
    使用 Louvain 算法对函数调用图进行社区发现，构建 Module 节点。

    策略：
    1. 基于 CALLS 边 + 文件共现构建加权图
    2. 转为无向图，运行 Louvain 社区发现（低 resolution 产生大社区）
    3. 过滤小社区，合并到最近的大社区
    4. 为每个社区创建 Module 节点
    5. 构建 BELONGS_TO 边和 MODULE_CALLS 边
    """
    import networkx as nx

    func_ids = {f["id"] for f in functions}
    if len(func_ids) < 10:
        return

    # 1. 构建加权无向图
    G = nx.Graph()
    for f in functions:
        G.add_node(f["id"], name=f["name"], file_path=f.get("file_path", ""))

    # CALLS 边权重 = 1.0
    for caller_id, callee_id, _ in edges.get("CALLS", []):
        if caller_id in func_ids and callee_id in func_ids and caller_id != callee_id:
            if G.has_edge(caller_id, callee_id):
                G[caller_id][callee_id]["weight"] += 1.0
            else:
                G.add_edge(caller_id, callee_id, weight=1.0)

    # 文件共现边：同一文件中的函数权重 = 0.5
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

    # 2. Louvain 社区发现（低 resolution 产生更大社区）
    try:
        partition = community_louvain.best_partition(G, weight="weight", resolution=0.3)
    except Exception as e:
        logger.warning("Louvain community detection failed: %s", e)
        return

    # 3. 合并小社区（< 10 个函数）到最近的大社区
    communities: dict[int, list[str]] = {}
    for func_id, comm_id in partition.items():
        communities.setdefault(comm_id, []).append(func_id)

    # 找出大社区（>= 10 个函数）
    large_communities = {cid: members for cid, members in communities.items() if len(members) >= 10}
    small_communities = {cid: members for cid, members in communities.items() if len(members) < 10}

    if large_communities:
        # 为每个小社区找到最相似的大社区（共享边最多）
        for small_cid, small_members in small_communities.items():
            best_large_cid = None
            best_score = -1
            for large_cid, large_members in large_communities.items():
                # 计算小社区和大社区之间的连接数
                score = sum(1 for m in small_members for lm in large_members if G.has_edge(m, lm))
                if score > best_score:
                    best_score = score
                    best_large_cid = large_cid
            # 如果没有任何连接，合并到最大的社区
            if best_large_cid is None or best_score == 0:
                best_large_cid = max(large_communities, key=lambda c: len(large_communities[c]))
            # 合并
            large_communities[best_large_cid].extend(small_members)

        communities = large_communities
    else:
        # 如果没有大社区，保留所有社区
        pass

    # 4. 为每个社区创建 Module 节点
    modules: list[dict[str, Any]] = []
    func_to_module: dict[str, str] = {}

    for idx, (comm_id, func_ids_in_comm) in enumerate(communities.items()):
        module_id = f"module:{idx}"
        module_name = _infer_module_name(func_ids_in_comm, G, functions)
        # 确保模块名唯一
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

    # 4. BELONGS_TO 边
    edges.setdefault("BELONGS_TO", [])
    for fid, module_id in func_to_module.items():
        edges["BELONGS_TO"].append((fid, module_id, {}))

    # 5. MODULE_CALLS 边（模块间调用）
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
    """
    推断模块名称。

    策略（按优先级）：
    1. 基于文件路径的公共目录
    2. 基于社区中度数最高的函数名
    3. 兜底: module_{id}
    """
    # 收集文件路径
    file_paths = [G.nodes[fid].get("file_path", "") for fid in func_ids if G.nodes[fid].get("file_path")]

    # 策略1: 公共目录
    if file_paths:
        # 统一为目录列表
        dirs = [os.path.dirname(fp).replace("\\", "/") for fp in file_paths]
        # 找公共目录前缀
        common_dir = os.path.commonprefix(dirs)
        if common_dir:
            common_dir = common_dir.rstrip("/")
        # 如果公共前缀为空，找最频繁的目录
        if not common_dir:
            from collections import Counter
            common_dir = Counter(dirs).most_common(1)[0][0]

        if common_dir:
            parts = common_dir.split("/")
            if parts and parts[-1]:
                return f"mod_{parts[-1]}"

    # 策略2: 度数最高的函数名
    degrees = {fid: G.degree(fid) for fid in func_ids if fid in G}
    if degrees:
        best_fid = max(degrees, key=degrees.get)
        best_name = G.nodes[best_fid].get("name", "")
        if best_name:
            return f"mod_{best_name}"

    # 兜底
    return f"module_{func_ids[0].split(':')[-1] if func_ids else 'unknown'}"
