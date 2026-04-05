import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT / "src"))   # 核心库
sys.path.insert(0, str(_ROOT))             # 根目录（config.py）

#!/usr/bin/env python3
"""
llama.cpp Issue AI 质量评估脚本
先抓取 N 条 closed issues，再对每条调用 LLM 评估其作为代码问答证据的价值。
输出按 quality_score 排序。
"""

import argparse
import json
import subprocess
import time
from pathlib import Path

from openai import OpenAI

# 加载 .env 配置
import sys
sys.path.insert(0, str(Path(__file__).parent))
from config import OPENAI_API_KEY, OPENAI_BASE_URL, LLM_MODEL


# ─────────────────────────────────────────────────────────────────────────────
# GitHub 数据抓取
# ─────────────────────────────────────────────────────────────────────────────

def fetch_issues(repo: str, count: int, labels: list[str] | None = None):
    """通过 gh CLI 抓取指定数量的 closed issues（排除 PR）"""
    all_issues = []
    page = 1

    while len(all_issues) < count:
        # 注意：/issues 端点返回 issues + PRs，需要过滤 pull_request 字段
        cmd = f'gh api "repos/{repo}/issues?state=closed&per_page=100&page={page}"'
        if labels:
            # URL-encode 标签（空格用 %20 或 +）
            label_str = ",".join(labels)
            cmd = f'gh api "repos/{repo}/issues?state=closed&per_page=100&page={page}&labels={label_str}"'

        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if result.returncode != 0 or not result.stdout.strip():
            break

        try:
            items = json.loads(result.stdout)
        except json.JSONDecodeError:
            break

        if not items:
            break

        for item in items:
            # 过滤掉 PR（pull_request 字段非空表示是 PR）
            if item.get("pull_request"):
                continue
            all_issues.append(item)

        if len(items) < 100:
            break
        page += 1
        time.sleep(0.2)

    return all_issues[:count]


# ─────────────────────────────────────────────────────────────────────────────
# LLM 评估
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """你是一个代码问答证据质量评估器。

你的任务：评估一条 GitHub Issue 是否包含可供代码 RAG 系统使用的代码上下文证据。

评分标准（0~1，分数越高越适合入库）：

0.0-0.2  不入库
  - 纯讨论/提问，无代码引用
  - 仅 "我也有这个问题" / "+1"
  - 文档修改、CI配置、脚本问题

0.3-0.4  可考虑入库
  - 有文件路径或函数名提及
  - 描述较完整但无 commit

0.5-0.7  建议入库
  - 有 commit SHA 引用
  - 有代码片段（函数调用、变量声明）
  - 有 expected/actual 错误描述模式

0.8-1.0  强烈建议入库
  - commit SHA + 多文件引用 + 详细错误日志
  - 清晰的复现步骤
  - body > 5000 chars

输出格式（纯 JSON，无 Markdown 包裹）：
{
  "quality_score": 0.XX,
  "入库建议": "入库 / 排除 / 可考虑",
  "理由": "一句话说明",
  "关键信号": ["commit SHA", "文件路径", "代码片段", "expected/actual", "长body", ...],
  "标签": ["bug", "performance", ...]
}"""

USER_PROMPT_TEMPLATE = """Issue Title: {title}
Issue Number: #{number}
Labels: {labels}
Created: {created_at}
Closed: {closed_at}

Body:
{body}

请评估这条 Issue 的代码问答证据质量，输出 JSON："""


def eval_issue(client: OpenAI, issue: dict) -> dict:
    """对单条 issue 调用 LLM 评估质量"""
    title = issue.get("title", "")
    number = issue.get("number", 0)
    body = issue.get("body", "") or "(无 body)"
    labels = [l["name"] for l in issue.get("labels", [])]
    created = issue.get("created_at", "")[:10]
    closed = issue.get("closed_at", "")[:10] if issue.get("closed_at") else "未关闭"

    user_prompt = USER_PROMPT_TEMPLATE.format(
        title=title,
        number=number,
        labels=", ".join(labels) if labels else "无",
        created_at=created,
        closed_at=closed,
        body=body[:8000],  # 限制 body 长度避免上下文爆炸
    )

    try:
        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0,
            max_tokens=512,
        )
        raw = response.choices[0].message.content.strip()

        # 尝试解析 JSON（可能包含在 markdown 里）
        if "```json" in raw:
            raw = raw.split("```json")[1].split("```")[0]
        elif "```" in raw:
            raw = raw.split("```")[1].split("```")[0]

        result = json.loads(raw.strip())
        result["number"] = number
        result["title"] = title
        result["gh_url"] = issue.get("html_url", f"https://github.com/ggml-org/llama.cpp/issues/{number}")
        result["raw_response"] = raw[:200]
        return result

    except Exception as e:
        return {
            "number": number,
            "title": title,
            "quality_score": None,
            "入库建议": "评估失败",
            "理由": str(e)[:100],
            "关键信号": [],
            "labels": labels,
            "gh_url": issue.get("html_url", ""),
            "raw_response": "",
        }


def eval_batch(client: OpenAI, issues: list[dict], concurrency: int = 5) -> list[dict]:
    """并发评估多条 issue"""
    import concurrent.futures

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {ex.submit(eval_issue, client, issue): issue for issue in issues}
        for i, future in enumerate(concurrent.futures.as_completed(futures)):
            result = future.result()
            results.append(result)
            score_str = f"{result['quality_score']:.2f}" if result.get("quality_score") is not None else "N/A"
            print(f"  [{i+1}/{len(issues)}] #{result['number']} score={score_str}  {result['入库建议']}  |  {result.get('理由', '')[:60]}")

    return results


# ─────────────────────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="AI 评估 GitHub Issue 质量")
    parser.add_argument("--count", "-n", type=int, default=100, help="评估多少条 issue（默认100）")
    parser.add_argument("--repo", "-r", default="ggml-org/llama.cpp", help="仓库（默认 ggml-org/llama.cpp）")
    parser.add_argument("--labels", "-l", nargs="*", help="只评估指定标签的 issues")
    parser.add_argument("--min-score", "-s", type=float, default=0.0, help="只输出分数>=此值的 issue")
    parser.add_argument("--output", "-o", type=str, help="输出 JSON 文件路径")
    parser.add_argument("--concurrency", "-c", type=int, default=5, help="并发评估数量（默认5）")
    args = parser.parse_args()

    # 初始化 LLM 客户端
    if not OPENAI_API_KEY:
        print("错误：未设置 OPENAI_API_KEY")
        sys.exit(1)

    base_url = OPENAI_BASE_URL.rstrip("/") if OPENAI_BASE_URL else "https://api.openai.com/v1"
    client = OpenAI(api_key=OPENAI_API_KEY, base_url=base_url)

    print(f"抓取 {args.count} 条 closed issues from {args.repo}...")
    if args.labels:
        print(f"  标签过滤: {', '.join(args.labels)}")
    issues = fetch_issues(args.repo, args.count, args.labels)
    print(f"获取到 {len(issues)} 条 issues\n")

    if not issues:
        print("未获取到任何 issues，退出。")
        return

    print("开始 AI 评估...\n")
    results = eval_batch(client, issues, concurrency=args.concurrency)

    # 按质量分数排序
    scored = [r for r in results if r.get("quality_score") is not None]
    scored.sort(key=lambda x: -x["quality_score"])

    failed = [r for r in results if r.get("quality_score") is None]

    # 输出汇总
    print("\n" + "=" * 80)
    print("评 估 结 果 汇 总")
    print("=" * 80)

    print(f"\n总计: {len(issues)} 条 | 成功评估: {len(scored)} 条 | 失败: {len(failed)} 条")
    print(f"平均分: {sum(r['quality_score'] for r in scored) / len(scored):.3f}" if scored else "N/A")

    # 分布
    bins = [(0.8, 1.0, "0.80-1.00 强烈建议入库"),
            (0.5, 0.8, "0.50-0.79 建议入库"),
            (0.3, 0.5, "0.30-0.49 可考虑入库"),
            (0.0, 0.3, "0.00-0.29 排除")]
    for lo, hi, label in bins:
        cnt = sum(1 for r in scored if lo <= r["quality_score"] < hi)
        pct = cnt / len(scored) * 100 if scored else 0
        print(f"  {label}: {cnt} 条 ({pct:.1f}%)")

    # 详细列表
    print(f"\n{'─'*80}")
    print(f"TOP 20（按质量分数降序）:")
    print(f"{'─'*80}")
    for r in scored[:20]:
        tags = ", ".join(r.get("labels", [])[:4])
        signals = ", ".join(r.get("关键信号", [])[:4])
        print(f"\n  #{r['number']}  [{r['quality_score']:.2f}]  {r['入库建议']}")
        print(f"    Title: {r['title'][:70]}")
        if tags:
            print(f"    Labels: {tags}")
        if signals:
            print(f"    信号: {signals}")
        print(f"    理由: {r.get('理由', '')}")
        print(f"    URL: {r.get('gh_url', '')}")

    # 被排除的
    below = [r for r in scored if r["quality_score"] < args.min_score]
    if below:
        print(f"\n{'─'*80}")
        print(f"低于阈值({args.min_score})的 issue: {len(below)} 条")
        for r in below[:5]:
            print(f"  #{r['number']} [{r['quality_score']:.2f}] {r['title'][:60]}")

    if failed:
        print(f"\n{'─'*80}")
        print(f"评估失败的 issue: {len(failed)} 条")
        for r in failed[:3]:
            print(f"  #{r['number']}  错误: {r.get('理由', '')}")

    # 保存结果
    if args.output:
        out_data = {
            "summary": {
                "total": len(issues),
                "evaluated": len(scored),
                "failed": len(failed),
                "avg_score": round(sum(r["quality_score"] for r in scored) / len(scored), 3) if scored else None,
                "repo": args.repo,
                "labels": args.labels,
            },
            "results": scored,
            "failed": failed,
        }
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(out_data, f, ensure_ascii=False, indent=2)
        print(f"\n结果已保存到: {args.output}")


if __name__ == "__main__":
    main()
