#!/usr/bin/env python3
"""Location-based evaluation for v2 benchmark - 评估答案是否准确指出文件位置"""
import json
import sys
import re
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from config import OPENAI_API_KEY, OPENAI_BASE_URL, LLM_MODEL
from openai import OpenAI

LOCATION_JUDGE_PROMPT = """你是一个严格的代码审查质量评审。请判断「生成答案」是否准确引用了关键证据中的文件路径和行号。

判断标准：
- CORRECT: 生成答案明确引用了至少一个关键证据中的文件路径（含行号或文件名），且引用与关键证据一致。答案不仅给出了结论，还提供了具体的代码位置作为支撑。
- INCORRECT: 生成答案没有引用任何文件路径，或引用的路径与关键证据不符，或只给出了概括性结论而没有具体位置证据。

关键证据中的文件路径：
{evidence_files}

【问题】
{question}

【参考答案】
{reference}

【生成答案】
{generated}

必须首行输出：结果: CORRECT 或 结果: INCORRECT
第二行起：简要说明理由（1-2句话）
"""

client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)


def location_judge(item: dict) -> tuple[bool, str, int]:
    """评估答案是否准确指出文件位置。"""
    # 提取证据中的文件路径
    evidence = item.get("evidence", "")
    evidence_files = set()
    for m in re.findall(r'`([^`]+?)`', evidence):
        if ':' in m and ('/' in m or '\\' in m):
            fp = m.rsplit(':', 1)[0]
            if '-' in fp:
                fp = fp.rsplit('-', 1)[0]
            evidence_files.add(fp.replace('\\', '/'))

    evidence_str = '\n'.join(f'- {f}' for f in sorted(evidence_files)) if evidence_files else '（无明确文件路径）'

    prompt = LOCATION_JUDGE_PROMPT.format(
        evidence_files=evidence_str,
        question=str(item.get("question", ""))[:500],
        reference=str(item.get("reference", ""))[:800],
        generated=str(item.get("generated", ""))[:1500]
    )

    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=200
        )
        text = resp.choices[0].message.content.strip()
        first_line = text.split('\n')[0].upper()
        is_correct = "CORRECT" in first_line and "INCORRECT" not in first_line
        return is_correct, text, item.get("index", 0)
    except Exception as e:
        print(f"    API错误: {e}")
        return False, f"评估错误: {e}", item.get("index", 0)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True, help="待评估的 benchmark 结果 JSON")
    parser.add_argument("-o", "--output", type=Path, required=True, help="输出评估结果 JSON")
    parser.add_argument("-w", "--workers", type=int, default=20, help="并行 worker 数")
    args = parser.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        data = json.load(f)

    print(f"加载 {len(data)} 题，开始 location-based 评估 ({args.workers} workers)...")

    correct = 0
    completed = 0

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(location_judge, item): item for item in data}

        for future in as_completed(futures):
            is_correct, reason, idx = future.result()
            item = futures[future]
            item["eval_location_correct"] = is_correct
            item["eval_location_reason"] = reason
            if is_correct:
                correct += 1
            completed += 1

            if completed % 10 == 0:
                print(f"  [{completed}/{len(data)}] 当前正确: {correct}/{completed} = {correct/completed*100:.1f}%")
                with open(args.output, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 评估完成！Location 正确: {correct}/{len(data)} = {correct/len(data)*100:.1f}%")


if __name__ == "__main__":
    main()
