"""Embedding 语义检索器。

基于预计算的 embedding 索引，通过 cosine 相似度做语义检索。
索引构建：从 Neo4j 提取 Function + Issue，计算 embedding 后存本地 JSON。
在线查询：计算 query embedding，排序返回 top-k。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from openai import OpenAI

from .base import BaseRetriever, RetrievalResult
from config import OPENAI_API_KEY, OPENAI_BASE_URL, EMBEDDING_MODEL

logger = logging.getLogger(__name__)

_DEFAULT_INDEX = Path(__file__).resolve().parent.parent.parent.parent / "data" / "qa_embedding_index.json"


def _cosine_sim(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


class EmbeddingRetriever(BaseRetriever):
    """基于 Embedding 的语义检索器。"""

    def __init__(
        self,
        index_path: Path | str | None = None,
        enabled: bool = True,
        model: str = EMBEDDING_MODEL,
    ):
        super().__init__("embedding", enabled)
        self.index_path = Path(index_path) if index_path else _DEFAULT_INDEX
        self.model = model
        self._index: dict | None = None
        self._client: OpenAI | None = None

    def _get_client(self) -> OpenAI:
        if self._client is None:
            self._client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)
        return self._client

    def _load_index(self) -> dict:
        if self._index is None:
            if not self.index_path.exists():
                raise RuntimeError(f"Embedding index not found: {self.index_path}")
            with open(self.index_path, encoding="utf-8") as f:
                self._index = json.load(f)
        return self._index

    def build_index(
        self,
        driver,
        database: str = "neo4j",
        repo_root: Path | str = "",
        force: bool = False,
    ) -> None:
        """构建 embedding 索引（只需运行一次）。"""
        if self.index_path.exists() and not force:
            logger.info("Embedding index already exists at %s", self.index_path)
            return

        from neo4j import GraphDatabase
        repo_root = Path(repo_root) if repo_root else Path(".")
        client = self._get_client()
        chunks: list[dict] = []

        with driver.session(database=database) as s:
            r = s.run("""
                MATCH (f:Function)
                RETURN f.id AS id, f.name AS name, f.file_path AS file_path,
                       f.start_line AS start_line, f.end_line AS end_line,
                       f.signature AS signature
                ORDER BY f.fan_in DESC
            """)
            for rec in r:
                code = _read_code(
                    repo_root, rec["file_path"],
                    rec["start_line"] or 0, rec["end_line"] or 0
                )
                sig = rec.get("signature") or ""
                text = f"Function: {rec['name']}\nFile: {rec['file_path']}\nSignature: {sig}\n\n{code}"
                chunks.append({
                    "id": rec["id"],
                    "type": "function",
                    "text": text,
                    "meta": {
                        "name": rec["name"],
                        "file_path": rec["file_path"],
                        "start_line": rec["start_line"],
                        "end_line": rec["end_line"],
                        "signature": sig,
                    },
                })

            try:
                r = s.run("""
                    MATCH (i:Issue)
                    RETURN i.id AS id, i.title AS title, i.question AS question,
                           i.answer AS answer, i.body AS body
                    LIMIT 1000
                """)
                for rec in r:
                    text = f"Issue: {rec.get('title') or rec.get('question', '')}\n{rec.get('answer', '') or rec.get('body', '')}"[:2000]
                    chunks.append({
                        "id": rec["id"],
                        "type": "issue",
                        "text": text,
                        "meta": {"title": rec.get("title", "")},
                    })
            except Exception:
                pass

        logger.info("Embedding %d chunks...", len(chunks))
        texts = [c["text"] for c in chunks]
        embeddings: list[list[float]] = []
        for i in range(0, len(texts), 64):
            batch = texts[i:i + 64]
            resp = client.embeddings.create(model=self.model, input=batch)
            for e in sorted(resp.data, key=lambda x: x.index):
                embeddings.append(e.embedding)
            logger.info("  embedded %d/%d", min(i + 64, len(texts)), len(texts))

        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self.index_path.write_text(
            json.dumps({"chunks": chunks, "embeddings": embeddings}, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("Index saved to %s", self.index_path)
        self._index = None  # 强制下次重新加载

    def retrieve(
        self,
        question: str,
        top_k: int = 5,
        file_filter: set[str] | None = None,
    ) -> list[RetrievalResult]:
        if not self.enabled:
            return []

        index = self._load_index()
        client = self._get_client()

        try:
            resp = client.embeddings.create(model=self.model, input=[question])
            query_emb = resp.data[0].embedding
        except Exception as e:
            logger.warning("Embedding API error: %s", e)
            return []

        scores = []
        filtered_count = 0
        total_count = len(index["embeddings"])

        for i, emb in enumerate(index["embeddings"]):
            chunk = index["chunks"][i]
            if file_filter:
                chunk_file = chunk.get("meta", {}).get("file_path", "")
                if chunk_file not in file_filter:
                    filtered_count += 1
                    continue
            sim = _cosine_sim(query_emb, emb)
            scores.append((sim, i))

        if file_filter:
            logger.info(
                "[Embedding] Filtered %d/%d chunks (%.1f%% in scope)",
                total_count - filtered_count,
                total_count,
                (total_count - filtered_count) / total_count * 100,
            )

        scores.sort(key=lambda x: -x[0])

        results = []
        for sim, idx in scores[:top_k]:
            chunk = index["chunks"][idx]
            results.append(RetrievalResult(
                id=chunk["id"],
                type=chunk["type"],
                content=chunk["text"],
                metadata=chunk.get("meta", {}),
                score=round(sim, 4),
                source="embedding",
            ))

        return results


def _read_code(repo_root: Path, file_path: str, start_line: int, end_line: int, max_chars: int = 3000) -> str:
    abs_path = repo_root / file_path
    if not abs_path.exists():
        return ""
    try:
        with open(abs_path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        s = max(0, start_line - 1)
        e = min(len(lines), end_line)
        code = "".join(lines[s:e])
        if len(code) > max_chars:
            code = code[:max_chars] + "\n... (truncated)"
        return code
    except Exception:
        return ""
