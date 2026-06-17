#!/usr/bin/env python3
"""补评估剩余题目（单线程稳定版）"""
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from config import OPENAI_API_KEY, OPENAI_BASE_URL, LLM_MODEL
from openai import OpenAI

BINARY_JUDGE_PROMPT = """请判断「生成答案」是否正确回答了问题。

判断标准：
- 正确 (CORRECT): 生成答案准确回答了问题，核心信息正确，无重大错误
- 错误 (INCORRECT): 生成答案与问题无关、信息错误、或未回答问题

必须首行输出：结果: CORRECT 或 结果: INCORRECT
第二行起：简要说明理由（1-2句话）

【问题】
{question}

【参考答案】
{reference}

【生成答案】
{generated}
"""

def binary_judge(client, question, reference, generated):
    import re
    prompt = BINARY_JUDGE_PROMPT.format(
        question=str(question)[:500],
        reference=str(reference)[:800],
        generated=str(generated)[:1500]
    )
    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=200
        )
        text = resp.choices[0].message.content.strip()
        is_correct = "CORRECT" in text.split('\n')[0].upper() and "INCORRECT" not in text.split('\n')[0].upper()
        return is_correct, text
    except Exception as e:
        return False, f"评估错误: {e}"

if __name__ == "__main__":
    client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)
    
    data = json.load(open("results/v8_no_cc_fileexp_360_eval_v2.json", "r", encoding="utf-8"))
    
    no_eval = [x for x in data if 'eval_binary_correct' not in x]
    print(f"需要补评估: {len(no_eval)}题")
    
    for i, item in enumerate(no_eval):
        is_correct, reason = binary_judge(
            client,
            item.get("具体问题", ""),
            item.get("参考答案", ""),
            item.get("生成答案", "")
        )
        item["eval_binary_correct"] = is_correct
        item["eval_binary_reason"] = reason
        
        if (i + 1) % 10 == 0:
            print(f"  已完成 {i+1}/{len(no_eval)}")
            # 保存进度
            with open("results/v8_no_cc_fileexp_360_eval_v2.json", "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
    
    # 最终保存
    with open("results/v8_no_cc_fileexp_360_eval_v2.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    
    # 统计
    correct = sum(1 for x in data if x.get('eval_binary_correct', False))
    print(f"\n评估完成！正确: {correct}/360 = {correct/360*100:.1f}%")
