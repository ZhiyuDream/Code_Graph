from __future__ import annotations

import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from .neo4j_writer import ensure_constraints, clear_code_graph
from .tree_sitter_shards import extract_to_shards, iter_shard_records


def _write_nodes(session, label: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    session.run(
        f"""
        UNWIND $rows AS row
        MERGE (n:{label} {{id: row.id}})
        SET n += row
        """,
        rows=rows,
    )


def _write_edges(session, rel_type: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(row["from_label"], row["to_label"])].append(row)
    for (from_label, to_label), group in groups.items():
        session.run(
            f"""
            UNWIND $rows AS row
            MATCH (a:{from_label} {{id: row.from_id}})
            MATCH (b:{to_label} {{id: row.to_id}})
            MERGE (a)-[r:{rel_type}]->(b)
            SET r += row.props
            """,
            rows=group,
        )


def _parent_dirs(file_path: str) -> list[str]:
    parts = Path(file_path).parts
    return [str(Path(*parts[:idx])) for idx in range(1, len(parts))]


def _file_rows(record: dict[str, Any], repo_id: str) -> tuple[list, list]:
    fr = record["file_result"]
    fp = fr["file_path"]
    suffix = Path(fp).suffix.lstrip(".") or "cpp"
    if suffix == "h":
        suffix = "cpp"

    nodes: list[tuple[str, dict[str, Any]]] = []
    edges: list[dict[str, Any]] = []
    parent = repo_id
    parent_label = "Repository"
    for directory in _parent_dirs(fp):
        did = f"dir:{directory}"
        nodes.append(("Directory", {"id": did, "path": directory, "name": Path(directory).name}))
        edges.append({"from_id": parent, "to_id": did, "from_label": parent_label, "to_label": "Directory", "props": {}})
        parent = did
        parent_label = "Directory"

    nodes.append(("File", {"id": fp, "path": fp, "name": Path(fp).name, "language": suffix}))
    edges.append({"from_id": parent, "to_id": fp, "from_label": parent_label, "to_label": "File", "props": {}})

    for cls in fr.get("classes", []):
        cid = f"{fp}:{cls['name']}:{cls['start_line']}"
        nodes.append(("Class", {
            "id": cid, "name": cls["name"], "file_path": fp,
            "start_line": cls["start_line"], "end_line": cls["end_line"],
        }))
        edges.append({"from_id": fp, "to_id": cid, "from_label": "File", "to_label": "Class", "props": {}})

    for fn in fr.get("functions", []):
        fid = fn["id"] or f"{fp}:{fn['name']}:{fn['start_line']}"
        nodes.append(("Function", {**fn, "id": fid}))
        edges.append({"from_id": fp, "to_id": fid, "from_label": "File", "to_label": "Function", "props": {}})

    return nodes, edges


def _build_symbol_index(shard_dir: Path, db_path: Path) -> sqlite3.Connection:
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE symbols (id TEXT PRIMARY KEY, name TEXT, tail TEXT, file_path TEXT, is_definition INTEGER)")
    conn.execute("CREATE INDEX symbols_tail ON symbols(tail)")
    conn.execute("CREATE INDEX symbols_file_tail ON symbols(file_path, tail)")
    for record in iter_shard_records(shard_dir):
        fr = record["file_result"]
        for fn in fr.get("functions", []):
            fid = fn["id"]
            name = fn["name"]
            conn.execute(
                "INSERT OR IGNORE INTO symbols VALUES (?, ?, ?, ?, ?)",
                (fid, name, name.split("::")[-1], fr["file_path"], int(fn.get("is_definition", True))),
            )
    conn.commit()
    return conn


def _resolve_call_candidates(conn: sqlite3.Connection, record: dict[str, Any]) -> list[dict[str, Any]]:
    fr = record["file_result"]
    functions = fr.get("functions", [])
    out: list[dict[str, Any]] = []
    for call in fr.get("calls", []):
        index = int(call.get("caller_index", -1))
        if index < 0 or index >= len(functions):
            continue
        caller_id = functions[index]["id"]
        tail = call["callee_name"].split("::")[-1]
        local = conn.execute(
            "SELECT id FROM symbols WHERE file_path = ? AND tail = ?",
            (fr["file_path"], tail),
        ).fetchall()
        candidates = local
        confidence = 0.95
        resolution = "same_file_unique"
        if len(local) != 1:
            candidates = conn.execute(
                "SELECT id FROM symbols WHERE tail = ? AND is_definition = 1",
                (tail,),
            ).fetchall()
            confidence = 0.60
            resolution = "global_unique" if len(candidates) == 1 else "ambiguous"
        if len(candidates) == 1 and candidates[0][0] != caller_id:
            out.append({
                "from_id": caller_id,
                "to_id": candidates[0][0],
                "from_label": "Function",
                "to_label": "Function",
                "props": {
                    "confidence": confidence,
                    "resolution": resolution,
                    "source": "tree-sitter",
                    "line": call.get("line", 0),
                },
            })
    return out


def run_tree_sitter_pipeline(
    repo_root: Path,
    driver: Any,
    database: str,
    shard_dir: Path | None = None,
    batch_size: int = 512,
    clear_existing: bool = True,
) -> dict[str, Any]:
    shard_dir = shard_dir or (repo_root / ".code_graph" / "tree_sitter_shards")
    extraction = extract_to_shards(repo_root, shard_dir, batch_size=batch_size)
    sqlite_path = shard_dir / "symbols.sqlite"
    conn = _build_symbol_index(shard_dir / "files", sqlite_path)

    repo_id = "repo:1"
    total_nodes = total_edges = total_calls = 0
    node_batches: dict[str, list[dict[str, Any]]] = defaultdict(list)
    edge_batches: dict[str, list[dict[str, Any]]] = defaultdict(list)

    try:
        ensure_constraints(driver, database)
        if clear_existing:
            clear_code_graph(driver, database)
        with driver.session(database=database) as session:
            _write_nodes(session, "Repository", [{"id": repo_id, "root_path": str(repo_root)}])
            for record in iter_shard_records(shard_dir / "files"):
                nodes, edges = _file_rows(record, repo_id)
                for label, node in nodes:
                    node_batches[label].append(node)
                    total_nodes += 1
                edge_batches["CONTAINS"].extend(edges)
                total_edges += len(edges)
                calls = _resolve_call_candidates(conn, record)
                edge_batches["CALLS_CANDIDATE"].extend(calls)
                total_calls += len(calls)
                if sum(len(v) for v in node_batches.values()) >= batch_size:
                    for label, rows in node_batches.items():
                        _write_nodes(session, label, rows)
                    _write_edges(session, "CONTAINS", edge_batches["CONTAINS"])
                    _write_edges(session, "CALLS_CANDIDATE", edge_batches["CALLS_CANDIDATE"])
                    node_batches.clear()
                    edge_batches.clear()
            for label, rows in node_batches.items():
                _write_nodes(session, label, rows)
            _write_edges(session, "CONTAINS", edge_batches["CONTAINS"])
            _write_edges(session, "CALLS_CANDIDATE", edge_batches["CALLS_CANDIDATE"])
    finally:
        conn.close()

    return {
        **extraction,
        "neo4j_nodes_submitted": total_nodes + 1,
        "neo4j_contains_edges_submitted": total_edges,
        "neo4j_calls_candidate_submitted": total_calls,
        "parser": "tree-sitter-only",
    }
