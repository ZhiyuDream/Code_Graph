"""Issue/PR搜索工具 - 基于Embedding语义搜索"""
from __future__ import annotations

import json
import numpy as np
from pathlib import Path
from typing import List, Dict

from ..core.llm_client import get_llm_client

_ISSUE_INDEX = None
EMBEDDING_MODEL = "text-embedding-3-small"


def _load_issue_index() -> Dict:
    """加载Issue语义搜索索引"""
    global _ISSUE_INDEX
    if _ISSUE_INDEX is None:
        idx_path = Path(__file__).parent.parent.parent / "data" / "issue_rag_index.json"
        if idx_path.exists():
            with open(idx_path, 'r', encoding='utf-8') as f:
                _ISSUE_INDEX = json.load(f)
    return _ISSUE_INDEX or {}


def _cosine_sim(v1: list, v2: list) -> float:
    """计算余弦相似度"""
    a = np.array(v1)
    b = np.array(v2)
    norm = np.linalg.norm(a) * np.linalg.norm(b)
    if norm == 0:
        return 0.0
    return float(np.dot(a, b) / norm)


def search_issues(
    query: str,
    top_k: int = 3
) -> List[Dict]:
    """
    基于Embedding语义搜索相关Issue/PR
    
    Args:
        query: 查询文本
        top_k: 返回数量
        
    Returns:
        Issue列表
    """
    idx = _load_issue_index()
    if not idx:
        return []
    
    issues = idx.get("issues", [])
    embeddings = idx.get("embeddings", [])
    
    if not issues or not embeddings:
        return []
    
    # 获取查询文本的embedding
    try:
        client = get_llm_client()
        resp = client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=[query[:500]]
        )
        query_emb = resp.data[0].embedding
    except Exception:
        # Embedding失败时回退到关键词匹配
        return _keyword_search_issues(query, issues, top_k)
    
    # 计算相似度
    scores = []
    for i, issue in enumerate(issues):
        if i < len(embeddings):
            sim = _cosine_sim(query_emb, embeddings[i])
            scores.append((sim, issue))
    
    # 排序并返回top_k
    scores.sort(key=lambda x: -x[0])
    
    results = []
    for sim, issue in scores[:top_k]:
        results.append({
            'number': issue.get('number', ''),
            'title': issue.get('title', ''),
            'body': (issue.get('body', '') or '')[:300],
            'score': sim
        })
    
    return results


def _keyword_search_issues(query: str, issues: list, top_k: int) -> List[Dict]:
    """关键词匹配回退"""
    keywords = query.lower().split()
    scored_issues = []
    
    for issue in issues:
        score = 0
        text = f"{issue.get('title', '')} {(issue.get('body', '') or '')}".lower()
        
        for kw in keywords:
            if len(kw) > 2 and kw in text:
                score += 1
        
        if score > 0:
            scored_issues.append({
                'number': issue.get('number', ''),
                'title': issue.get('title', ''),
                'body': (issue.get('body', '') or '')[:300],
                'score': score
            })
    
    scored_issues.sort(key=lambda x: x['score'], reverse=True)
    return scored_issues[:top_k]
