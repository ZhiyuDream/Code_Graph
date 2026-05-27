"""
分层上下文答案生成器（方案2）。

数据流：
  阶段1：函数片段 → generate_answer() → 初稿（和 baseline 一样）
  阶段2：LLM 自检 → 判断是否需要补充完整文件
  阶段3（可选）：按需读取完整文件 → 重新生成答案（在初稿基础上补充）

baseline 完全不走这个流程，零影响。
"""
from __future__ import annotations

import json
from pathlib import Path

from tools.core.llm_client import call_llm_json, call_llm
from tools.search.code_reader import read_full_file
from tools.core.answer_generator import build_context

_PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "answer_completeness_check.txt"


def _check_completeness(
    question: str,
    draft: str,
    functions: list[dict],
    model: str = "deepseek-v4-pro",
    provider: str = "deepseek",
) -> dict:
    """
    阶段2：LLM 自检初稿是否完整。

    返回: {"need_supplement": bool, "reason": str, "files": [str], "missing_aspects": [str]}
    """
    # 构建函数摘要
    func_lines = []
    for i, fn in enumerate(functions[:10]):
        fp = fn.get("file", "")
        name = fn.get("name", "")
        score = fn.get("score", 0)
        func_lines.append(f"{i+1}. {name} ({fp}, score={score:.3f})")
    func_summary = "\n".join(func_lines)

    prompt_template = _PROMPT_PATH.read_text(encoding="utf-8")
    prompt = f"""{prompt_template}

---

【用户问题】
{question}

【已检索到的相关函数】
{func_summary}

【初稿答案】
{draft}

请输出 JSON："""

    result = call_llm_json(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=500,
        timeout=60,
        model=model,
        provider=provider,
    )

    if result is None:
        return {"need_supplement": False, "reason": "自检失败", "files": [], "missing_aspects": []}

    need = result.get("need_supplement", False)
    files = result.get("files", [])
    if not isinstance(files, list):
        files = []
    files = [f for f in files if isinstance(f, str) and f.strip()]

    return {
        "need_supplement": need and len(files) > 0,
        "reason": result.get("reason", ""),
        "files": files[:3],  # 最多 3 个
        "missing_aspects": result.get("missing_aspects", []),
    }


def generate_answer_hierarchical(
    question: str,
    collected: dict,
    generate_fn,
    max_tokens: int = 8192,
    model: str = "deepseek-v4-pro",
    provider: str = "deepseek",
    max_supplement_files: int = 3,
    max_supplement_tokens: int = 150000,
) -> tuple[str, dict]:
    """
    分层上下文答案生成。

    Args:
        question: 用户问题
        collected: 检索结果（包含 functions, issues, full_files 等）
        generate_fn: 答案生成函数（tools.core.generate_answer）
        max_tokens: 生成答案的最大 token 数
        model: 模型名称
        provider: 提供商
        max_supplement_files: 阶段3 最多补充的文件数
        max_supplement_tokens: 阶段3 的 token 预算

    Returns:
        (answer, meta) 其中 meta 包含阶段信息
    """
    meta = {
        "phase1_used": True,
        "phase2_used": True,
        "phase3_used": False,
        "supplement_files": [],
        "supplement_tokens": 0,
        "check_result": {},
    }

    funcs = collected.get("functions", [])

    # === 阶段1：用函数片段生成初稿 ===
    draft_collected = {
        "functions": funcs,
        "issues": collected.get("issues", []),
    }
    draft = generate_fn(
        question=question,
        collected=draft_collected,
        max_tokens=max_tokens,
        model=model,
        provider=provider,
    )

    # === 阶段2：LLM 自检 ===
    check = _check_completeness(
        question, draft, funcs, model=model, provider=provider
    )
    meta["check_result"] = check

    if not check.get("need_supplement", False):
        # 不需要补充，直接返回初稿
        return draft, meta

    # === 阶段3：按需读取完整文件，重新生成答案 ===
    meta["phase3_used"] = True

    # 读取 LLM 推荐的文件
    full_files = {}
    total_chars = 0
    max_chars = int(max_supplement_tokens * 2.5)
    single_file_max = 50000  # 50KB

    for fp in check["files"]:
        if len(full_files) >= max_supplement_files:
            break

        content = read_full_file(fp)
        if content.startswith("// 文件不存在") or content.startswith("// 读取文件失败"):
            continue

        # 单文件截断到 50KB（但保证不截断函数——这个在 read_full_file 里已经处理）
        if len(content) > single_file_max:
            content = content[:single_file_max] + "\n\n... [文件过大，已截断到 50KB] ...\n"

        if total_chars + len(content) > max_chars:
            remaining = max_chars - total_chars
            if remaining > 3000:
                content = content[:remaining] + "\n// [已达预算上限]\n"
                full_files[fp] = content
                total_chars += len(content)
            break

        full_files[fp] = content
        total_chars += len(content)

    meta["supplement_files"] = list(full_files.keys())
    meta["supplement_tokens"] = int(total_chars / 2.5)

    if not full_files:
        # 文件读取失败，返回初稿
        return draft, meta

    # === 阶段3：只提取证据，不重新生成答案 ===
    # 构建补充文件的上下文
    ff_context = build_context({"full_files": full_files})

    missing = ", ".join(check.get("missing_aspects", ["相关证据"]))
    files_str = "\n".join([f"- {fp}" for fp in full_files.keys()])

    evidence_prompt = f"""你是代码证据提取专家。你的任务是从补充文件中提取能支持初稿结论的代码证据。

【用户问题】
{question}

【初稿答案】
{draft}

【需要补充的方面】
{missing}

【补充文件内容】
{ff_context}

【任务要求】
1. 只从补充文件中提取与问题相关的代码证据
2. 每条证据必须包含：文件路径、行号、具体代码片段、证明了什么结论
3. 不要修改初稿中的任何结论
4. 不要输出与初稿重复的内容
5. 如果补充文件中找不到相关证据，明确说明"补充文件中未找到相关证据"

输出格式：
【补充证据】
1. `文件路径:行号` - 代码片段：... - 证明：...
2. `文件路径:行号` - 代码片段：... - 证明：...
...

如果没有找到相关证据，只输出：
【补充证据】
补充文件中未找到相关证据。"""

    evidence_text = call_llm(
        messages=[{"role": "user", "content": evidence_prompt}],
        max_tokens=max_tokens,
        model=model,
        provider=provider,
    )

    meta["evidence_text_length"] = len(evidence_text)

    # === 阶段4：后处理拼接 ===
    # 如果提取到了有效证据，拼接到初稿后面
    if "未找到相关证据" in evidence_text or evidence_text.strip() == "【补充证据】":
        # 没有有效证据，返回初稿
        meta["evidence_added"] = False
        return draft, meta

    # 拼接初稿 + 补充证据
    final = (
        draft
        + "\n\n"
        + "---\n\n"
        + "【补充证据】\n"
        + "基于完整文件补充的代码证据：\n\n"
        + evidence_text.replace("【补充证据】", "").strip()
    )

    meta["evidence_added"] = True
    return final, meta
