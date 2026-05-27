"""语义搜索工具 - 基于预计算索引的代码检索"""
from __future__ import annotations

import json
import numpy as np
from typing import List, Dict
from pathlib import Path

from ..core.llm_client import get_llm_client
from .code_reader import enrich_function_with_code
from .frequency_penalty import apply_penalty, DEFAULT_PENALTY

EMBEDDING_MODEL = "text-embedding-3-small"
_RAG_INDEX = None


def _load_rag_index() -> dict | None:
    """加载RAG索引文件"""
    global _RAG_INDEX
    if _RAG_INDEX is None:
        idx_path = Path(__file__).resolve().parent.parent.parent / "data" / "classic_rag_index.json"
        if idx_path.exists():
            with open(idx_path, 'r', encoding='utf-8') as f:
                _RAG_INDEX = json.load(f)
    return _RAG_INDEX


def get_embedding(text: str) -> list[float]:
    """获取文本的embedding向量"""
    client = get_llm_client()
    resp = client.embeddings.create(
        model=EMBEDDING_MODEL,
        input=text[:8000]
    )
    return resp.data[0].embedding


def cosine_similarity(v1: list[float], v2: list[float]) -> float:
    """计算余弦相似度"""
    a = np.array(v1)
    b = np.array(v2)
    norm = np.linalg.norm(a) * np.linalg.norm(b)
    if norm == 0:
        return 0.0
    return float(np.dot(a, b) / norm)


def search_functions_by_text(
    query_text: str,
    top_k: int = 5,
    score_threshold: float = 0.0
) -> List[Dict]:
    """
    基于文本语义搜索函数
    
    Args:
        query_text: 查询文本
        top_k: 返回结果数量
        score_threshold: 相似度阈值
        
    Returns:
        函数列表，包含name, file, text, score
    """
    idx = _load_rag_index()
    if idx is None:
        return []
    
    # 获取查询embedding
    query_emb = get_embedding(query_text)
    
    # 只搜索function类型的chunk
    func_chunks = [(i, c) for i, c in enumerate(idx["chunks"]) if c["type"] == "function"]
    
    # 计算相似度
    scores = []
    for i, chunk in func_chunks:
        sim = cosine_similarity(query_emb, idx["embeddings"][i])
        if sim >= score_threshold:
            scores.append((sim, chunk))
    
    # 排序并返回top_k
    scores.sort(key=lambda x: -x[0])
    
    results = []
    for sim, chunk in scores[:top_k * 2]:  # 先取更多，降权后再截断
        meta = chunk.get("meta", {})
        func = {
            'name': meta.get('name', ''),
            'file': meta.get('file', ''),
            'text': chunk.get('text', ''),
            'score': sim,
            'source': 'embedding',
            'start_line': meta.get('start_line', meta.get('line', 0)),
            'end_line': meta.get('end_line', meta.get('line', 0))  # 信任 RAG index 的精确 end_line
        }
        # 补充完整代码
        func = enrich_function_with_code(func)
        results.append(func)
    
    # 去重：同名函数优先保留 text 更长的（实现 vs 声明）
    seen_names = {}
    deduped = []
    for func in results:
        name = func.get('name', '')
        if not name:
            deduped.append(func)
            continue
        if name in seen_names:
            # 保留 text 更长的（通常是实现而非声明）
            existing = seen_names[name]
            if len(func.get('text', '')) > len(existing.get('text', '')):
                # 替换已有记录
                for i, f in enumerate(deduped):
                    if f.get('name') == name:
                        deduped[i] = func
                        seen_names[name] = func
                        break
        else:
            seen_names[name] = func
            deduped.append(func)
    results = deduped
    
    # 高频函数降权（P2 优化）
    results = apply_penalty(results, penalty=DEFAULT_PENALTY)
    
    return results[:top_k]
