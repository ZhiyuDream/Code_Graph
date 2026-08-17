#!/usr/bin/env python3
"""为每个函数生成一句职责描述（deepseek-v4-flash，批量）。

输出：data/function_summaries.json  {fid: summary}
支持断点续跑：已存在的 fid 会跳过。
"""
from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from src.core.llm_client import call_llm_json

INDEX = _ROOT / "data" / "qa_embedding_index.json"
OUT = _ROOT / "data" / "function_summaries.json"
BATCH = 10
MODEL = "deepseek-v4-flash"
WORKERS = 30

PROMPT = """为以下每个 C++ 函数写一句职责描述（≤40 字，中文，只写这个函数做什么，不要评价）。

输出 JSON：{{"summaries": ["职责1", "职责2", ...]}}，顺序与输入一一对应，数量必须一致。

函数列表：
{funcs}
"""


def fmt_func(chunk: dict) -> str:
    m = chunk["meta"]
    code = chunk.get("text", "")
    # text 已含 Function:/File:/Signature: 头部，截断到 1200 字符控制成本
    return f"--- {m['name']} @ {m['file_path']}:{m['start_line']}-{m['end_line']} ---\n{code[:1200]}"


def gen_batch(chunks: list[dict]) -> list[str]:
    prompt = PROMPT.format(funcs="\n\n".join(fmt_func(c) for c in chunks))
    for _ in range(3):
        try:
            r = call_llm_json(messages=[{"role": "user", "content": prompt}], max_tokens=1200, model=MODEL)
            if isinstance(r, dict) and isinstance(r.get("summaries"), list):
                s = r["summaries"]
                if len(s) == len(chunks):
                    return [str(x).strip() for x in s]
        except Exception:
            time.sleep(2)
    return [""] * len(chunks)


def main():
    data = json.load(open(INDEX))
    chunks = data["chunks"]
    done = {}
    if OUT.exists():
        done = json.load(open(OUT))
    todo = [c for c in chunks if c["id"] not in done]
    print(f"总函数 {len(chunks)}，已完成 {len(done)}，待生成 {len(todo)}")

    batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]
    t0 = time.time()
    finished = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(gen_batch, b): b for b in batches}
        for fut in as_completed(futs):
            b = futs[fut]
            sums = fut.result()
            for c, s in zip(b, sums):
                if s:
                    done[c["id"]] = s
            finished += 1
            if finished % 50 == 0:
                OUT.write_text(json.dumps(done, ensure_ascii=False))
                rate = finished / (time.time() - t0) * BATCH * 60
                print(f"[{finished}/{len(batches)}] 累计 {len(done)}，速度 {rate:.0f} 函数/分", flush=True)
    OUT.write_text(json.dumps(done, ensure_ascii=False))
    print(f"完成：{len(done)}/{len(chunks)}，输出 {OUT}")


if __name__ == "__main__":
    main()
