#!/usr/bin/env python3
"""快速评估 full-files 100 题结果"""
import json
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

_ROOT = Path(__file__).resolve().parent
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

client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)

def binary_judge(item):
    prompt = BINARY_JUDGE_PROMPT.format(
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

if __name__ == "__main__":
    input_file = "results/v8_fullfiles_100.json"
    output_file = "results/v8_fullfiles_100.eval.json"
    
    with open(input_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    print(f"加载 {len(data)} 题，开始并行评估 (20 workers)...")
    
    # 先检查是否已有评估结果
    already_evaluated = sum(1 for item in data if "eval_binary_correct" in item)
    if already_evaluated > 0:
        print(f"已有 {already_evaluated} 题被评估，将重新评估全部")
    
    correct = 0
    completed = 0
    
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(binary_judge, item): item for item in data}
        
        for future in as_completed(futures):
            is_correct, reason, idx = future.result()
            item = futures[future]
            item["eval_binary_correct"] = is_correct
            item["eval_binary_reason"] = reason
            if is_correct:
                correct += 1
            completed += 1
            
            if completed % 10 == 0:
                print(f"  [{completed}/{len(data)}] 当前正确: {correct}/{completed} = {correct/completed*100:.1f}%")
                with open(output_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
    
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    
    print(f"\n✅ 评估完成！正确: {correct}/{len(data)} = {correct/len(data)*100:.1f}%")
