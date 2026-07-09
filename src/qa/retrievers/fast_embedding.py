"""Fast local embedding retriever backed by qa_embedding_index.json.

Avoids repeated API calls by loading the full embedding matrix once and
computing cosine similarity locally.
"""
import json
from pathlib import Path

import numpy as np

from config import EMBEDDING_MODEL
from src.core.embedding_client import get_encoder


def _normalize_path(path: str, repo_root: str) -> str:
    if path.startswith(repo_root):
        path = path[len(repo_root):].lstrip('/')
    return path.lstrip('./').lstrip('/')


def _cosine_sim_matrix(query_embs: np.ndarray, doc_embs: np.ndarray) -> np.ndarray:
    qn = np.linalg.norm(query_embs, axis=1, keepdims=True)
    dn = np.linalg.norm(doc_embs, axis=1, keepdims=True)
    qn[qn == 0] = 1e-10
    dn[dn == 0] = 1e-10
    return (query_embs @ doc_embs.T) / (qn @ dn.T)


class FastEmbeddingRetriever:
    """Loads qa_embedding_index.json and supports fast local retrieval."""

    def __init__(self, index_path: Path | None = None, repo_root: str | None = None):
        if index_path is None:
            index_path = Path(__file__).resolve().parents[3] / "data" / "qa_embedding_index.json"
        self.index_path = index_path
        self.repo_root = repo_root or ""
        self.chunks: list[dict] = []
        self.doc_matrix: np.ndarray | None = None
        self.file_to_chunks: dict[str, list[int]] = {}
        self.chunk_by_name_file: dict[tuple[str, str], dict] = {}
        self._load()

    def _load(self) -> None:
        with open(self.index_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.chunks = data["chunks"]
        embs = np.asarray(data["embeddings"], dtype=np.float32)
        self.doc_matrix = embs
        for idx, ch in enumerate(self.chunks):
            meta = ch.get("meta", {})
            fp = meta.get("file_path", "")
            self.file_to_chunks.setdefault(fp, []).append(idx)
            name = meta.get("name", "")
            self.chunk_by_name_file[(name, fp)] = ch

    def encode_queries(self, queries: list[str]) -> np.ndarray:
        """Encode queries via unified embedding client."""
        encoder = get_encoder(model=EMBEDDING_MODEL)
        return encoder.encode(queries)

    def normalize(self, path: str) -> str:
        return _normalize_path(path, self.repo_root)

    def retrieve(self, query_emb: np.ndarray, top_k: int = 5,
                 file_filter: set[str] | None = None) -> list[dict]:
        if file_filter is not None:
            indices = []
            for fp in file_filter:
                indices.extend(self.file_to_chunks.get(fp, []))
            if not indices:
                return []
            sub_matrix = self.doc_matrix[indices]
            sims = _cosine_sim_matrix(query_emb.reshape(1, -1), sub_matrix)[0]
            pairs = [(sims[i], indices[i]) for i in range(len(indices))]
        else:
            sims = _cosine_sim_matrix(query_emb.reshape(1, -1), self.doc_matrix)[0]
            pairs = [(sims[i], i) for i in range(len(self.chunks))]
        pairs.sort(key=lambda x: -x[0])
        results = []
        for sim, idx in pairs[:top_k]:
            ch = self.chunks[idx]
            results.append({
                "score": round(float(sim), 4),
                "metadata": ch.get("meta", {}),
                "text": ch.get("text", ""),
            })
        return results

    def get_function_text(self, name: str, file_path: str) -> str:
        key = (name, file_path)
        ch = self.chunk_by_name_file.get(key)
        if ch:
            return ch.get("text", "")
        # Fallback: search all chunks with same name
        for ch in self.chunks:
            if ch.get("meta", {}).get("name") == name:
                return ch.get("text", "")
        return ""
