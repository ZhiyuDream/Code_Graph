#!/usr/bin/env python3
"""快速测试扩展的ReAct工具选择"""

import sys
sys.path.insert(0, '/data/yulin/RUC/Code_Graph')

import os
os.environ['LLM_MODEL'] = 'kimi-k2.5'

from scripts.run_qa_v7_p0_improved import react_decide
import pandas as pd
from openai import OpenAI

# 测试问题（涉及不同工具类型）
test_questions = [
    # 宏定义相关
    "What is the value of MAX_MEMORY_SIZE macro?",
    # 结构体相关
    "What fields does the Config structure have?",
    # 调用链相关
    "What functions call the main function?",
]

def test_react_decide():
    client = OpenAI(
        api_key=os.getenv("MOONSHOT_API_KEY"),
        base_url="https://api.moonshot.cn/v1"
    )
    
    for q in test_questions:
        print(f"\n{'='*60}")
        print(f"问题: {q}")
        print('='*60)
        
        # 模拟已收集的信息
        collected = {
            "functions": [
                {"name": "main", "file": "src/main.c", "text": "int main() {...}"}
            ],
            "steps": [],
            "code_snippets": [],
            "variables": [],
            "attributes": []
        }
        
        # 测试第一步决策
        decision = react_decide(client, q, collected, step=2)
        print(f"\n决策结果:")
        print(f"  thought: {decision.get('thought', 'N/A')[:100]}...")
        print(f"  action: {decision.get('action', 'N/A')}")
        print(f"  target: {decision.get('target', 'N/A')}")
        print(f"  sufficient: {decision.get('sufficient', False)}")

if __name__ == "__main__":
    test_react_decide()
