#!/usr/bin/env python3
"""SkillOpt 去敏试点：反思失败轨迹 → 有界编辑 → 去敏校验 → 门控。

与 microsoft/SkillOpt 对应的简化版：
- 冻结 worker（deepseek-v4-flash），只优化 skill 文本
- 有界编辑：add/delete/replace，每轮最多 4 条
- 去敏防线：编辑内容禁止出现具体符号名/文件名/目录名/后端名（防过拟合）
- 验证门控：候选 skill 在 selection 题集上严格更优才接受

用法：
  # 1. 反思+生成候选 skill（不跑 QA，只做文本优化）
  .venv/bin/python scripts/qa/skillopt_loop.py propose \
      --traces results/qa_react_v38_full.json \
      --skill data/qa_skill.md \
      --out data/qa_skill_candidate.md

  # 2. 门控验证（各跑 selection 题集对比）——由调用方用 QA_SKILL_PATH 分别跑
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from src.core.llm_client import call_llm_json

# 去敏防线：编辑内容命中以下任一模式即拒绝（通用策略不应引用具体仓库实体）
_TOOL_NAMES = (
    "read_function|read_lines|list_functions|list_files|find_callers|find_callees|"
    "search_symbol|expand_recall|mark_file_irrelevant|skip_candidates|function_relevance|action_input"
)
FORBIDDEN_PATTERNS = [
    r"\.(cpp|cc|cxx|c|h|hh|hpp|hxx)\b",   # 文件名
    r"\b(ggml|llama|sycl|cann|webgpu|virtgpu|miniaudio|opencl|vulkan|metal|cuda|hexagon|blas|rpc|zendnn|zdnn)[\w:]*\b",
]

PROPOSE_PROMPT = """你是调查策略优化器。一个代码审计 agent 按以下【当前策略】调查仓库，以下是它完全失败（零引用）的【失败轨迹摘要】。

【当前策略】
{skill}

【失败轨迹摘要】
{traces}

---

任务：提出最多 {max_edits} 条对策略文本的编辑，让 agent 在这类失败上表现得更好。

硬性约束（违反即作废）：
1. 编辑必须是**通用程序性规则**（适用于任何仓库、任何问题），禁止出现具体符号名、文件名、目录名、后端名、公司/项目名
2. 不要重写整个策略，只提局部编辑
3. 每条编辑针对一个可复用的失败模式，不要针对单个 case
4. 保持策略总长度不超过现在的 1.3 倍

【输出格式】（必须是有效的 JSON）
{{
  "analysis": "失败共性的一句话诊断",
  "edits": [
    {{"op": "add|delete|replace", "target": "（replace/delete 时填被替换的策略原文片段，add 时填空）", "content": "新文本"}}
  ]
}}
"""


def is_clean(text: str) -> bool:
    """去敏校验：文本不得含具体仓库实体。"""
    for pat in FORBIDDEN_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            return False
    # 路径检测：去掉工具名后仍有 a/b 形式即视为具体路径
    stripped = re.sub(_TOOL_NAMES, "", text)
    if re.search(r"[\w\-]+/[\w\-]+", stripped):
        return False
    return True


def apply_edits(skill: str, edits: list[dict]) -> str:
    out = skill
    for e in edits:
        op = e.get("op")
        content = (e.get("content") or "").strip()
        target = (e.get("target") or "").strip()
        if not content and op != "delete":
            continue
        if op == "add":
            out = out.rstrip() + "\n" + content + "\n"
        elif op == "delete" and target and target in out:
            out = out.replace(target, "", 1)
        elif op == "replace" and target and target in out:
            out = out.replace(target, content, 1)
    return out


def summarize_traces(traces_path: Path, eval_path: Path | None = None, max_q: int = 6) -> str:
    """从零引用题的 trace 提取压缩摘要（每题：问题+动作序列+监督判错+答案开头）。"""
    results = {r["qa_id"]: r for r in json.load(open(traces_path))}
    if eval_path and eval_path.exists():
        zero_ids = [e["qa_id"] for e in json.load(open(eval_path))
                    if e["eval_citation"]["coverage_ratio"] == 0]
    else:
        zero_ids = list(results)
    parts = []
    for qid in zero_ids[:max_q]:
        item = results.get(qid)
        if not item:
            continue
        notes = item.get("supervisor_notes", [])
        wrong = [x for x in notes if x.get("direction") == "错误"]
        actions = " → ".join(s["action"] for s in item.get("steps", []))
        parts.append(
            f"■ 问题: {item['question'][:100]}\n"
            f"  动作序列: {actions[:300]}\n"
            f"  监督判错: {wrong[0].get('assessment', '')[:100] if wrong else '（无）'}\n"
            f"  答案开头: {item.get('answer', '')[:150]}"
        )
    return "\n\n".join(parts)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("propose")
    p.add_argument("--traces", type=Path, required=True)
    p.add_argument("--eval", type=Path, default=None, help="评估结果（用于定位零引用题）")
    p.add_argument("--skill", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--model", default="deepseek-v4-flash")
    p.add_argument("--max-edits", type=int, default=4)
    args = ap.parse_args()

    skill = args.skill.read_text(encoding="utf-8") if args.skill.exists() else ""
    if not skill:
        from scripts.qa.run_react_concept_symbol_qa import DEFAULT_SKILL
        skill = DEFAULT_SKILL
        args.skill.parent.mkdir(parents=True, exist_ok=True)
        args.skill.write_text(skill, encoding="utf-8")
        print(f"初始化 skill -> {args.skill}")

    traces = summarize_traces(args.traces, args.eval)
    if not traces:
        print("没有找到零相关的失败轨迹"); return

    r = call_llm_json(
        messages=[{"role": "user", "content": PROPOSE_PROMPT.format(
            skill=skill, traces=traces, max_edits=args.max_edits)}],
        max_tokens=2000, model=args.model,
    )
    if not isinstance(r, dict) or not r.get("edits"):
        print("optimizer 未返回有效编辑"); return

    print(f"诊断: {r.get('analysis', '')}")
    accepted, rejected = [], []
    for e in r["edits"][:args.max_edits]:
        text = f"{e.get('target', '')} {e.get('content', '')}"
        if is_clean(text):
            accepted.append(e)
        else:
            rejected.append(e)

    for e in rejected:
        print(f"[去敏拒绝] {e.get('op')}: {(e.get('content') or e.get('target') or '')[:80]}")
    if not accepted:
        print("所有编辑都被去敏防线拒绝，策略保持不变"); return

    candidate = apply_edits(skill, accepted)
    if len(candidate) > len(skill) * 1.3:
        print("候选超过长度上限 1.3x，拒绝"); return

    args.out.write_text(candidate, encoding="utf-8")
    print(f"\n接受 {len(accepted)} 条编辑，候选已写入 {args.out}")
    for e in accepted:
        print(f"  [{e.get('op')}] {(e.get('content') or '')[:100]}")
    print(f"\n门控验证：QA_SKILL_PATH={args.out} 跑 selection 题集，与当前 skill 对比")


if __name__ == "__main__":
    main()
