import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT / "src"))   # 核心库
sys.path.insert(0, str(_ROOT))             # 根目录（config.py）

#!/usr/bin/env python3
"""
Agentic 注释生成器：让 LLM 自己决定需要探索哪些上下游代码，
直到有足够上下文再生成 V2 annotation。

流程：
1. LLM 读取目标函数的代码
2. 判断是否 wrapper → 需要读下游
3. 判断是否需要理解 caller 上下文 → 需要读上游
4. 探索足够后，输出最终 JSON annotation

与 annotate_functions.py 的区别：
- annotate_functions.py: 单次 LLM 调用，只给 caller/callee 名字
- agentic_annotate.py: 多步 agent 循环，可以主动读上下游代码
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from neo4j import GraphDatabase

from config import LLM_MODEL, OPENAI_API_KEY, OPENAI_BASE_URL, NEO4J_DATABASE, get_repo_root
from neo4j_writer import get_driver

ANNOTATION_SCHEMA_VERSION = 2

# ---------------------------------------------------------------------------
# Agentic Prompt
# ---------------------------------------------------------------------------

AGENT_SYSTEM_PROMPT = """You are an expert at understanding C/C++ functions in large codebases.

Your task: Generate a V2 JSON annotation for the target function.

OUTPUT FORMAT - STRICT:
You MUST output EXACTLY a valid JSON object with these top-level fields:
{
  "schema_version": 2,
  "summary": "one short sentence - be specific, NOT generic",
  "workflow_role": "one short phrase",
  "invocation_context": ["phrase1", "phrase2"],
  "failure_modes": ["known crash point or empty list"],
  "confidence": "high|medium|low",
  "is_wrapper": true|false,
  "call_depth_hint": 0|1|2|3,
  "why_wrapper": "only if is_wrapper=true, explain WHY",
  "neighborhood_confidence": "high|medium|low",
  "caller_signatures": [{"name": "...", "signature": "..."}],
  "callee_signatures": [{"name": "...", "signature": "..."}]
}

EXPLORATION MODE:
- You can explore up to 5 rounds
- To read a function's code, write EXACTLY on its own line: READ: func_name
- When you have enough context, output the JSON above, then on a new line: ANNOTATION_COMPLETE

RULES:
- Do NOT wrap the JSON in any extra keys like "function" or "annotation"
- Do NOT add any text before or after the JSON
- If is_wrapper=true, you MUST fill in why_wrapper
- summary must describe what this specific function does, not generic text
- If something is unknown, say so honestly"""


def _read_function_code(repo_root: Optional[Path], file_path: str, start_line: int, end_line: int, max_lines: int = 100) -> str:
    """读取函数体"""
    if not repo_root or not file_path:
        return "(code not available)"
    path = (repo_root / file_path).resolve()
    if not path.is_file():
        return f"(file not found: {file_path})"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return "(read error)"
    s = max(0, start_line - 1)
    e = min(len(lines), end_line, s + max_lines)
    return "\n".join(lines[s:e])


def _get_function_info(driver, func_name: str) -> Optional[Dict[str, Any]]:
    """从 Neo4j 获取函数信息和邻域"""
    with driver.session(database=NEO4J_DATABASE) as s:
        r = s.run("""
            MATCH (f:Function {name: $name})
            OPTIONAL MATCH (caller:Function)-[:CALLS]->(f)
            WITH f, collect(DISTINCT {name: caller.name, signature: coalesce(caller.signature, caller.name)}) AS raw_callers
            OPTIONAL MATCH (f)-[:CALLS]->(callee:Function)
            WITH f, raw_callers, collect(DISTINCT {name: callee.name, signature: coalesce(callee.signature, callee.name)}) AS raw_callees
            RETURN f.name AS name, f.file_path AS file_path,
                   coalesce(f.start_line, 0) AS start_line,
                   coalesce(f.end_line, 0) AS end_line,
                   raw_callers[0..5] AS callers,
                   raw_callees[0..5] AS callees
        """, name=func_name).single()
        if not r:
            return None
        return dict(r)


def _get_callee_info(driver, callee_name: str) -> Optional[Dict[str, Any]]:
    """获取某个 callee 的详细信息"""
    with driver.session(database=NEO4J_DATABASE) as s:
        r = s.run("""
            MATCH (f:Function {name: $name})
            RETURN f.name AS name, f.file_path AS file_path,
                   coalesce(f.start_line, 0) AS start_line,
                   coalesce(f.end_line, 0) AS end_line,
                   coalesce(f.annotation_json, '') AS existing_ann
        """, name=callee_name).single()
        return dict(r) if r else None


def _call_llm(messages: List[dict]) -> Optional[str]:
    """调用 LLM"""
    if not OPENAI_API_KEY:
        return None
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)
    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=messages,
            max_tokens=600,
        )
    except Exception:
        return None
    if not resp.choices or not resp.choices[0].message.content:
        return None
    return resp.choices[0].message.content.strip()


def _parse_annotation_response(text: str) -> tuple[Optional[Dict], str]:
    """
    解析 LLM 返回。如果包含 ANNOTATION_COMPLETE，返回 annotation dict 和之前的所有探索内容。
    否则返回 (None, text)
    """
    if "ANNOTATION_COMPLETE" in text:
        # 去掉 ANNOTATION_COMPLETE 行，提取 JSON
        lines = text.splitlines()
        json_lines = []
        for line in lines:
            if "ANNOTATION_COMPLETE" in line:
                break
            json_lines.append(line)
        json_text = "\n".join(json_lines)
        try:
            ann = json.loads(json_text)
            return ann, text
        except Exception:
            return None, text
    return None, text


def _read_func_code_by_name(driver, repo_root, func_name: str) -> str:
    """根据函数名查 Neo4j 并读源码"""
    info = _get_function_info(driver, func_name)
    if not info:
        return f"// {func_name}: not found in graph"
    fp = info.get("file_path", "")
    sl = info.get("start_line", 0)
    el = info.get("end_line", 0)
    code = _read_function_code(repo_root, fp, sl, el)
    return f"// {func_name} ({fp}:{sl}-{el})\n{code}"


def agentic_generate_annotation(
    driver,
    repo_root: Optional[Path],
    func_name: str,
    max_rounds: int = 5,
) -> Optional[Dict[str, Any]]:
    """
    Agentic 生成 V2 annotation。
    流程：
    1. 获取函数信息
    2. 构造初始 prompt（函数代码 + caller/callee 名字）
    3. 多轮对话，LLM 可以探索上下游代码
    4. LLM 返回 ANNOTATION_COMPLETE 时停止
    """
    info = _get_function_info(driver, func_name)
    if not info:
        print(f"  [skip] {func_name}: not found in Neo4j")
        return None

    file_path = info.get("file_path", "")
    start_line = info.get("start_line", 0)
    end_line = info.get("end_line", 0)
    callers = info.get("callers", [])
    callees = info.get("callees", [])

    own_code = _read_function_code(repo_root, file_path, start_line, end_line)

    # 初始化探索历史
    explored = {func_name: {"code": own_code, "type": "target"}}

    initial_msg = f"""Generate V2 annotation for this function:

Function name: {func_name}
File: {file_path} (lines {start_line}-{end_line})

Own code:
{own_code}

Known callers (from call graph):
{json.dumps(callers, indent=2) if callers else "(none)"}

Known callees (from call graph):
{json.dumps(callees, indent=2) if callees else "(none)"}

To explore: say "READ: func_name" (e.g. "READ: ggml_add_impl")
To finish: output the JSON annotation with ANNOTATION_COMPLETE at the end."""

    messages = [
        {"role": "system", "content": AGENT_SYSTEM_PROMPT},
        {"role": "user", "content": initial_msg},
    ]

    # 可探索的函数池（所有 caller + callee）
    all_related = {}
    for c in callers:
        n = c.get("name") or ""
        if n:
            all_related[n] = ("caller", c)
    for c in callees:
        n = c.get("name") or ""
        if n:
            all_related[n] = ("callee", c)

    for round_i in range(max_rounds):
        resp = _call_llm(messages)
        if not resp:
            print(f"  [skip] {func_name}: LLM returned empty")
            return None

        resp_text = resp.strip()

        # 检查是否完成
        ann, _ = _parse_annotation_response(resp_text)
        if ann:
            print(f"  [ok] {func_name} @ {file_path} (explored {len(explored)} functions)")
            return ann

        # 未完成，继续探索
        messages.append({"role": "assistant", "content": resp_text})

        # 从回复中提取 READ: func_name 请求
        func_to_read = _extract_read_request(resp_text, all_related, explored)
        if func_to_read:
            if func_to_read in explored:
                # Already explored - tell LLM and push to finish
                messages.append({"role": "user", "content": f"""You already have the code for '{func_to_read}' (shown above as the target function).
Stop re-reading explored functions.

Already explored: {list(explored.keys())}
Unexplored related functions: {[fn for fn in all_related if fn not in explored][:5]}

Now output the final JSON annotation with ANNOTATION_COMPLETE."""})
            else:
                code = _read_func_code_by_name(driver, repo_root, func_to_read)
                # Determine type: if in all_related use that, else check if it has callers/callees
                if func_to_read in all_related:
                    ftype = all_related[func_to_read][0]
                else:
                    # Check Neo4j for relationship
                    info = _get_callee_info(driver, func_to_read)
                    if info and info.get("existing_ann"):
                        ftype = "callee"  # exists but not linked
                    else:
                        ftype = "discovered"
                explored[func_to_read] = {"code": code, "type": ftype}

                # 列出已探索的函数
                explored_summary = "\n".join([
                    f"- {fn} ({e['type']}): {e['code'][:80].splitlines()[0]}"
                    for fn, e in explored.items()
                ])

                messages.append({"role": "user", "content": f"""Code for '{func_to_read}':
{code}

Already explored:
{explored_summary}

Unexplored: {[fn for fn in all_related if fn not in explored][:5]}

Continue exploring or output the final JSON annotation with ANNOTATION_COMPLETE."""})
        else:
            # 没有明确的探索请求，但还没完成。给 hint 并列出未探索的函数
            unexp = [fn for fn in all_related if fn not in explored]
            # Get the target's own code from explored
            target_code = explored.get(func_name, {}).get('code', '')[:200]
            hint = f"""Reminder: The target function '{func_name}' code was already provided at the start.
Do NOT ask to re-read it.

Unexplored related functions you can READ: {unexp[:5]}
Already explored: {list(explored.keys())}

Choose an unexplored function to READ, OR output the final JSON annotation with ANNOTATION_COMPLETE."""
            messages.append({"role": "user", "content": hint})

    print(f"  [max rounds] {func_name}: exceeded {max_rounds} rounds, explored {len(explored)}")
    return None


def _extract_read_request(
    resp_text: str,
    all_related: Dict[str, tuple],
    explored: Dict[str, Any],
) -> Optional[str]:
    """
    从 LLM 回复中提取 "READ: func_name" 请求。
    只要 LLM 请求读取某个函数（且未被探索过），就返回。
    是否在 graph 中由调用方检查。
    """
    import re
    # 匹配 "READ: func_name" 或 "read: func_name"
    matches = re.findall(r'(?:^|\n)READ:\s*([a-z_][a-z0-9_]*|[\w::]+)', resp_text, re.IGNORECASE)
    for match in matches:
        if match not in explored:
            return match
    return None


def _post_process_annotation(ann: Dict[str, Any]) -> Dict[str, Any]:
    """确保 annotation 有所有必填字段"""
    defaults = {
        "schema_version": ANNOTATION_SCHEMA_VERSION,
        "failure_modes": [],
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


def _update_annotation(driver, func_id: str, annotation: Dict[str, Any], version: int) -> None:
    """写回 Neo4j"""
    with driver.session(database=NEO4J_DATABASE) as s:
        s.run("""
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


def main():
    parser = argparse.ArgumentParser(description="Agentic V2 annotation generator")
    parser.add_argument("--func-names", type=str, default="",
                        help="逗号分隔的函数名列表")
    parser.add_argument("--csv", type=Path, default=None,
                        help="从 CSV 文件的问题中提取函数名")
    parser.add_argument("--limit", type=int, default=20,
                        help="最多处理多少个函数")
    parser.add_argument("--max-rounds", type=int, default=5,
                        help="每个函数最多探索轮数")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    driver = get_driver()
    try:
        driver.verify_connectivity()
    except Exception as e:
        print(f"Neo4j connection failed: {e}")
        return 1

    repo_root = get_repo_root()

    # 确定要处理的函数列表
    if args.func_names:
        func_names = [fn.strip() for fn in args.func_names.split(",") if fn.strip()]
    elif args.csv:
        import pandas as pd
        import re
        df = pd.read_csv(args.csv)
        funcs = set()
        for _, row in df.iterrows():
            text = str(row.get("问题", "")) + " " + str(row.get("参考答案", ""))
            names = re.findall(r'[a-z_][a-z0-9_]*|[A-Z][a-zA-Z0-9]+::[\w]+', text)
            funcs.update([n for n in names if len(n) > 4 and "_" in n or "::" in n])
        func_names = sorted(funcs)[:args.limit]
    else:
        print("Must specify --func-names or --csv")
        return 1

    print(f"Processing {len(func_names)} functions with agentic annotation (max_rounds={args.max_rounds})")
    if args.dry_run:
        for fn in func_names:
            print(f"  - {fn}")
        return 0

    processed = 0
    skipped = 0
    for fn in func_names:
        print(f"Processing: {fn}")
        ann = agentic_generate_annotation(driver, repo_root, fn, max_rounds=args.max_rounds)
        if ann is None:
            skipped += 1
            continue

        ann = _post_process_annotation(ann)

        # 获取 func_id
        with driver.session(database=NEO4J_DATABASE) as s:
            r = s.run("MATCH (f:Function {name: $name}) RETURN f.id AS id", name=fn).single()
            if r:
                _update_annotation(driver, r["id"], ann, version=3)
                processed += 1
            else:
                print(f"  [skip] {fn}: not found")
                skipped += 1

    driver.close()
    print(f"\nDone: {processed} annotated, {skipped} skipped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
