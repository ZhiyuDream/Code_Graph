import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT / "src"))   # 核心库
sys.path.insert(0, str(_ROOT))             # 根目录（config.py）

#!/usr/bin/env python3
"""
注释幻觉检测：检查 annotation_json 中提到的函数名是否真实存在于图中。
纯 Cypher + 文本解析，无 LLM 调用。

用法：
  python check_annotation_hallucination.py [--limit N] [--output PATH]
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from config import NEO4J_DATABASE
from neo4j_writer import get_driver

DEFAULT_OUTPUT = Path(__file__).resolve().parent / "experiments" / "hallucination_report.json"


def _get_all_function_names(driver, database: str) -> set[str]:
    """获取图中所有 Function 节点名称。"""
    with driver.session(database=database) as s:
        r = s.run("MATCH (f:Function) RETURN f.name AS name")
        return {rec["name"] for rec in r}


def _get_annotated_functions(driver, database: str, limit: int = 0) -> list[dict]:
    """获取所有有注释的函数。"""
    q = """
        MATCH (f:Function)
        WHERE f.annotation_json IS NOT NULL
        RETURN f.name AS name, f.annotation_json AS annotation_json,
               coalesce(f.file_path, '') AS file_path
    """
    if limit > 0:
        q += f" LIMIT {limit}"
    with driver.session(database=database) as s:
        r = s.run(q)
        out = []
        for rec in r:
            raw = rec["annotation_json"]
            try:
                ann = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                continue
            out.append({
                "name": rec["name"],
                "file_path": rec["file_path"],
                "annotation": ann,
            })
        return out


def _extract_mentioned_names(annotation: dict, self_name: str) -> set[str]:
    """从注释文本中提取可能的函数名引用（C/C++ 风格标识符）。"""
    texts = []
    texts.append(annotation.get("summary", ""))
    texts.append(annotation.get("workflow_role", ""))
    ctx = annotation.get("invocation_context", [])
    if isinstance(ctx, list):
        texts.extend(ctx)
    elif isinstance(ctx, str):
        texts.append(ctx)

    combined = " ".join(texts)
    # 匹配 C/C++ 标识符：至少含一个下划线或驼峰，长度>=3，排除常见英文词
    candidates = set(re.findall(r'\b([a-zA-Z_][a-zA-Z0-9_]{2,})\b', combined))

    # 过滤：排除自身、常见非函数名词汇
    stop_words = {
        self_name, "the", "this", "that", "with", "from", "for", "and", "not",
        "are", "was", "were", "been", "being", "have", "has", "had", "does",
        "did", "will", "would", "could", "should", "may", "might", "shall",
        "can", "need", "must", "used", "using", "use", "call", "calls",
        "called", "function", "functions", "method", "class", "file", "code",
        "data", "type", "value", "name", "path", "result", "error", "return",
        "void", "int", "bool", "char", "float", "double", "size", "string",
        "const", "static", "struct", "enum", "NULL", "nullptr", "true", "false",
        "include", "define", "ifdef", "endif", "else", "case", "break",
        "continue", "while", "switch", "default", "typedef", "namespace",
        "public", "private", "protected", "virtual", "override", "template",
        "auto", "inline", "extern", "register", "volatile", "unsigned", "signed",
        "long", "short", "sizeof", "assert", "main", "std", "vector", "map",
        "set", "list", "pair", "make", "push", "pop", "begin", "end",
        "size_t", "uint8_t", "uint16_t", "uint32_t", "uint64_t",
        "int8_t", "int16_t", "int32_t", "int64_t",
        # 中文语境常见英文词
        "context", "token", "model", "layer", "tensor", "buffer", "memory",
        "input", "output", "param", "params", "config", "init", "free",
        "alloc", "malloc", "realloc", "printf", "fprintf", "sprintf",
    }

    # 只保留看起来像函数名的（含下划线，或以特定前缀开头）
    func_like = set()
    for c in candidates:
        if c.lower() in {w.lower() for w in stop_words}:
            continue
        # 含下划线 → 很可能是函数名
        if '_' in c:
            func_like.add(c)
        # 以项目常见前缀开头
        elif c.startswith(('ggml_', 'llama_', 'common_', 'gguf_', 'llm_')):
            func_like.add(c)

    return func_like


def check(driver, database: str, limit: int = 0):
    print("加载图中所有函数名...")
    all_names = _get_all_function_names(driver, database)
    print(f"  图中函数: {len(all_names)} 个")

    print("加载已注释函数...")
    annotated = _get_annotated_functions(driver, database, limit)
    print(f"  已注释函数: {len(annotated)} 个")

    results = []
    total_mentions = 0
    total_hallucinated = 0
    total_verified = 0

    for item in annotated:
        mentioned = _extract_mentioned_names(item["annotation"], item["name"])
        if not mentioned:
            continue

        existing = mentioned & all_names
        hallucinated = mentioned - all_names

        total_mentions += len(mentioned)
        total_verified += len(existing)
        total_hallucinated += len(hallucinated)

        if hallucinated:
            results.append({
                "function": item["name"],
                "file": item["file_path"],
                "mentioned_names": sorted(mentioned),
                "verified": sorted(existing),
                "hallucinated": sorted(hallucinated),
                "hallucination_rate": round(len(hallucinated) / len(mentioned), 3),
            })

    results.sort(key=lambda x: x["hallucination_rate"], reverse=True)

    # 摘要
    print(f"\n--- 幻觉检测摘要 ---")
    print(f"检查注释数: {len(annotated)}")
    print(f"提及函数名总数: {total_mentions}")
    print(f"  图中存在: {total_verified} ({100*total_verified/max(total_mentions,1):.1f}%)")
    print(f"  疑似幻觉: {total_hallucinated} ({100*total_hallucinated/max(total_mentions,1):.1f}%)")
    print(f"含幻觉的注释数: {len(results)} ({100*len(results)/max(len(annotated),1):.1f}%)")

    if results:
        print(f"\nTop 20 幻觉率最高的注释:")
        for r in results[:20]:
            print(f"  {r['function']} ({r['file']})")
            print(f"    幻觉名: {', '.join(r['hallucinated'][:5])}")
            print(f"    幻觉率: {r['hallucination_rate']:.0%} ({len(r['hallucinated'])}/{len(r['mentioned_names'])})")

    return results


def main():
    parser = argparse.ArgumentParser(description="注释幻觉检测")
    parser.add_argument("--limit", type=int, default=0, help="检查前 N 个注释（0=全部）")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    driver = get_driver()
    try:
        driver.verify_connectivity()
        results = check(driver, NEO4J_DATABASE, args.limit)
    finally:
        driver.close()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n详细报告已写入: {args.output}")


if __name__ == "__main__":
    main()
