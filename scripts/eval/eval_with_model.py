#!/usr/bin/env python3
"""
用指定模型作为 Judge 评估结果文件
用法:
    python eval_with_model.py -i results/v8_gpt5_fileexp_360.json -o results/v8_gpt5_eval_by_41mini.json -m gpt-4.1-mini -w 20
"""
import argparse
import json
import sys
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))

# 从文件加载 judge prompt
from src.core.prompt_loader import load_prompt
from src.core.llm_client import call_llm
from src.core.model_config import ModelRegistry

def build_judge_prompt(question: str, reference: str, generated: str) -> str:
    return load_prompt(
        "judge_binary",
        question=question,
        reference=reference,
        generated=generated
    )


def judge_single(item: dict, model_name: str):
    """评估单个题目"""
    question = str(item.get("question", "") or item.get("具体问题", "") or "")
    reference = str(item.get("reference", "") or item.get("reference_answer", "") or item.get("参考答案", "") or "")
    generated = str(item.get("generated", "") or item.get("answer", "") or item.get("生成答案", "") or "")
    
    if not generated:
        return item, False, "无生成答案"
    
    prompt = build_judge_prompt(
        question=question[:500],
        reference=reference[:800],
        generated=generated[:1500]
    )
    
    try:
        text = call_llm(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000,
            timeout=120,
            model=model_name,
        )
        
        is_correct = "CORRECT" in text.split('\n')[0].upper() and "INCORRECT" not in text.split('\n')[0].upper()
        
        # 提取理由
        lines = text.split('\n')
        reason = '\n'.join(lines[1:]).strip() if len(lines) > 1 else text
        
        return item, is_correct, reason
    except Exception as e:
        return item, False, f"评判失败: {e}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-i", "--input", required=True, help="输入结果 JSON")
    parser.add_argument("-o", "--output", required=True, help="输出评估 JSON")
    parser.add_argument("-m", "--model", required=True, help="评判模型名称")
    parser.add_argument("-w", "--workers", type=int, default=20, help="并行数")
    args = parser.parse_args()
    
    data = json.load(open(args.input, "r", encoding="utf-8"))
    print(f"加载 {len(data)} 题，使用模型 {args.model} 评判")
    print(f"模型配置: {ModelRegistry.resolve(args.model).provider}")
    
    completed = 0
    correct_count = 0
    save_lock = threading.Lock()
    
    def save_progress():
        with save_lock:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
    
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(judge_single, item, args.model): i for i, item in enumerate(data)}
        
        for future in as_completed(futures):
            item, is_correct, reason = future.result()
            item["eval_binary_correct"] = is_correct
            item["eval_binary_reason"] = reason
            
            if is_correct:
                correct_count += 1
            completed += 1
            
            if completed % 10 == 0:
                print(f"  已评估 {completed}/{len(data)}，当前正确率 {correct_count/completed*100:.1f}%")
                save_progress()
    
    save_progress()
    
    print(f"\n评估完成！")
    print(f"正确: {correct_count}/{len(data)} = {correct_count/len(data)*100:.1f}%")
    print(f"结果保存至: {args.output}")


if __name__ == "__main__":
    main()
