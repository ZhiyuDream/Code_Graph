#!/usr/bin/env python3
"""
Classic RAG baseline：
  1. 离线构建：把图中所有 Function 注解 + Issue/PR 文本 chunk 化，计算 embedding，存到本地 JSON
  2. 在线检索：对问题做 embedding，取 top-k chunk，拼 prompt，调 LLM 生成答案

用法：
  # 构建索引（只需一次）
  python tools/classic_rag.py build

  # 跑评测
  python tools/classic_rag.py run --csv results/qav2.csv --output results/classic_rag_results.json [--limit N]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# ── path setup ──────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent  # Code_Graph root
sys.path.insert(0, str(_ROOT / "src"))   # 核心库（neo4j_writer 等）
sys.path.insert(0, str(_ROOT))            # 根目录（config.py）

from config import (NEO4J_DATABASE, OPENAI_API_KEY, OPENAI_BASE_URL,
                    LLM_MODEL, EMBEDDING_MODEL)
from neo4j_writer import get_driver

INDEX_PATH = Path(__file__).resolve().parent.parent / "data" / "classic_rag_index.json"
TOP_K = 6
CHUNK_MAX = 400


# ---------------------------------------------------------------------------
# 构建索引
# ---------------------------------------------------------------------------

def build_chunks(driver) -> list[dict]:
    """从 Neo4j 提取所有 chunk，每个 chunk 有 text + metadata"""
    chunks = []

    with driver.session(database=NEO4J_DATABASE) as s:
        # 获取所有函数（无论是否有 annotation_json）
        r = s.run("""
            MATCH (f:Function)
            WHERE f.file_path IS NOT NULL
            RETURN f.name AS name, f.file_path AS file,
                   f.signature AS signature, f.start_line AS start_line
        """)
        for rec in r:
            sig = rec.get("signature") or ""
            text = (f"函数: {rec['name']}\n文件: {rec['file']}\n"
                    f"签名: {sig}")
            chunks.append({
                "id": f"func::{rec['name']}::{rec['file']}:{rec['start_line']}",
                "type": "function",
                "text": text[:CHUNK_MAX],
                "meta": {"name": rec["name"], "file": rec["file"]},
            })

        # Issue/PR 可能不存在，跳过
        try:
            r = s.run("""
                MATCH (i:Issue)
                RETURN i.number AS num, i.title AS title, i.body AS body
                LIMIT 1000
            """)
            for rec in r:
                body = (rec.get("body") or "")[:300]
                text = f"Issue #{rec['num']}: {rec['title']}\n{body}"
                chunks.append({
                    "id": f"issue::{rec['num']}",
                    "type": "issue",
                    "text": text[:CHUNK_MAX],
                    "meta": {"num": rec["num"], "title": rec["title"]},
                })
        except Exception:
            pass

        try:
            r = s.run("""
                MATCH (p:PullRequest)
                RETURN p.number AS num, p.title AS title, p.body AS body
                LIMIT 1000
            """)
            for rec in r:
                body = (rec.get("body") or "")[:200]
                text = f"PR #{rec['num']}: {rec['title']}\n{body}"
                chunks.append({
                    "id": f"pr::{rec['num']}",
                    "type": "pr",
                    "text": text[:CHUNK_MAX],
                    "meta": {"num": rec["num"], "title": rec["title"]},
                })
        except Exception:
            pass

    return chunks


def embed_texts(client, texts: list[str], batch_size: int = 64) -> list[list[float]]:
    out = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        resp = client.embeddings.create(model=EMBEDDING_MODEL, input=batch)
        for e in sorted(resp.data, key=lambda x: x.index):
            out.append(e.embedding)
        print(f"  embedded {min(i + batch_size, len(texts))}/{len(texts)}", flush=True)
    return out


def cmd_build(_args):
    if not OPENAI_API_KEY:
        print("ERROR: OPENAI_API_KEY 未设置", file=sys.stderr)
        sys.exit(1)
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)

    driver = get_driver()
    driver.verify_connectivity()
    print("提取 chunks...")
    chunks = build_chunks(driver)
    driver.close()
    print(f"共 {len(chunks)} 个 chunks（Function/Issue/PR）")

    print("计算 embedding...")
    texts = [c["text"] for c in chunks]
    embeddings = embed_texts(client, texts)

    index = {"chunks": chunks, "embeddings": embeddings}
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    print(f"索引已保存: {INDEX_PATH}")


# ---------------------------------------------------------------------------
# 检索 + 生成
# ---------------------------------------------------------------------------

def cosine_sim(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def retrieve(query_emb: list[float], index: dict, top_k: int = TOP_K) -> list[dict]:
    scores = [(i, cosine_sim(query_emb, emb))
              for i, emb in enumerate(index["embeddings"])]
    scores.sort(key=lambda x: -x[1])
    results = []
    for i, score in scores[:top_k]:
        chunk = dict(index["chunks"][i])
        chunk["score"] = round(score, 4)
        results.append(chunk)
    return results


def generate_answer(client, question: str, chunks: list[dict]) -> tuple[str, dict]:
    context = "\n\n---\n\n".join(c["text"] for c in chunks)
    if len(context) > 4000:
        context = context[:4000] + "\n...(已截断)"
    prompt = f"""你是 llama.cpp 代码库的专家助手。请根据下面的参考信息，用中文回答用户问题。

【参考信息】
{context}

【用户问题】
{question}

请根据参考信息回答。如果参考信息不足以回答，请如实说明。
回答格式：【你的回答】"""
    resp = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=700,
        timeout=60,
    )
    usage = resp.usage.model_dump() if resp.usage else {}
    return (resp.choices[0].message.content or "").strip(), usage


def parse_evidence(evidence_str: str) -> set[str]:
    import re
    if not evidence_str or not isinstance(evidence_str, str):
        return set()
    out = set()
    for m in re.finditer(r'Issue #(\d+)', evidence_str):
        out.add(f"Issue #{m.group(1)}")
    for m in re.finditer(r'PR #(\d+)', evidence_str):
        out.add(f"PR #{m.group(1)}")
    for m in re.finditer(r'(?:src|ggml|common|tests|tools|examples|vendor)[/\\][^\s:,]+', evidence_str):
        out.add(m.group(0))
    return out


def calc_evidence_hit(gold: set[str], retrieved_chunks: list[dict]) -> dict:
    if not gold:
        return {"evidence_count": 0, "hit_count": 0, "recall": None}
    import re
    mentioned = set()
    for c in retrieved_chunks:
        text = c["text"]
        for m in re.finditer(r'Issue #(\d+)', text):
            mentioned.add(f"Issue #{m.group(1)}")
        for m in re.finditer(r'PR #(\d+)', text):
            mentioned.add(f"PR #{m.group(1)}")
        for m in re.finditer(r'(?:src|ggml|common|tests)[/\\][^\s:,]+', text):
            mentioned.add(m.group(0))
    hits = gold & mentioned
    recall = len(hits) / len(gold) if gold else None
    return {
        "evidence_count": len(gold),
        "hit_count": len(hits),
        "recall": round(recall, 4) if recall is not None else None,
        "hits": list(hits),
    }


def cmd_run(args):
    if not OPENAI_API_KEY:
        print("ERROR: OPENAI_API_KEY 未设置", file=sys.stderr)
        sys.exit(1)
    if not INDEX_PATH.exists():
        print(f"ERROR: 索引不存在，请先运行 'python classic_rag.py build'", file=sys.stderr)
        sys.exit(1)

    from openai import OpenAI
    import pandas as pd
    client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)

    print("加载索引...", flush=True)
    index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    print(f"  {len(index['chunks'])} chunks", flush=True)

    df = pd.read_csv(args.csv, encoding="utf-8")
    if args.limit > 0:
        df = df.head(args.limit)

    total = len(df)
    results = []

    for i, row in df.iterrows():
        idx = row.get("index", i)
        category = row.get("类别", row.get("level1", ""))
        subcat = row.get("子类", row.get("level2", ""))
        question = row.get("具体问题", row.get("question", ""))
        evidence_raw = str(row.get("Evidence", row.get("evidence", "")))
        gold = parse_evidence(evidence_raw)

        print(f"[{i+1}/{total}] {category}/{subcat}: {question[:60]}...", flush=True)
        t0 = time.time()
        try:
            q_emb = client.embeddings.create(
                model=EMBEDDING_MODEL, input=[question]).data[0].embedding
            top_chunks = retrieve(q_emb, index, top_k=TOP_K)
            answer, token_usage = generate_answer(client, question, top_chunks)
            latency = round(time.time() - t0, 2)
            ev_hit = calc_evidence_hit(gold, top_chunks)
            results.append({
                "index": int(idx),
                "类别": category,
                "子类": subcat,
                "具体问题": question,
                "参考答案": str(row.get("答案", row.get("full_answer", ""))) if pd.notna(row.get("答案", "")) else "",
                "生成答案": answer,
                "Evidence": evidence_raw,
                "证据命中": ev_hit,
                "检索chunks": [{"id": c["id"], "score": c["score"]} for c in top_chunks],
                "延迟_s": latency,
                "错误": None,
                "token_usage": token_usage,
            })
        except Exception as e:
            results.append({
                "index": int(idx),
                "类别": category,
                "子类": subcat,
                "具体问题": question,
                "参考答案": str(row.get("答案", row.get("full_answer", ""))) if pd.notna(row.get("答案", "")) else "",
                "生成答案": "",
                "Evidence": evidence_raw,
                "证据命中": {},
                "检索chunks": [],
                "延迟_s": 0,
                "错误": str(e),
                "token_usage": {},
            })

    import csv as csvlib
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        writer = csvlib.DictWriter(f, fieldnames=[
            "index", "类别", "子类", "具体问题", "参考答案", "生成答案",
            "Evidence", "证据命中", "检索chunks", "延迟_s", "错误", "token_usage"
        ])
        writer.writeheader()
        writer.writerows(results)

    print(f"结果已保存: {args.output}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("build", help="构建 embedding 索引")

    run_p = sub.add_parser("run", help="跑评测")
    run_p.add_argument("--csv", type=Path,
                       default=Path(__file__).resolve().parent.parent / "results" / "qav2.csv")
    run_p.add_argument("--output", type=Path,
                       default=Path(__file__).resolve().parent.parent / "results" / "classic_rag_results.json")
    run_p.add_argument("--limit", type=int, default=0)

    args = parser.parse_args()
    if args.cmd == "build":
        cmd_build(args)
    elif args.cmd == "run":
        cmd_run(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
