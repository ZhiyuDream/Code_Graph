"""Scope Planner — 基于文档索引决定搜索范围"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .document_index import (
    DocumentUnit,
    build_document_index,
    search_documents,
)

logger = logging.getLogger(__name__)

_SOURCE_EXTS = (
    ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp",
    ".cu", ".cuh", ".py", ".go", ".rs", ".java",
)


@dataclass
class SearchScope:
    """搜索范围"""
    documents: list[str]   # 相关文档 section ID 列表
    files: list[str]       # 相关文件路径列表（相对 repo_root）

    def to_file_filter(self) -> set[str] | None:
        if not self.files:
            return None
        return set(self.files)


class SearchScopePlanner:
    """
    搜索范围规划器（V0：极简版）。
    流程：Question → BM25 Top Sections → 提取文件引用 + 路径匹配 → 文件列表
    """

    def __init__(self, repo_root: str):
        self.repo_root = Path(repo_root)
        self.units, self.bm25 = build_document_index(repo_root)
        self._all_source_files: list[str] | None = None
        logger.info(
            "[ScopePlanner] Loaded %d document units from %s",
            len(self.units), repo_root,
        )

    def _get_all_source_files(self) -> list[str]:
        if self._all_source_files is None:
            files = []
            for ext in _SOURCE_EXTS:
                files.extend(self.repo_root.rglob(f"*{ext}"))
            self._all_source_files = sorted(
                {str(f.relative_to(self.repo_root)) for f in files}
            )
        return self._all_source_files

    def plan(self, question: str, top_k_docs: int = 3) -> SearchScope:
        """
        为问题规划搜索范围。
        """
        # 1. BM25 检索相关 sections
        top_sections = search_documents(
            question, self.units, self.bm25, top_k=top_k_docs,
        )

        if not top_sections:
            logger.warning(
                "[Scope] No relevant sections for: %s", question[:60],
            )
            return SearchScope(documents=[], files=[])

        doc_ids = [u.id for u, _ in top_sections]
        logger.info("[Scope] Top sections: %s", doc_ids)

        # 2. 从 sections 提取文件
        files: set[str] = set()
        mentioned_count = 0
        path_matched_count = 0

        for section, score in top_sections:
            # Signal 1: section 内容里显式提到的文件
            mentioned = self._extract_file_mentions(section.content)
            files.update(mentioned)
            mentioned_count += len(mentioned)

            # Signal 2: section 标题关键词 → 文件路径匹配
            path_matched = self._match_files_by_keywords(section.title)
            files.update(path_matched)
            path_matched_count += len(path_matched)

        # 3. 问题本身是否直接提到了文件名
        question_files = self._extract_file_mentions(question)
        files.update(question_files)

        file_list = sorted(files)[:20]

        logger.info(
            "[Scope] %d sections → %d files "
            "(mentioned=%d, path_matched=%d, from_question=%d)",
            len(top_sections), len(file_list),
            mentioned_count, path_matched_count, len(question_files),
        )

        return SearchScope(documents=doc_ids, files=file_list)

    # ------------------------------------------------------------------ #
    # 内部 helpers
    # ------------------------------------------------------------------ #

    def _extract_file_mentions(self, text: str) -> list[str]:
        """
        从文本中提取文件引用。
        匹配 `filename.cpp`、`src/path/file.cpp` 等。
        """
        # 匹配路径+文件名：允许字母数字下划线横线，扩展名为源码扩展名
        ext_group = "|".join(re.escape(e) for e in _SOURCE_EXTS)
        pattern = rf'(?:[\w\-]+/)*[\w\-]+\.(?:{ext_group})'

        found: set[str] = set()
        for match in re.finditer(pattern, text, re.IGNORECASE):
            fname = match.group()
            full = self._resolve_file(fname)
            if full:
                found.add(full)
        return list(found)

    def _resolve_file(self, fname: str) -> str | None:
        """根据文件名或路径片段，在仓库中找到完整相对路径。"""
        all_files = self._get_all_source_files()
        fname_norm = fname.replace("\\", "/")

        # 1. 直接相等
        for f in all_files:
            if f == fname_norm:
                return f

        # 2. 以 /fname 结尾
        for f in all_files:
            if f.endswith("/" + fname_norm):
                return f

        # 3. basename 相等
        basename = Path(fname_norm).name
        for f in all_files:
            if Path(f).name == basename:
                return f

        return None

    def _match_files_by_keywords(self, title: str) -> list[str]:
        """
        用 section 标题关键词匹配文件路径。
        例如 title="CUDA Backend" → 匹配路径含 "cuda" 或 "backend" 的文件。
        """
        keywords = _extract_keywords(title)
        if not keywords:
            return []

        all_files = self._get_all_source_files()
        matched = []
        for f in all_files:
            f_lower = f.lower()
            if any(kw in f_lower for kw in keywords):
                matched.append(f)
        return matched


def _extract_keywords(text: str) -> list[str]:
    """提取关键词（极简版）"""
    words = re.findall(r'[a-zA-Z]{3,}', text.lower())
    stopwords = {
        "the", "and", "for", "are", "but", "not", "you", "all", "can", "how",
        "does", "what", "why", "with", "from", "this", "that", "have", "has",
        "had", "was", "were", "been", "being", "will", "would", "could",
        "should", "may", "might", "doing", "done", "about", "into", "only",
        "other", "then", "under", "work", "also", "back", "after", "first",
        "well", "even", "new", "want", "because", "any", "these", "give",
        "day", "most", "us", "is", "be", "or", "an", "as", "at", "by", "on",
        "to", "of", "in", "it", "do", "did", "get", "got", "his", "him",
        "her", "she", "they", "them", "their", "there", "where", "when",
        "who", "which", "while", "than", "too", "very", "just", "now",
        "here", "over", "such", "take", "make", "made", "come", "came",
        "see", "saw", "know", "knew", "think", "thought", "say", "said",
        "use", "used", "find", "found", "tell", "told", "ask", "asked",
        "seem", "seemed", "feel", "felt", "try", "tried", "leave", "left",
        "call", "called", "good", "new", "old", "great", "high", "small",
        "different", "large", "next", "early", "young", "important", "public",
        "same", "able", "sure", "own", "long", "last", "late", "few", "little",
        "own", "right", "still", "yet", "already", "always", "never",
        "sometimes", "often", "usually", "really", "actually", "probably",
        "definitely", "certainly", "clearly", "obviously", "apparently",
        "basically", "generally", "specifically", "particularly", "especially",
        "mainly", "mostly", "partly", "fully", "completely", "entirely",
        "totally", "almost", "nearly", "roughly", "approximately", "exactly",
        "precisely", "literally", "simply", "easily", "quickly", "slowly",
        "carefully", "properly", "correctly", "directly", "immediately",
        "recently", "finally", "eventually", "suddenly", "gradually",
        "increasingly", "decreasingly", "significantly", "substantially",
        "considerably", "slightly", "somewhat", "somehow", "somewhere",
        "someone", "somebody", "something", "anyone", "anybody", "anything",
        "anywhere", "everyone", "everybody", "everything", "everywhere",
        "nobody", "nothing", "nowhere", "else", "rather", "quite", "pretty",
        "fairly", "extremely", "highly", "deeply", "strongly", "widely",
        "closely", "lightly", "heavily", "badly", "poorly", "well", "better",
        "best", "worse", "worst", "more", "most", "less", "least", "much",
        "many", "lot", "lots", "plenty", "enough", "several", "various",
        "numerous", "countless", "multiple", "single", "double", "triple",
        "alone", "together", "apart", "away", "down", "up", "off", "out",
        "around", "across", "through", "along", "among", "between", "within",
        "without", "against", "toward", "towards", "forward", "backward",
        "beyond", "above", "below", "beneath", "beside", "besides", "except",
        "including", "regarding", "concerning", "according", "depending",
        "following", "during", "before", "after", "since", "until", "till",
        "upon", "onto", "inside", "outside", "throughout", "notwithstanding",
        "despite", "although", "though", "whereas", "while", "unless", "until",
        "whether", "either", "neither", "both", "all", "none", "nor", "either",
        "neither", "whether", "if", "unless", "provided", "assuming",
        "supposing", "given", "considering", "regardless", "notwithstanding",
        "else", "otherwise", "instead", "meanwhile", "otherwise", "furthermore",
        "moreover", "however", "nevertheless", "nonetheless", "therefore",
        "thus", "hence", "consequently", "accordingly", "subsequently",
        "eventually", "finally", "initially", "originally", "previously",
        "formerly", "lately", "recently", "currently", "presently", "nowadays",
        "today", "tomorrow", "yesterday", "soon", "later", "earlier", "before",
        "afterwards", "meantime", "meanwhile", "simultaneously", "concurrently",
        "alternatively", "conversely", "similarly", "likewise", "otherwise",
        "instead", "rather", "instead", "besides", "additionally", "further",
        "moreover", "furthermore", "also", "too", "either", "neither", "both",
        "all", "each", "every", "either", "neither", "whether", "whatever",
        "whenever", "wherever", "however", "whoever", "whichever", "whomever",
        "anything", "anybody", "anyone", "anything", "anywhere", "everybody",
        "everyone", "everything", "everywhere", "nobody", "none", "nothing",
        "nowhere", "somebody", "someone", "something", "somewhere", "else",
    }
    return [w for w in words if w not in stopwords]
