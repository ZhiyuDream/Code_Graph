#!/usr/bin/env python3
"""单题调试：对比截断 vs 不截断的完整过程"""
import json, sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.core import get_neo4j_driver, generate_answer
from src.core.answer_generator import build_context
from src.core.llm_client import get_llm_client
from run_qa_prompt_ablation_flexible import react_search, process_single

# 加载问题
with open("datasets/llama_cpp_QA_cleaned.json", encoding="utf-8") as f:
    data = json.load(f)
questions = data.get("questions", [])

# 选第一题
idx = 0
item = questions[idx]
question = item["question"]
print(f"问题: {question}")
print("="*80)

driver = get_neo4j_driver()
client = get_llm_client()

# 使用 V2 prompt 检索
collected = react_search(driver, client, question, "react_decide_v2")

print(f"\n检索到 {len(collected['functions'])} 个函数, {len(collected.get('steps', []))} 步")
for fn in collected["functions"]:
    text_len = len(fn.get("text", ""))
    print(f"  - {fn['name']} @ {fn['file']} (text={text_len} chars, source={fn.get('source', '')})")

# 构建 context（当前版本：截断 2000 字符）
context_truncated = build_context(collected)
print(f"\n截断版 context 长度: {len(context_truncated)} 字符")
print(f"Context 前 500 字符:\n{context_truncated[:500]}...")

# 模拟不截断：临时修改 _format_func_code
from src.core import answer_generator
orig_format = answer_generator._format_func_code

def no_trunc_format(fn, max_len=None):
    text = fn.get('text', '')
    if not text:
        return ''
    sig = answer_generator._get_func_signature(fn)
    if sig and text.strip().startswith(sig.split('(')[0].strip()):
        return text
    prefix = f"{sig}\n" if sig else ""
    return prefix + text

answer_generator._format_func_code = no_trunc_format
context_full = build_context(collected)
answer_generator._format_func_code = orig_format

print(f"\n完整版 context 长度: {len(context_full)} 字符")
print(f"长度差异: {len(context_full) - len(context_truncated)} 字符")

# 生成答案（截断版）
print("\n" + "="*80)
print("截断版 (max_tokens=8192) 答案:")
answer_truncated = generate_answer(question, collected, max_tokens=8192)
print(answer_truncated[:1500])
print(f"\n[长度: {len(answer_truncated)} 字符]")

# 生成答案（完整版）
print("\n" + "="*80)
print("完整版 (max_tokens=8192) 答案:")
answer_generator._format_func_code = no_trunc_format
answer_full = generate_answer(question, collected, max_tokens=8192)
answer_generator._format_func_code = orig_format
print(answer_full[:1500])
print(f"\n[长度: {len(answer_full)} 字符]")

driver.close()
