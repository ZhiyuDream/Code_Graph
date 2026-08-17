#!/usr/bin/env python3
"""构建文件级 embedding 索引（File Profile 版）。

File Profile = 路径 + 函数名列表 + 类名列表 + 签名（截断），不塞全文、不用 LLM 总结。

输出：data/file_embedding_index.json
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from src.core.embedding_client import get_encoder
from src.core.neo4j_client import run_cypher

OUT = _ROOT / "data" / "file_embedding_index.json"


def main():
    funcs = run_cypher(
        "MATCH (f:Function) RETURN f.file_path AS fp, f.name AS name, f.signature AS sig"
    )
    classes = run_cypher("MATCH (c:Class) RETURN c.file_path AS fp, c.name AS name")

    files: dict[str, dict] = defaultdict(lambda: {"funcs": [], "sigs": [], "classes": []})
    for r in funcs:
        files[r["fp"]]["funcs"].append(r["name"])
        if r.get("sig"):
            files[r["fp"]]["sigs"].append(r["sig"][:120])
    for r in classes:
        files[r["fp"]]["classes"].append(r["name"])

    profiles = []
    for fp, d in sorted(files.items()):
        func_names = "\n".join(d["funcs"][:60])
        sigs = "\n".join(d["sigs"][:30])
        text = (
            f"File: {fp}\n"
            f"Classes: {', '.join(d['classes'][:20])}\n"
            f"Functions ({len(d['funcs'])}):\n{func_names}\n"
            f"Signatures:\n{sigs}"
        )[:8000]
        profiles.append({"file_path": fp, "text": text, "n_functions": len(d["funcs"])})

    print(f"文件数: {len(profiles)}")
    encoder = get_encoder()
    texts = [p["text"] for p in profiles]
    embeddings = []
    for i in range(0, len(texts), 64):
        embeddings.extend(encoder.encode(texts[i:i + 64]).tolist())
        print(f"  embedded {min(i + 64, len(texts))}/{len(texts)}", flush=True)

    OUT.write_text(json.dumps({"profiles": profiles, "embeddings": embeddings}, ensure_ascii=False))
    print(f"写入 {OUT}")


if __name__ == "__main__":
    main()
