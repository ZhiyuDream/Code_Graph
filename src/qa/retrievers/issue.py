"""Issue 检索器 — 搜索 GitHub Issue/PR。

策略：
1. 关键词搜索（标题+正文包含）
2. 如果关键词无结果，fallback 到 embedding 语义搜索
3. 返回 Issue 标题、描述、关联 PR
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from .base import BaseRetriever, RetrievalResult
from config import OPENAI_API_KEY, OPENAI_BASE_URL, EMBEDDING_MODEL

logger = logging.getLogger(__name__)

_DEFAULT_INDEX = Path(__file__).resolve().parent.parent.parent.parent / "data" / "issue_rag_index.json"

_STOPWORDS = {"a", "an", "the", "in", "on", "at", "to", "for", "of", "and", "or", "but",
              "is", "are", "was", "were", "be", "with", "by", "from", "it", "its", "this",
              "that", "these", "those"}


def _jaccard_score(text: str, keyword: str) -> float:
    kw_words = {w.lower() for w in re.findall(r'\b\w+\b', keyword) if w.lower() not in _STOPWORDS and len(w) > 1}
    if not kw_words:
        return 0.5
    text_words = {w.lower() for w in re.findall(r'\b\w+\b', text) if w.lower() not in _STOPWORDS and len(w) > 1}
    inter = kw_words & text_words
    union = kw_words | text_words
    return len(inter) / len(union) if union else 0.0


class IssueRetriever(BaseRetriever):
    """基于 Neo4j Issue 节点和 embedding 的 Issue 检索器。"""

    def __init__(
        self,
        driver,
        database: str = "neo4j",
        enabled: bool = True,
        use_embedding_fallback: bool = True,
    ):
        super().__init__("issue", enabled)
        self.driver = driver
        self.database = database
        self.use_embedding_fallback = use_embedding_fallback
        self._issue_index: dict | None = None

    def _run(self, cypher: str, params: dict) -> list[dict]:
        with self.driver.session(database=self.database) as s:
            r = s.run(cypher, params)
            return [dict(rec) for rec in r]

    def _keyword_search(self, keyword: str, limit: int) -> list[dict]:
        """关键词搜索 Issue"""
        rows = self._run("""
            MATCH (i:Issue)
            WHERE toLower(i.title) CONTAINS toLower($kw)
               OR toLower(coalesce(i.body, '')) CONTAINS toLower($kw)
            RETURN i.number AS num, i.title AS title, i.body AS body,
                   i.ranking_score AS ranking_score, i.tier AS tier,
                   i.labels AS labels
            LIMIT 100
        """, {"kw": keyword})

        scored = []
        for r in rows:
            relevance = _jaccard_score((r.get("title") or "") + " " + (r.get("body") or "")[:500], keyword)
            ranking = float(r.get("ranking_score") or 0.5)
            scored.append((relevance * ranking, r))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [r for _, r in scored[:limit]]

    def _embedding_search(self, query: str, limit: int) -> list[dict]:
        """Embedding 语义搜索 Issue（fallback）"""
        if not self.use_embedding_fallback:
            return []

        idx_path = _DEFAULT_INDEX
        if not idx_path.exists():
            return []

        if self._issue_index is None:
            with open(idx_path, encoding="utf-8") as f:
                self._issue_index = json.load(f)

        idx = self._issue_index
        issues = idx.get("issues", [])
        embeddings = idx.get("embeddings", [])

        if not issues or not embeddings:
            return []

        try:
            from openai import OpenAI
            client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)
            resp = client.embeddings.create(model=EMBEDDING_MODEL, input=[query])
            query_emb = resp.data[0].embedding
        except Exception as e:
            logger.warning("Issue embedding search failed: %s", e)
            return []

        def _cosine_sim(a, b):
            dot = sum(x * y for x, y in zip(a, b))
            na = sum(x * x for x in a) ** 0.5
            nb = sum(y * y for y in b) ** 0.5
            return dot / (na * nb) if na and nb else 0.0

        scores = []
        for i, issue in enumerate(issues):
            if i < len(embeddings):
                sim = _cosine_sim(query_emb, embeddings[i])
                scores.append((sim, issue))

        scores.sort(key=lambda x: -x[0])
        return [{"num": issue.get("number", ""), "title": issue.get("title", ""),
                 "body": issue.get("body", ""), "score": sim}
                for sim, issue in scores[:limit]]

    def retrieve(self, question: str, top_k: int = 3) -> list[RetrievalResult]:
        if not self.enabled:
            return []

        # 提取核心关键词（取最长的实义词）
        words = re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]{2,}\b", question)
        keywords = [w for w in words if w.lower() not in _STOPWORDS][:3]
        if not keywords:
            keywords = [question[:30]]

        results = []
        seen = set()

        for kw in keywords:
            rows = self._keyword_search(kw, limit=top_k)
            for r in rows:
                num = str(r.get("num", ""))
                if num in seen:
                    continue
                seen.add(num)
                content = f"Issue #{num}: {r.get('title', '')}\n{r.get('body', '')[:400]}"
                results.append(RetrievalResult(
                    id=f"issue:{num}",
                    type="issue",
                    content=content,
                    metadata={
                        "number": num,
                        "title": r.get("title", ""),
                        "tier": r.get("tier", ""),
                    },
                    score=float(r.get("ranking_score", 0.5)),
                    source="issue",
                ))

        # fallback
        if len(results) < top_k and self.use_embedding_fallback:
            emb_rows = self._embedding_search(question, limit=top_k)
            for r in emb_rows:
                num = str(r.get("num", ""))
                if num in seen:
                    continue
                seen.add(num)
                content = f"Issue #{num}: {r.get('title', '')}\n{r.get('body', '')[:400]}"
                results.append(RetrievalResult(
                    id=f"issue:{num}",
                    type="issue",
                    content=content,
                    metadata={"number": num, "title": r.get("title", "")},
                    score=r.get("score", 0.5),
                    source="issue_embedding",
                ))

        return results[:top_k]
