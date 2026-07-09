"""Utilities for building function-level candidate pools."""
from config import get_repo_root
from src.qa.retrievers.fast_embedding import FastEmbeddingRetriever


def _normalize_path(path: str) -> str:
    repo_root = str(get_repo_root())
    if path.startswith(repo_root):
        path = path[len(repo_root):].lstrip('/')
    return path.lstrip('./').lstrip('/')


def build_function_pool(query_emb,
                        retriever: FastEmbeddingRetriever,
                        top_k_files: int = 20,
                        top_m_functions: int = 5,
                        max_pool_size: int | None = None) -> list[dict]:
    """Two-stage pool construction: top files -> top functions per file.

    Returns a globally ranked list of candidate functions. Each candidate has:
      - file_path
      - name
      - score
      - signature
      - text (function body)
    """
    file_results = retriever.retrieve(query_emb, top_k=top_k_files * 3)
    file_scores = {}
    for r in file_results:
        fp = _normalize_path(r["metadata"].get("file_path", ""))
        if fp:
            file_scores[fp] = max(file_scores.get(fp, 0), r["score"])
    top_files = sorted(file_scores.items(), key=lambda x: -x[1])[:top_k_files]

    all_functions = []
    seen = set()
    for fp, _ in top_files:
        func_results = retriever.retrieve(query_emb, top_k=top_m_functions, file_filter={fp})
        for r in func_results:
            name = r["metadata"].get("name", "")
            key = (fp, name)
            if name and key not in seen:
                seen.add(key)
                all_functions.append({
                    "file_path": fp,
                    "name": name,
                    "score": r["score"],
                    "signature": r["metadata"].get("signature", ""),
                    "text": r["text"],
                })
    all_functions.sort(key=lambda x: -x["score"])
    if max_pool_size:
        all_functions = all_functions[:max_pool_size]
    return all_functions
