import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT / "src"))   # 核心库
sys.path.insert(0, str(_ROOT))             # 根目录（config.py）

#!/usr/bin/env python3
"""
为 Neo4j 中的 Function 节点生成基于代码 + 图邻域的注释（graph-aware annotation）。
V2 schema：summary / workflow_role / invocation_context / confidence / is_wrapper / call_depth_hint / neighborhood_confidence / caller_signatures / callee_signatures。

设计目标：
- 离线批注脚本：按需、分批为函数生成 / 更新注释，而不是在 QA 热路径里频繁调 LLM。
- 注释结构固定，便于后续在检索、embedding、QA prompt 中统一使用。
- 支持 --fetch-signatures 从源码提取真实函数签名，替换 Neo4j 中为空的 signature。

用法示例（请在 Code_Graph 目录下运行）：

  python annotate_functions.py --version 2 --limit 50 --module-prefix "src/llama-"
  python annotate_functions.py --version 2 --limit 200 --dry-run
  python annotate_functions.py --version 2 --limit 500 --concurrency 16
  python annotate_functions.py --version 2 --fetch-signatures --limit 100

参数说明：
- --version：本次注释版本号（整数）。仅为 annotation_version < version 或无注释的函数生成/更新注释。
                     V2 会处理所有 annotation_schema_version < 2 或 annotation_version < 2 的函数。
- --limit：最多处理多少个函数（默认 100）。用于小批量试跑。
- --module-prefix：仅处理 file_path 以该前缀开头的函数（例如 src/llama- 或 ggml/src/）。
- --dry-run：只打印将要处理的函数，不实际调用 LLM 或写回 Neo4j。
- --concurrency：并发 LLM 调用数（默认 8）。
- --fetch-signatures：从源码提取真实函数签名，替换 Neo4j 中为空的 signature。
"""
from __future__ import annotations

import argparse
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional

from neo4j import GraphDatabase

from config import LLM_MODEL, OPENAI_API_KEY, OPENAI_BASE_URL, NEO4J_DATABASE, get_repo_root
from neo4j_writer import get_driver

# V2 schema: confidence + is_wrapper + call_depth_hint + caller/callee signatures
ANNOTATION_SCHEMA_VERSION = 2

# ---------------------------------------------------------------------------
# Signature extraction (optional, from source)
# ---------------------------------------------------------------------------

# Match function declarations at the start of a line (possibly with attributes/qualifiers before)
# Captures: (1) full match for validation, group(1)=return type, group(2)=name, group(3)=params
_DECL_RE = re.compile(
    r'^([\w\s\*\(\)\[\]]+?)'          # return type (group 1)
    r'\s+'
    r'(~?[\w_][\w\d_]*)'              # name (group 2)
    r'\s*\(([^)]*)\)'                 # params (group 3)
    r'\s*(const)?\s*$',               # optional const qualifier
    re.MULTILINE
)

# Fallback: find first line matching a simple C/C++ function declaration pattern
_SIMPLE_DECL_RE = re.compile(
    r'^([\w\s\*\(\)\[\]]+?)'          # return type
    r'\s+'
    r'(~?[\w_][\w\d_]*)'              # function name
    r'\s*\(',
    re.MULTILINE
)


def _extract_signature_from_source(repo_root: Path, file_path: str, start_line: int, name: str) -> str:
    """
    从源码文件第 start_line 行附近提取函数声明。
    优先用声明行本身；如果 start_line 指向函数体内部，则回退到扫描前5行。
    """
    if not repo_root or not file_path:
        return ""
    path = (repo_root / file_path).resolve()
    if not path.is_file():
        return ""

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""

    if not (1 <= start_line <= len(lines)):
        return ""

    # Clamp search window: 3 lines before start_line to 1 line after
    window_start = max(0, start_line - 4)
    window_end = min(len(lines), start_line + 2)
    window = "\n".join(lines[window_start:window_end])

    # Try full regex first
    for m in _DECL_RE.finditer(window):
        matched_name = m.group(2)
        if matched_name == name:
            ret = m.group(1).strip()
            params = m.group(3).strip()
            const_qual = m.group(4) or ""
            return f"{ret} {matched_name}({params}){const_qual}"

    # Fallback: find first line where something like "return_type name(" appears
    for i, lnum in enumerate(range(window_start + 1, window_end + 1)):
        line = lines[lnum - 1].strip()
        m = _SIMPLE_DECL_RE.match(line)
        if m and m.group(2) == name:
            # Grab this line and continuation lines until we see a balanced ")"
            decl_lines = [line]
            depth = line.count("(") - line.count(")")
            pos = 1
            while depth > 0 and (lnum + pos) <= len(lines):
                next_line = lines[lnum + pos - 1].strip()
                decl_lines.append(next_line)
                depth += next_line.count("(") - next_line.count(")")
                pos += 1
                if depth == 0:
                    break
            full_decl = " ".join(decl_lines)
            # Trim trailing { or ; or const etc
            full_decl = re.sub(r'\s*[\{;].*$', '', full_decl)
            return full_decl.strip()

    return ""


# ---------------------------------------------------------------------------
# Candidate retrieval
# ---------------------------------------------------------------------------

def _infer_module_from_path(file_path: str) -> str:
    """根据 file_path 的前缀做一个简单的 module 推断。"""
    p = (file_path or "").replace("\\", "/").lstrip("/")
    if not p:
        return ""
    parts = p.split("/")
    if parts[0] in {"src", "ggml", "common", "tests", "tools", "examples"} and len(parts) > 1:
        return f"{parts[0]}/{parts[1]}"
    return parts[0]


def _get_function_candidates(
    driver: GraphDatabase.driver,
    database: str,
    version: int,
    limit: int,
    module_prefix: Optional[str],
    func_names: Optional[List[str]],
) -> List[Dict[str, Any]]:
    """
    从 Neo4j 取需要注释的 Function 列表。
    规则：annotation_version < version 或 annotation_json 为空 或 annotation_schema_version < 当前版本。
    """
    if func_names:
        cypher = """
        MATCH (f:Function)
        WHERE f.name IN $func_names
        RETURN f.id AS id, f.name AS name, f.file_path AS file_path,
               coalesce(f.start_line, 0) AS start_line,
               coalesce(f.end_line, 0) AS end_line,
               coalesce(f.annotation_version, 0) AS old_version,
               coalesce(f.annotation_schema_version, 1) AS old_schema_version
        ORDER BY id
        LIMIT $limit
        """
        with driver.session(database=database) as session:
            r = session.run(cypher, func_names=func_names, limit=limit)
            return [dict(rec) for rec in r]
    else:
        cypher = """
        MATCH (f:Function)
        WHERE (coalesce(f.annotation_version, 0) < $version
               OR coalesce(f.annotation_schema_version, 1) < $schema_version)
        """
        if module_prefix:
            cypher += " AND f.file_path STARTS WITH $module_prefix"
        cypher += """
        RETURN f.id AS id, f.name AS name, f.file_path AS file_path,
               coalesce(f.start_line, 0) AS start_line,
               coalesce(f.end_line, 0) AS end_line,
               coalesce(f.annotation_version, 0) AS old_version,
               coalesce(f.annotation_schema_version, 1) AS old_schema_version
        ORDER BY id
        LIMIT $limit
        """
        with driver.session(database=database) as session:
            r = session.run(
                cypher,
                version=version,
                schema_version=ANNOTATION_SCHEMA_VERSION,
                module_prefix=module_prefix or "",
                limit=limit,
            )
            return [dict(rec) for rec in r]


# ---------------------------------------------------------------------------
# Neighborhood retrieval
# ---------------------------------------------------------------------------

def _get_neighborhood(
    driver: GraphDatabase.driver,
    database: str,
    func_id: str,
    callers_limit: int = 8,
    callees_limit: int = 8,
    fetch_signatures: bool = False,
    repo_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    获取函数的图邻域：file_path / callers / callees。
    当 fetch_signatures=True 且 Neo4j 中的 signature 为空时，
    从源码提取真实签名。
    """
    cypher = """
    MATCH (f:Function {id: $id})
    OPTIONAL MATCH (caller:Function)-[:CALLS]->(f)
    WITH f, collect(DISTINCT {name: caller.name, signature: caller.signature,
                               file_path: caller.file_path, start_line: caller.start_line}) AS raw_callers
    OPTIONAL MATCH (f)-[:CALLS]->(callee:Function)
    WITH f, raw_callers, collect(DISTINCT {name: callee.name, signature: callee.signature,
                                           file_path: callee.file_path, start_line: callee.start_line}) AS raw_callees
    RETURN f.file_path AS file_path,
           [x IN raw_callers | x][0..$callers_limit] AS callers,
           [x IN raw_callees | x][0..$callees_limit] AS callees
    """
    with driver.session(database=database) as session:
        rec = session.run(
            cypher,
            id=func_id,
            callers_limit=callers_limit,
            callees_limit=callees_limit,
        ).single()
    if not rec:
        return {"file_path": "", "callers": [], "callees": []}

    callers: List[Dict[str, Any]] = rec.get("callers") or []
    callees: List[Dict[str, Any]] = rec.get("callees") or []

    if fetch_signatures and repo_root:
        for item in callers + callees:
            stored_sig = item.get("signature") or ""
            if not stored_sig or stored_sig == item.get("name", ""):
                sig = _extract_signature_from_source(
                    repo_root,
                    item.get("file_path") or "",
                    item.get("start_line") or 0,
                    item.get("name") or "",
                )
                if sig:
                    item["signature"] = sig

    # Normalize: coalesce empty signature to name
    for item in callers:
        sig = item.get("signature") or ""
        item["signature"] = sig if sig else item.get("name", "")
    for item in callees:
        sig = item.get("signature") or ""
        item["signature"] = sig if sig else item.get("name", "")

    return {
        "file_path": rec.get("file_path") or "",
        "callers": callers,
        "callees": callees,
    }


# ---------------------------------------------------------------------------
# Code reading
# ---------------------------------------------------------------------------

def _read_function_code(repo_root: Optional[Path], file_path: str, start_line: int, end_line: int) -> str:
    """从源码仓读取函数体（按 start_line/end_line）。失败则返回空字符串。"""
    if not repo_root or not file_path:
        return ""
    path = (repo_root / file_path).resolve()
    if not path.is_file():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    if start_line <= 0 or end_line <= 0 or end_line < start_line:
        return ""
    s = max(0, start_line - 1)
    e = min(len(lines), end_line)
    return "\n".join(lines[s:e])


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

PROMPT_TEMPLATE = """You are helping to document functions in a large C++ codebase.

Function name:
{name}

Function code:
{code}

File:
{file_path}

Module (coarse-grained, inferred from path):
{module}

Callers (functions that call this one, ordered by importance):
{callers}

Callees (functions this one calls, ordered by importance):
{callees}

Task:
Based on the code and graph neighborhood above, generate a V2 JSON annotation for this function.

IMPORTANT RULES:
- If you CANNOT reliably identify callers/callees from the code, set their lists to empty and set neighborhood_confidence to "low". Do NOT guess.
- Use ONLY the caller/callee signatures provided above. Do NOT fabricate, complete, or infer additional parameter types or return types. If a signature is just the function name with no parameters, use it as-is.
- Rate confidence honestly: "low" means you made significant inferences not directly visible in the code.

The JSON must have exactly these fields:
- schema_version: 2 (always integer 2)
- summary: one short sentence describing what the function does.
- workflow_role: one short phrase describing this function's role in larger workflows. Use "generic helper" if truly unknown.
- invocation_context: a list of 1-3 short phrases describing typical invocation scenarios.
- confidence: "high" if you are confident based on code, "medium" if you made some inferences, "low" if uncertain.
- is_wrapper: true if this function mostly delegates to many downstream callees; false otherwise. When in doubt, false.
- call_depth_hint: estimate of call depth: 0 for leaf functions, 1-2 for typical functions, 3+ for deep/wrapper functions.
- neighborhood_confidence: "high" if caller/callee lists accurately reflect the call graph, "medium" if partially complete, "low" if lists are uncertain/empty.
- caller_signatures: list of caller items (name + signature). Empty list if neighborhood_confidence is "low".
- callee_signatures: list of callee items (name + signature). Empty list if neighborhood_confidence is "low".

Output format:
Return ONLY a valid JSON object, no extra text, no markdown, no explanation.

Example:
{{
  "schema_version": 2,
  "summary": "Initialize network interfaces by configuring ports and addresses.",
  "workflow_role": "network initialization step",
  "invocation_context": ["system startup", "network reconfiguration"],
  "confidence": "high",
  "is_wrapper": false,
  "call_depth_hint": 1,
  "neighborhood_confidence": "high",
  "caller_signatures": [
    {{"name": "init_all", "signature": "void init_all(config*)"}}
  ],
  "callee_signatures": [
    {{"name": "open_port", "signature": "int open_port(int)"}},
    {{"name": "set_addr", "signature": "void set_addr(struct sockaddr_in*)"}}
  ]
}}
"""


def _fmt_caller_callee(items: List[Dict[str, str]], label: str) -> str:
    """格式化 caller/callee 列表为文本。"""
    if not items:
        return f"{label}: (none)"
    lines = [f"{label}:"]
    for item in items:
        name = item.get("name", "")
        sig = item.get("signature", "")
        if sig and sig != name:
            lines.append(f"  - {name}: {sig}")
        else:
            lines.append(f"  - {name}")
    return "\n".join(lines)


def _build_prompt(
    name: str,
    code: str,
    file_path: str,
    module: str,
    callers: List[Dict[str, str]],
    callees: List[Dict[str, str]],
) -> str:
    callers_text = _fmt_caller_callee(callers, "Callers")
    callees_text = _fmt_caller_callee(callees, "Callees")
    return PROMPT_TEMPLATE.format(
        name=name,
        code=code or "(code not available)",
        file_path=file_path or "(unknown)",
        module=module or "(unknown)",
        callers=callers_text,
        callees=callees_text,
    )


# ---------------------------------------------------------------------------
# LLM calling (thread-safe, creates its own OpenAI client per call)
# ---------------------------------------------------------------------------

def _call_llm(prompt: str) -> Optional[Dict[str, Any]]:
    """调用 LLM 生成注释，返回解析后的 dict（失败返回 None）。"""
    if not OPENAI_API_KEY:
        return None
    from openai import OpenAI

    client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)
    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=400,
        )
    except Exception:
        return None
    if not resp.choices or not resp.choices[0].message.content:
        return None
    text = resp.choices[0].message.content.strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# Post-processing & writing
# ---------------------------------------------------------------------------

def _post_process_annotation(ann: Dict[str, Any]) -> Dict[str, Any]:
    """V2 后处理：确保 annotation 包含所有必填字段，缺失时填充默认值。"""
    defaults: Dict[str, Any] = {
        "schema_version": ANNOTATION_SCHEMA_VERSION,
        "confidence": "medium",
        "is_wrapper": False,
        "call_depth_hint": 1,
        "neighborhood_confidence": "medium",
        "caller_signatures": [],
        "callee_signatures": [],
    }
    result = dict(ann)
    for k, v in defaults.items():
        if k not in result:
            result[k] = v
    result["schema_version"] = ANNOTATION_SCHEMA_VERSION
    return result


def _update_function_annotation(
    driver: GraphDatabase.driver,
    database: str,
    func_id: str,
    annotation: Dict[str, Any],
    version: int,
) -> None:
    """将注释写回 Neo4j 的 Function 节点。"""
    with driver.session(database=database) as session:
        session.run(
            """
            MATCH (f:Function {id: $id})
            SET f.annotation_json = $annotation_json,
                f.annotation_version = $version,
                f.annotation_schema_version = $schema_version,
                f.annotation_quality = 'passed'
            """,
            id=func_id,
            annotation_json=json.dumps(annotation, ensure_ascii=False),
            version=version,
            schema_version=ANNOTATION_SCHEMA_VERSION,
        )


# ---------------------------------------------------------------------------
# Per-function worker (runs in thread pool)
# ---------------------------------------------------------------------------

def _process_function(
    func_dict: Dict[str, Any],
    driver: GraphDatabase.driver,
    database: str,
    version: int,
    repo_root: Optional[Path],
    fetch_signatures: bool,
) -> tuple[str, str, str, bool, str, str]:
    """
    处理单个函数：拉邻域 → 读代码 → 拼 prompt → 调 LLM → 后处理 → 写回。
    返回 (func_id, name, file_path, success, confidence, is_wrapper_str, error_msg)。
    """
    func_id = func_dict["id"]
    name = func_dict["name"]
    file_path = func_dict.get("file_path") or ""
    start_line = int(func_dict.get("start_line") or 0)
    end_line = int(func_dict.get("end_line") or 0)

    try:
        nbr = _get_neighborhood(
            driver, database, func_id,
            fetch_signatures=fetch_signatures,
            repo_root=repo_root,
        )
        file_path_effective = nbr.get("file_path") or file_path
        if not file_path_effective:
            file_path_effective = file_path

        module = _infer_module_from_path(file_path_effective)
        code = _read_function_code(repo_root, file_path_effective, start_line, end_line)

        prompt = _build_prompt(
            name=name,
            code=code,
            file_path=file_path_effective,
            module=module,
            callers=nbr.get("callers") or [],
            callees=nbr.get("callees") or [],
        )

        raw = _call_llm(prompt)
        if raw is None:
            return func_id, name, file_path_effective, False, "?", "?", "LLM returned empty or non-JSON"

        annotation = _post_process_annotation(raw)
        _update_function_annotation(driver, database, func_id, annotation, version)

        conf = annotation.get("confidence", "?")
        is_wrp = str(annotation.get("is_wrapper", "?"))
        return func_id, name, file_path_effective, True, conf, is_wrp, ""

    except Exception as e:
        return func_id, name, file_path, False, "?", "?", str(e)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="为 Function 节点生成基于代码+图邻域的注释（V2）")
    parser.add_argument("--version", type=int, default=2, help="注释版本号")
    parser.add_argument("--limit", type=int, default=100, help="最多处理多少个函数（默认 100）")
    parser.add_argument(
        "--module-prefix", type=str, default="",
        help="仅处理 file_path 以该前缀开头的函数"
    )
    parser.add_argument(
        "--func-names", type=str, default="",
        help="逗号分隔的函数名列表，只处理这些函数（优先级最高）"
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印候选函数")
    parser.add_argument(
        "--concurrency", type=int, default=8,
        help="并发 LLM 调用数（默认 8）"
    )
    parser.add_argument(
        "--fetch-signatures", action="store_true",
        help="从源码提取真实函数签名，替换 Neo4j 中为空的 signature"
    )
    args = parser.parse_args()

    func_names: Optional[List[str]] = None
    if args.func_names:
        func_names = [fn.strip() for fn in args.func_names.split(",") if fn.strip()]

    driver = get_driver()
    try:
        driver.verify_connectivity()
    except Exception as e:
        print(f"Neo4j 连接失败: {e}")
        return 1

    repo_root = get_repo_root()
    if not repo_root:
        print("警告：未配置 REPO_ROOT，将无法读取函数代码。")

    candidates = _get_function_candidates(
        driver,
        NEO4J_DATABASE,
        version=args.version,
        limit=args.limit,
        module_prefix=args.module_prefix or None,
        func_names=func_names,
    )
    filter_desc = (
        f"func_names={func_names}" if func_names
        else f"prefix={args.module_prefix}" if args.module_prefix
        else "全部"
    )
    print(f"待处理函数数：{len(candidates)}（{filter_desc}，version<{args.version} 或 schema_version<{ANNOTATION_SCHEMA_VERSION}）")
    print(f"并发数：{args.concurrency}，签名补全：{args.fetch_signatures}")

    if not candidates:
        driver.close()
        return 0

    if args.dry_run:
        for c in candidates:
            print(
                f"- {c['id']}: {c['name']} @ {c['file_path']} "
                f"(old_v={c['old_version']}, old_schema={c['old_schema_version']})"
            )
        driver.close()
        return 0

    processed = 0
    skipped = 0

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {
            pool.submit(
                _process_function,
                c, driver, NEO4J_DATABASE, args.version,
                repo_root, args.fetch_signatures,
            ): c
            for c in candidates
        }

        for future in as_completed(futures):
            func_id, name, fp, success, conf, is_wrp, err = future.result()
            if success:
                print(f"[ok] {func_id}: {name} @ {fp} [confidence={conf}, wrapper={is_wrp}]")
                processed += 1
            else:
                print(f"[skip] {func_id}: {name} — {err}")
                skipped += 1

    driver.close()
    print(f"完成：成功更新 {processed} 个函数的注释，{skipped} 个跳过（version={args.version}, schema={ANNOTATION_SCHEMA_VERSION}）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
