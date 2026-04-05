import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT / "src"))   # 核心库
sys.path.insert(0, str(_ROOT))             # 根目录（config.py）

#!/usr/bin/env python3
"""
用 LLM 重写 Issue 驱动题的答案。

原答案 = 粗暴拼接 Issue body 原文，质量差。
目标答案 = 开发者视角的分析：症状说明 → 可能原因 → 排查思路 → 已知结论。

用法：python rewrite_issue_answers.py [--dry-run] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import pandas as pd

from config import LLM_MODEL, OPENAI_API_KEY, OPENAI_BASE_URL

OUTPUT = Path(__file__).resolve().parent / "llama_cpp_QA_v3.csv"

SYSTEM_PROMPT = """你是一名 llama.cpp 核心开发者，擅长分析 Issue 报告、排查 bug、解释架构决策。
请用中文回答，语气专业简洁，结构清晰。"""

USER_TMPL = """下面是一个关于 llama.cpp 的问题，以及对应的 Issue 原始信息（可能包含噪声）。

## 问题
{question}

## Issue #{issue_num} 原始信息（仅供参考）
标题：{issue_title}
内容摘要：
{issue_body}

## 要求
请根据以上信息，以开发者角度写一个高质量的回答，包含：
1. **症状描述**：这个问题的核心现象是什么
2. **可能原因**：结合 llama.cpp 架构分析可能的根因
3. **排查思路**：如何诊断和定位这个问题
4. **已知结论**：如果 Issue 中有明确结论或修复方案，请说明

回答长度控制在 200-350 字，不要照抄 Issue 原文。"""

USER_TMPL_FEATURE = """下面是一个关于 llama.cpp 功能需求的问题，以及对应的 Issue 原始信息。

## 问题
{question}

## Issue #{issue_num} 原始信息（仅供参考）
标题：{issue_title}
内容摘要：
{issue_body}

## 要求
请根据以上信息，以开发者角度写一个高质量的回答，包含：
1. **需求背景**：这个功能需求的动机和使用场景
2. **当前状态**：llama.cpp 是否已支持，或者有什么替代方案
3. **社区态度**：Issue 中社区的反馈和讨论方向
4. **使用建议**：给开发者的实用建议

回答长度控制在 200-350 字，不要照抄 Issue 原文。"""

USER_TMPL_PERF = """下面是一个关于 llama.cpp 性能问题的问题，以及对应的 Issue 原始信息。

## 问题
{question}

## Issue #{issue_num} 原始信息（仅供参考）
标题：{issue_title}
内容摘要：
{issue_body}

## 要求
请根据以上信息，以开发者角度写一个高质量的回答，包含：
1. **性能瓶颈**：这个性能问题的核心原因
2. **影响范围**：哪些场景/硬件/模型受影响
3. **优化方向**：已知的优化手段或配置建议
4. **参考信息**：Issue 中提到的关键数据或结论

回答长度控制在 200-350 字，不要照抄 Issue 原文。"""

USER_TMPL_FIX = """下面是一个关于 llama.cpp bug 修复链路的问题，以及 Issue+PR 的原始信息。

## 问题
{question}

## Issue #{issue_num} 原始信息（仅供参考）
标题：{issue_title}
内容摘要：
{issue_body}

## 要求
请根据以上信息，以开发者角度写一个高质量的回答，包含：
1. **问题症状**：这个 bug 的具体表现
2. **根本原因**：导致这个 bug 的技术原因
3. **修复思路**：应该从哪个方向入手排查和修复
4. **修复方案**：如果已有 PR 修复，简述修复方案

回答长度控制在 200-350 字，不要照抄 Issue 原文。"""


def extract_issue_num(evidence: str) -> str:
    m = re.search(r'Issue #(\d+)', str(evidence))
    return m.group(1) if m else ""


def extract_issue_title(raw_answer: str) -> str:
    """从原答案的第一行提取 issue number，从 github_data 里找 title"""
    return ""


def get_template(subcat: str) -> str:
    if subcat in ("Bug修复链路",):
        return USER_TMPL_FIX
    elif subcat in ("Feature需求",):
        return USER_TMPL_FEATURE
    elif subcat in ("性能问题",):
        return USER_TMPL_PERF
    else:  # Bug排查
        return USER_TMPL


def call_llm(client, question: str, subcat: str, issue_num: str,
             issue_title: str, issue_body: str) -> str:
    tmpl = get_template(subcat)
    user_msg = tmpl.format(
        question=question,
        issue_num=issue_num,
        issue_title=issue_title,
        issue_body=issue_body[:600],
    )
    resp = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.3,
        max_tokens=600,
        timeout=60,
    )
    return resp.choices[0].message.content.strip()


def load_github_data() -> dict:
    """加载 github_pr_issue_data.json，建立 issue_num -> issue 的索引"""
    data_path = Path(__file__).resolve().parent / "experiments" / "github_pr_issue_data.json"
    if not data_path.exists():
        return {"issues": {}, "prs": {}}
    with open(data_path, encoding="utf-8") as f:
        raw = json.load(f)
    issues_by_num = {}
    for item in raw.get("issues", []):
        num = str(item.get("number", item.get("num", "")))
        if num:
            issues_by_num[num] = item
    return {"issues": issues_by_num}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只打印，不保存")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 条（调试用）")
    args = parser.parse_args()

    if not OPENAI_API_KEY:
        print("ERROR: OPENAI_API_KEY 未设置", file=sys.stderr)
        sys.exit(1)

    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)

    df = pd.read_csv(OUTPUT, encoding="utf-8")
    github = load_github_data()
    issues_by_num = github["issues"]

    issue_mask = df["类别"] == "Issue驱动"
    total = issue_mask.sum()
    print(f"Issue 驱动题共 {total} 条，开始 LLM 重写...")

    updated = 0
    errors = 0
    indices = df[issue_mask].index.tolist()
    if args.limit > 0:
        indices = indices[:args.limit]

    for i, idx in enumerate(indices):
        row = df.loc[idx]
        question = str(row["问题"])
        subcat = str(row["子类"])
        old_answer = str(row["答案"])
        evidence = str(row.get("Evidence", ""))

        # 提取 issue_num
        issue_num = extract_issue_num(evidence)
        if not issue_num:
            # 从答案第一行提取
            m = re.search(r'Issue #(\d+)', old_answer)
            issue_num = m.group(1) if m else "?"

        # 找 issue title + body
        issue_info = issues_by_num.get(issue_num, {})
        issue_title = issue_info.get("title", "(unknown)")
        # 原答案里包含了 clean 后的 body，直接用作 context
        # 去掉答案开头的 "Issue #XXXX 记录了这个问题。" 前缀
        body_in_answer = re.sub(r'^(Issue #\d+[^。]*。|这个问题在 Issue #\d+[^。]*。)', '', old_answer).strip()
        # 优先用 github data 中的原始 body
        if issue_info.get("body"):
            from generate_qa_v3 import clean_issue_body
            body_context = clean_issue_body(issue_info["body"], 600)
        else:
            body_context = body_in_answer[:600]

        print(f"[{i+1}/{len(indices)}] idx={idx} Issue#{issue_num} {subcat}: {question[:60]}...")

        if args.dry_run:
            print(f"  [dry-run] 跳过 LLM 调用")
            continue

        try:
            new_answer = call_llm(client, question, subcat, issue_num,
                                  issue_title, body_context)
            df.at[idx, "答案"] = new_answer
            updated += 1
            print(f"  -> {new_answer[:80]}...", flush=True)
            # 每 5 条增量保存一次
            if updated % 5 == 0:
                df.to_csv(OUTPUT, index=False, encoding="utf-8")
                print(f"  [checkpoint] 已保存 {updated} 条", flush=True)
        except Exception as e:
            print(f"  ERROR: {e}", flush=True)
            errors += 1

        time.sleep(0.3)  # 避免限速

    print(f"\n完成：重写 {updated} 条，失败 {errors} 条")

    if not args.dry_run and updated > 0:
        df.to_csv(OUTPUT, index=False, encoding="utf-8")
        print(f"已保存: {OUTPUT}")


if __name__ == "__main__":
    main()
