"""Document Index — 按 markdown section 切分，建 BM25 索引"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from rank_bm25 import BM25Okapi


@dataclass
class DocumentUnit:
    """一个文档单元（markdown 的一个 section）"""
    id: str           # "README.md#Supported-backends"
    title: str        # "Supported backends"
    content: str      # section 文本（不含标题行）
    source_file: str  # "README.md"


def _tokenize(text: str) -> list[str]:
    """简单分词：提取字母词，过滤短词和常见停用词。"""
    words = re.findall(r'[a-zA-Z]{3,}', text.lower())
    stopwords = {
        "the", "and", "for", "are", "but", "not", "you", "all", "can", "had",
        "her", "was", "one", "our", "out", "get", "has", "him", "his", "how",
        "man", "new", "now", "old", "see", "two", "way", "who", "did", "its",
        "let", "put", "say", "she", "too", "use", "with", "have", "this",
        "will", "your", "from", "they", "know", "want", "been", "good", "much",
        "some", "time", "very", "when", "come", "here", "just", "like", "long",
        "make", "many", "over", "such", "take", "than", "them", "well", "were",
        "what", "would", "there", "their", "said", "each", "which", "does",
        "could", "should", "may", "might", "being", "doing", "done", "about",
        "into", "only", "other", "then", "under", "work", "also", "back",
        "after", "first", "well", "way", "even", "new", "want", "because",
        "any", "these", "give", "day", "most", "us", "is", "was", "were",
        "be", "or", "an", "as", "at", "by", "on", "to", "of", "in", "it",
    }
    return [w for w in words if w not in stopwords]


def split_markdown_into_sections(file_path: str, text: str) -> list[DocumentUnit]:
    """
    把 markdown 文本按 H2/H3 切分成 sections。
    如果没有 H2/H3，把整个文件作为一个 section。
    """
    lines = text.split('\n')
    sections: list[DocumentUnit] = []
    current_title: str | None = None
    current_content: list[str] = []
    file_basename = Path(file_path).name

    for line in lines:
        h2_match = re.match(r'^##\s+(.+)$', line)
        h3_match = re.match(r'^###\s+(.+)$', line)

        if h2_match or h3_match:
            if current_title is not None:
                content = '\n'.join(current_content).strip()
                if content:
                    sections.append(DocumentUnit(
                        id=f"{file_path}#{current_title}",
                        title=current_title,
                        content=content,
                        source_file=file_path,
                    ))
            current_title = (h2_match or h3_match).group(1).strip()
            # 去掉 markdown 链接标记，保留纯文本
            current_title = re.sub(r'\[(.*?)\]\(.*?\)', r'\1', current_title)
            current_content = []
        else:
            current_content.append(line)

    # 保存最后一个 section
    if current_title is not None:
        content = '\n'.join(current_content).strip()
        if content:
            sections.append(DocumentUnit(
                id=f"{file_path}#{current_title}",
                title=current_title,
                content=content,
                source_file=file_path,
            ))

    # 如果整个文件没有 H2/H3，把整个文件作为一个 section
    if not sections:
        content = text.strip()
        if content:
            h1_match = re.search(r'^#\s+(.+)$', text, re.MULTILINE)
            title = h1_match.group(1).strip() if h1_match else file_basename
            sections.append(DocumentUnit(
                id=file_path,
                title=title,
                content=content,
                source_file=file_path,
            ))

    return sections


def build_document_index(repo_root: str) -> tuple[list[DocumentUnit], BM25Okapi | None]:
    """
    遍历 README + docs/**/*.md，切块，建 BM25 索引。
    Returns: (units, bm25_index)
    """
    repo_root = Path(repo_root)
    units: list[DocumentUnit] = []

    # README.md
    readme_path = repo_root / "README.md"
    if readme_path.exists():
        text = readme_path.read_text(encoding="utf-8", errors="ignore")
        units.extend(split_markdown_into_sections("README.md", text))

    # docs/**/*.md
    docs_dir = repo_root / "docs"
    if docs_dir.exists():
        for md_file in docs_dir.rglob("*.md"):
            rel_path = str(md_file.relative_to(repo_root))
            text = md_file.read_text(encoding="utf-8", errors="ignore")
            units.extend(split_markdown_into_sections(rel_path, text))

    if not units:
        return units, None

    tokenized_docs = [_tokenize(u.title + " " + u.content) for u in units]
    bm25 = BM25Okapi(tokenized_docs)

    return units, bm25


def search_documents(
    query: str,
    units: list[DocumentUnit],
    bm25: BM25Okapi | None,
    top_k: int = 3,
) -> list[tuple[DocumentUnit, float]]:
    """
    BM25 检索最相关的 DocumentUnit。
    Returns: [(DocumentUnit, score), ...] 按分数降序
    """
    if bm25 is None or not units:
        return []

    tokenized_query = _tokenize(query)
    scores = bm25.get_scores(tokenized_query)

    top_indices = np.argsort(scores)[::-1][:top_k]

    results = []
    for idx in top_indices:
        if scores[idx] > 0:
            results.append((units[idx], float(scores[idx])))

    return results
