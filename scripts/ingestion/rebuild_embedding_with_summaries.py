#!/usr/bin/env python3
"""用函数职责重建 embedding 索引。

新文本 = 函数名 + 一句职责 + 签名 + 代码前 800 字符
（原版 = 函数名 + 文件路径 + 签名 + 代码全文截断 16000）

输出：data/qa_embedding_index_v2.json（保持 chunks+embeddings 结构）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from src.core.embedding_client import get_encoder

INDEX = _ROOT / "data" / "qa_embedding_index.json"
SUMM = _ROOT / "data" / "function_summaries.json"
OUT = _ROOT / "data" / "qa_embedding_index_v2.json"


def main():
    data = json.load(open(INDEX))
    chunks = data["chunks"]
    summaries = json.load(open(SUMM))
    print(f"chunks {len(chunks)}, summaries {len(summaries)}")

    n_with = 0
    for c in chunks:
        m = c["meta"]
        summ = summaries.get(c["id"], "")
        if summ:
            n_with += 1
        code = c.get("text", "")
        # 去掉原 text 里的头部行，只留代码部分的前 800 字符
        code_body = code.split("\n\n", 1)[-1][:800]
        c["text"] = (
            f"Function: {m['name'][:200]}\n"
            f"File: {m['file_path']}\n"
            f"Summary: {summ}\n"
            f"Signature: {(m.get('signature') or '')[:200]}\n\n"
            f"{code_body}"
        )
    print(f"带职责的函数: {n_with}/{len(chunks)}")

    encoder = get_encoder()
    texts = [c["text"] for c in chunks]
    embeddings = []
    for i in range(0, len(texts), 64):
        embs = encoder.encode(texts[i:i + 64])
        embeddings.extend(embs.tolist())
        if (i // 64) % 20 == 0:
            print(f"  embedded {i}/{len(texts)}", flush=True)

    OUT.write_text(json.dumps({"chunks": chunks, "embeddings": embeddings}, ensure_ascii=False))
    print(f"写入 {OUT}")


if __name__ == "__main__":
    main()
