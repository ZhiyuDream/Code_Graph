"""批量内容分诊：子 agent 一次读一批候选函数的真实代码，逐个判与问题的相关性。

动机：名字/签名级分诊会在"名字不像但代码相关"的函数上误杀
（013 案例：get_cache_acl_tensor 名字像"获取 tensor"，代码却是缓存扩容本体）。
内容分诊让 LLM 看真实代码再判，一次大调用完成，不占主 Agent 的步数和上下文。
"""
from __future__ import annotations

from pathlib import Path

from src.core.llm_client import call_llm, call_llm_json
from src.core.prompt_loader import load_prompt
from src.qa.tools.grep_call_chain import read_function as _raw_read_function


def triage_functions_batched(
    question: str,
    candidates: list[dict],
    repo_root: Path,
    model: str,
    batch_size: int = 24,
    max_candidates: int = 96,
    workers: int = 4,
) -> dict:
    """分批并发分诊：覆盖池内前 max_candidates 个候选（默认 96 = 4 批 × 24）。

    24 个一批是单调用上下文和判定质量的平衡点；批间并发，总耗时≈单批耗时。
    """
    from concurrent.futures import ThreadPoolExecutor
    cands = candidates[:max_candidates]
    batches = [cands[i:i + batch_size] for i in range(0, len(cands), batch_size)]
    scores: dict = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for part in ex.map(
            lambda b: triage_functions_llm(question, b, repo_root, model), batches
        ):
            scores.update(part)
    return scores


def redescribe_missing_function(question: str, model: str, usage_sink: list | None = None) -> str:
    """HyDE 式反查：召回池全场低分时，让 LLM 描述"正确的函数应该长什么样"。

    返回一段英文技术描述（假设性函数名/行为/关键操作），供 embedding 重新召回。
    针对"问题词汇 ≠ 代码词汇"（013：'缓存扩容' ↔ get_cache_acl_tensor）。
    """
    prompt = load_prompt("triage_redescribe").format(question=question)
    out = call_llm(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=None,
        model=model,
        _usage_sink=usage_sink,
    )
    return (out or "").strip()


def triage_functions_llm(
    question: str,
    candidates: list[dict],
    repo_root: Path,
    model: str,
    usage_sink: list | None = None,
) -> dict:
    """对候选函数批量读代码 + LLM 相关性分诊（单批）。

    返回 {key: {"score": 0-10, "reason": str}}，key 为 "name@file"。
    分诊失败的候选不给分（上层按中立分处理，不误杀）。

    代码完整不截断：截断会系统性误判（关键语义可能在函数任何位置），
    宁可单次调用大一些，也不让分诊建立在残缺代码上。
    """
    blocks = []
    keys = []
    for c in candidates:
        name, fp = c["name"], c["file_path"].split(":")[0]
        key = f"{name}@{fp}"
        keys.append(key)
        result = _raw_read_function(name, fp, repo_root)
        code = result.get("code", "")
        if not code:
            blocks.append(f"### {key}\n(代码读取失败: {result.get('error', '未知')}，仅有签名: {c.get('signature', '')[:100]})")
            continue
        blocks.append(f"### {key}\n```cpp\n{code}\n```")

    prompt = load_prompt("pool_triage").format(
        question=question,
        functions_block="\n\n".join(blocks),
    )
    out = call_llm_json(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=None,
        model=model,
        _usage_sink=usage_sink,
    )
    scores: dict = {}
    evals = (out or {}).get("evaluations") if isinstance(out, dict) else None
    if isinstance(evals, list):
        for e in evals:
            if not isinstance(e, dict):
                continue
            name, fp = e.get("name", ""), e.get("file", "")
            try:
                score = float(e.get("score"))
            except (TypeError, ValueError):
                continue
            score = max(0.0, min(10.0, score))
            # 精确匹配 key；匹配不上就按裸名+文件 basename 模糊匹配
            key = f"{name}@{fp}"
            if key not in keys:
                matched = [k for k in keys
                           if k.split("@")[0] == name and k.split("@", 1)[-1].endswith(fp[-30:])]
                if len(matched) == 1:
                    key = matched[0]
                else:
                    continue
            scores[key] = {"score": score, "reason": str(e.get("reason", ""))[:60]}
    return scores
