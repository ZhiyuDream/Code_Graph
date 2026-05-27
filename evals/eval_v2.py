#!/usr/bin/env python3
"""评估 v2 benchmark 结果：binary judge + 证据引用准确率"""
import json
import sys
import re
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from config import OPENAI_API_KEY, OPENAI_BASE_URL
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
            model=os.environ.get("JUDGE_MODEL", "gpt-4.1-mini"),
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


def extract_cited_files(answer_text: str) -> set:
    """从答案中提取引用的文件路径。"""
    # 匹配 `path/to/file:line` 或 path/to/file
    patterns = [
        r'`([^`]+?\.(?:cpp|c|h|hpp))`',
        r'`([^`]+?\.(?:cpp|c|h|hpp):\d+)`',
        r'([\w\-/]+\.(?:cpp|c|h|hpp))',
    ]
    files = set()
    for pattern in patterns:
        for match in re.finditer(pattern, answer_text):
            fp = match.group(1).rsplit(':', 1)[0]
            files.add(fp)
    return files


def compute_citation_accuracy(item: dict) -> dict:
    """计算证据引用准确率。"""
    evidence_text = item.get("evidence", "")
    answer_text = item.get("generated", "")
    
    # 提取关键证据中的文件路径
    evidence_pattern = r'`([^`]+:\d+)`'
    evidence_matches = re.findall(evidence_pattern, evidence_text)
    evidence_files = set()
    for m in evidence_matches:
        fp = m.rsplit(':', 1)[0]
        evidence_files.add(fp)
    
    # 提取答案中引用的文件路径
    cited_files = extract_cited_files(answer_text)
    
    if not evidence_files:
        return {"evidence_files": [], "cited_files": [], "hit_files": [], "citation_accuracy": 0}
    
    hit_files = evidence_files & cited_files
    accuracy = len(hit_files) / len(evidence_files)
    
    return {
        "evidence_files": sorted(evidence_files),
        "cited_files": sorted(cited_files),
        "hit_files": sorted(hit_files),
        "citation_accuracy": accuracy,
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default="results/v2_deepseek_fullfiles.json")
    parser.add_argument("-o", "--output", type=Path, default="results/v2_deepseek_fullfiles.eval.json")
    parser.add_argument("-w", "--workers", type=int, default=20)
    args = parser.parse_args()
    
    input_file = args.input
    output_file = args.output
    
    with open(input_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    print(f"加载 {len(data)} 题，开始并行评估 (20 workers)...")
    
    correct = 0
    completed = 0
    
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(binary_judge, item): item for item in data}
        
        for future in as_completed(futures):
            is_correct, reason, idx = future.result()
            item = futures[future]
            item["eval_binary_correct"] = is_correct
            item["eval_binary_reason"] = reason
            
            # 计算证据引用准确率
            citation = compute_citation_accuracy(item)
            item["citation_accuracy"] = citation
            
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
    
    # 汇总证据引用准确率
    total_citation = 0
    for item in data:
        cit = item.get("citation_accuracy", {})
        total_citation += cit.get("citation_accuracy", 0)
    
    print(f"平均证据引用准确率: {total_citation/len(data)*100:.1f}%")
    
    # 按维度拆分
    dim_stats = {}
    for item in data:
        d1 = item.get("dimension_1", "unknown")
        d2 = item.get("dimension_2", "unknown")
        key = f"{d1}/{d2}"
        if key not in dim_stats:
            dim_stats[key] = {"total": 0, "correct": 0}
        dim_stats[key]["total"] += 1
        if item.get("eval_binary_correct"):
            dim_stats[key]["correct"] += 1
    
    print("\n按维度拆分:")
    for key, stats in sorted(dim_stats.items()):
        print(f"  {key}: {stats['correct']}/{stats['total']} = {stats['correct']/stats['total']*100:.1f}%")
