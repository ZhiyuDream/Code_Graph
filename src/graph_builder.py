"""
从 AST 解析结果构建图结构：Repository、Directory、File、Function、Class 及 CONTAINS、CALLS。
方案 B：显式 Directory 与 File 节点。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def _file_id(path: str) -> str:
    return path


def _func_id(file_path: str, name: str, start_line: int) -> str:
    return f"{file_path}:{name}:{start_line}"


def _class_id(file_path: str, name: str, start_line: int) -> str:
    return f"{file_path}:{name}:{start_line}"


def _var_id_from_parser(var: dict[str, Any]) -> str:
    """Variable 的 id 由 parser 产出，直接使用。"""
    return var["id"]


def _dir_paths_from_file_paths(file_paths: list[str]) -> set[str]:
    """从文件路径集合推导出所有目录路径（不含文件名）。"""
    out: set[str] = set()
    for fp in file_paths:
        parts = Path(fp).parts
        for i in range(len(parts) - 1):  # 不包含最后一段（文件名）
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


def build_graph(
    tu_results: list[dict[str, Any]],
    repo_root: str = "",
    var_refs_global: list[tuple[str, str, int]] | None = None,
) -> dict[str, Any]:
    """
    输入: ast_parser.collect_all_tus() 或 clangd_parser.collect_all_via_clangd() 的返回值（后者需传入 var_refs_global）。
    输出: 图结构，含 nodes 与 edges，供 Neo4j 写入使用。
    var_refs_global: 可选的 (func_id, var_id, line) 列表，用于 clangd 跨文件变量引用。
    """
    file_paths: set[str] = set()
    functions: list[dict[str, Any]] = []
    classes: list[dict[str, Any]] = []
    file_to_functions: dict[str, list[int]] = {}
    file_to_classes: dict[str, list[int]] = {}
    calls_raw: list[tuple[int, str, str, int, str | None]] = []
    # 变量：id -> 节点属性；引用 (func_id, var_id, line) 后聚合为 (func_id, var_id) -> lines
    variables_by_id: dict[str, dict[str, Any]] = {}
    refs_raw: list[tuple[str, str, int]] = []  # (func_id, var_id, line)

    for tu in tu_results:
        fp = tu["file_path"]
        file_paths.add(fp)
        base = len(functions)
        for f in tu["functions"]:
            functions.append({
                "id": _func_id(fp, f["name"], f["start_line"]),
                "name": f["name"],
                "signature": f.get("signature", ""),
                "file_path": fp,
                "start_line": f["start_line"],
                "end_line": f.get("end_line", f["start_line"]),
            })
        file_to_functions[fp] = list(range(base, len(functions)))
        base_c = len(classes)
        for c in tu["classes"]:
            classes.append({
                "id": _class_id(fp, c["name"], c["start_line"]),
                "name": c["name"],
                "file_path": fp,
                "start_line": c["start_line"],
                "end_line": c.get("end_line", c["start_line"]),
            })
        file_to_classes[fp] = list(range(base_c, len(classes)))
        for call in tu["calls"]:
            ci = call["caller_index"]
            if 0 <= ci < len(tu["functions"]):
                global_caller_idx = base + ci
                callee_fp = call.get("callee_file_path") or None
                calls_raw.append((global_caller_idx, call["callee_name"], fp, call.get("line", 0), callee_fp))
        # 变量：合并到全局，并确定 parent
        for v in tu.get("variables", []):
            vid = _var_id_from_parser(v)
            if vid in variables_by_id:
                continue
            sf = v.get("scope_function_index")
            sc = v.get("scope_class_index")
            if sf is not None and 0 <= base + sf < len(functions):
                variables_by_id[vid] = {**v, "parent_type": "Function", "parent_id": functions[base + sf]["id"]}
            elif sc is not None and 0 <= base_c + sc < len(classes):
                variables_by_id[vid] = {**v, "parent_type": "Class", "parent_id": classes[base_c + sc]["id"]}
            else:
                variables_by_id[vid] = {**v, "parent_type": "File", "parent_id": v["file_path"]}
        for fi, var_id, line in tu.get("var_refs", []):
            if 0 <= base + fi < len(functions):
                refs_raw.append((functions[base + fi]["id"], var_id, line))
    if var_refs_global:
        refs_raw.extend(var_refs_global)
    # 构建目录集合与层级
    all_dirs = _dir_paths_from_file_paths(list(file_paths))
    sorted_dirs = sorted(all_dirs, key=lambda d: (d.count("/"), d))

    variables_list: list[dict[str, Any]] = []
    for v in variables_by_id.values():
        variables_list.append({
            "id": v["id"],
            "name": v["name"],
            "file_path": v.get("file_path", ""),
            "start_line": v.get("start_line", 0),
            "kind": v.get("kind", "global"),
        })
    # 引用聚合 (func_id, var_id) -> lines
    refs_agg: dict[tuple[str, str], list[int]] = {}
    for func_id, var_id, line in refs_raw:
        key = (func_id, var_id)
        if key not in refs_agg:
            refs_agg[key] = []
        refs_agg[key].append(line)

    nodes: dict[str, list[dict[str, Any]]] = {
        "Repository": [],
        "Directory": [],
        "File": [],
        "Function": functions,
        "Class": classes,
        "Variable": variables_list,
        "Attribute": [],  # Class 成员（struct field / class member）
    }

    edges: dict[str, list[tuple[str, str, dict[str, Any]]]] = {
        "CONTAINS": [],
        "CALLS": [],
        "REFERENCES_VAR": [],
        "HAS_MEMBER": [],  # Class -> Attribute
    }

    repo_id = "repo:1"
    nodes["Repository"].append({
        "id": repo_id,
        "root_path": repo_root,
        "last_processed_commit": "",
    })

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

    for fp in sorted(file_paths):
        file_id = _file_id(fp)
        nodes["File"].append({
            "id": file_id,
            "path": fp,
            "name": _file_name(fp),
            "language": Path(fp).suffix.lstrip(".") or "cpp",
        })
        # File 的父目录
        parent = _parent_dir(fp)
        if parent and parent in dir_ids:
            edges["CONTAINS"].append((dir_ids[parent], file_id, {}))
        else:
            # 根级文件，挂在 repo 下（或挂在根目录 "." 下，若存在）
            if "." in dir_ids:
                edges["CONTAINS"].append((dir_ids["."], file_id, {}))
            else:
                edges["CONTAINS"].append((repo_id, file_id, {}))

    for f in functions:
        edges["CONTAINS"].append((f["file_path"], f["id"], {}))
    for c in classes:
        edges["CONTAINS"].append((c["file_path"], c["id"], {}))
    for v in variables_by_id.values():
        edges["CONTAINS"].append((v["parent_id"], v["id"], {}))
        # Class 成员变量：同时创建 Attribute 节点 + HAS_MEMBER 边
        if v.get("parent_type") == "Class" and v.get("kind") == "member":
            nodes["Attribute"].append({
                "id": v["id"],
                "name": v["name"],
                "file_path": v.get("file_path", ""),
                "start_line": v.get("start_line", 0),
                "member_of_class": v.get("parent_id"),
            })
            edges["HAS_MEMBER"].append((v["parent_id"], v["id"], {}))
    for (func_id, var_id), lines in refs_agg.items():
        edges["REFERENCES_VAR"].append((func_id, var_id, {"lines": sorted(set(lines))}))

    # 同一文件内按 name 解析被调用函数；跨文件仅当全局唯一 name 时再连（可选，此处简化为同文件）
    func_by_id = {f["id"]: f for f in functions}
    file_funcs_by_name: dict[str, dict[str, list[str]]] = {}
    for f in functions:
        fp, name = f["file_path"], f["name"]
        if fp not in file_funcs_by_name:
            file_funcs_by_name[fp] = {}
        if name not in file_funcs_by_name[fp]:
            file_funcs_by_name[fp][name] = []
        file_funcs_by_name[fp][name].append(f["id"])

    for caller_idx, callee_name, file_path, _line, callee_file_path in calls_raw:
        if caller_idx >= len(functions):
            continue
        caller_id = functions[caller_idx]["id"]
        callee_id = None
        # 优先用 callee_file_path（clangd 提供的跨文件信息），否则同文件按 name
        search_path = callee_file_path if callee_file_path else file_path
        if search_path in file_funcs_by_name and callee_name in file_funcs_by_name[search_path]:
            cands = file_funcs_by_name[search_path][callee_name]
            callee_id = cands[0] if len(cands) == 1 else cands[0]
        if callee_id and callee_id != caller_id:
            edges["CALLS"].append((caller_id, callee_id, {}))

    return {"nodes": nodes, "edges": edges}
