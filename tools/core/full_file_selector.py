"""
智能完整文件选择器。

核心设计：
1. LLM 先看函数片段，决定需要看哪些完整文件
2. 读取完整文件时，不按字符位置粗暴截断
3. 如果文件太大，改为提取该文件中的完整函数片段（函数级截断）

不影响 baseline 的 react_search 流程，只在 generate_answer 前按需调用。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from tools.search.code_reader import read_full_file
from tools.search.semantic_search import _load_rag_index
from tools.core.llm_client import call_llm_json

_PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "full_file_decision.txt"
_rag_index_cache = None


def _get_rag_index():
    global _rag_index_cache
    if _rag_index_cache is None:
        _rag_index_cache = _load_rag_index()
    return _rag_index_cache


def llm_decide_full_files(
    question: str,
    functions: list[dict],
    client=None,
    model: str = "deepseek-v4-pro",
    provider: str = "deepseek"
) -> dict:
    """
    让 LLM 决定需要看哪些完整文件。

    返回: {"need_full_files": bool, "reason": str, "files": [str]}
    """
    if not functions:
        return {"need_full_files": False, "reason": "无检索结果", "files": []}

    # 构建函数摘要
    func_lines = []
    for i, fn in enumerate(functions[:15]):
        fp = fn.get("file", "")
        name = fn.get("name", "")
        score = fn.get("score", 0)
        source = fn.get("source", "embedding")
        start = fn.get("start_line", 0)
        end = fn.get("end_line", 0)
        line_info = f":{start}-{end}" if start and end else ""
        func_lines.append(
            f"{i+1}. {name} ({fp}{line_info}, score={score:.3f}, source={source})"
        )
    func_summary = "\n".join(func_lines)

    # 读取 prompt 模板
    prompt_template = _PROMPT_PATH.read_text(encoding="utf-8")
    prompt = f"""{prompt_template}

---

【用户问题】
{question}

【已检索到的相关函数】
{func_summary}

请输出 JSON："""

    result = call_llm_json(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=500,
        timeout=60,
        model=model,
        provider=provider,
    )

    if result is None:
        return {"need_full_files": False, "reason": "LLM 决策失败", "files": []}

    need = result.get("need_full_files", False)
    files = result.get("files", [])
    if not isinstance(files, list):
        files = []
    # 过滤无效路径
    files = [f for f in files if isinstance(f, str) and f.strip()]

    return {
        "need_full_files": need and len(files) > 0,
        "reason": result.get("reason", ""),
        "files": files[:5],  # 最多 5 个
    }


def build_file_context(
    file_path: str,
    collected_functions: list[dict],
    max_chars: int = 50000
) -> str:
    """
    构建文件上下文。

    策略：
    1. 先尝试读取完整文件
    2. 如果完整文件能装下（<= max_chars），返回完整内容
    3. 如果太大，从 RAG index 提取该文件中的相关函数片段
       保证每个函数都是完整的，不会从中间截断
    """
    full_content = read_full_file(file_path)
    if full_content.startswith("// 文件不存在") or full_content.startswith("// 读取文件失败"):
        return ""

    # 情况1：完整文件能装下
    if len(full_content) <= max_chars:
        return full_content

    # 情况2：文件太大，函数级提取
    # 从 collected_functions 中找到属于该文件的函数
    file_funcs = [
        fn for fn in collected_functions
        if fn.get("file", "") == file_path
    ]

    if not file_funcs:
        # 该文件中没有已检索到的函数，尝试从 RAG index 找
        rag_index = _get_rag_index()
        if rag_index:
            for chunk in rag_index.get("chunks", []):
                if chunk.get("type") != "function":
                    continue
                meta = chunk.get("meta", {})
                if meta.get("file", "") == file_path:
                    file_funcs.append({
                        "name": meta.get("name", ""),
                        "file": file_path,
                        "text": chunk.get("text", ""),
                        "score": 0.3,
                    })

    if not file_funcs:
        # 实在找不到，返回文件头 + 前 max_chars 字符（最后手段）
        lines = full_content.splitlines()
        header_lines = []
        for line in lines:
            header_lines.append(line)
            if len("\n".join(header_lines)) > 2000:
                break
        header = "\n".join(header_lines)
        return (
            header
            + f"\n\n// [文件 {file_path} 过大 ({len(full_content)} 字符)，"
            "已切换为函数级提取，但未找到相关函数片段]\n"
        )

    # 按 score 排序，优先保留高相关函数
    file_funcs.sort(key=lambda f: f.get("score", 0), reverse=True)

    parts = [
        f"// ========== 文件: {file_path} ==========",
        "// [文件过大，以下为该文件中与问题相关的完整函数片段]\n",
    ]
    total_len = len("\n".join(parts))

    for fn in file_funcs:
        name = fn.get("name", "unknown")
        text = fn.get("text", "")
        func_block = (
            f"\n// ----- 函数: {name} -----\n"
            f"{text}\n"
        )
        if total_len + len(func_block) > max_chars:
            parts.append(
                f"\n// [还有更多相关函数，因预算限制未展示]\n"
            )
            break
        parts.append(func_block)
        total_len += len(func_block)

    return "\n".join(parts)


def collect_full_files_smart(
    collected: dict,
    question: str,
    client=None,
    model: str = "deepseek-v4-pro",
    provider: str = "deepseek",
    max_files: int = 10,
    max_tokens: int = 400000,
) -> dict:
    """
    主入口：LLM 决策 + 函数级文件收集。

    不影响 baseline 逻辑，只在需要时调用。
    """
    funcs = collected.get("functions", [])
    if not funcs:
        collected["full_files"] = {}
        return collected

    # 阶段1：LLM 决策
    decision = llm_decide_full_files(
        question, funcs, client=client, model=model, provider=provider
    )

    if not decision.get("need_full_files", False):
        collected["full_files"] = {}
        collected["full_files_decision"] = decision
        return collected

    # 阶段2：按预算读取文件
    full_files = {}
    total_chars = 0
    max_chars = int(max_tokens * 2.5)
    # 每个文件的预算（动态分配，但单文件不超过 50K 字符）
    per_file_budget = min(50000, max_chars // max_files)

    for fp in decision["files"]:
        if len(full_files) >= max_files:
            break

        context = build_file_context(
            fp,
            collected_functions=funcs,
            max_chars=per_file_budget,
        )

        if not context:
            continue

        # 检查总预算
        if total_chars + len(context) > max_chars:
            remaining = max_chars - total_chars
            if remaining > 3000:
                # 截断到预算内（这是最后手段，build_file_context 已经尽量保证完整性）
                context = context[:remaining] + "\n// [已达总预算上限]\n"
                full_files[fp] = context
            break

        full_files[fp] = context
        total_chars += len(context)

    collected["full_files"] = full_files
    collected["full_files_decision"] = decision
    return collected
