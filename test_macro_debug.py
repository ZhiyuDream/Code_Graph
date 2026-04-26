#!/usr/bin/env python3
"""测试宏定义问题 - 调试版本"""

import sys
sys.path.insert(0, '/data/yulin/RUC/Code_Graph')

import os
from scripts.run_qa_v7_p0_improved import react_decide, call_llm_with_retry
from neo4j import GraphDatabase
from openai import OpenAI

# 宏定义相关问题
test_question = "系统中所有的GGML_HEXAGON_MAX_SESSIONS包含哪些，分别承担什么功能，它们之间的依赖关系是怎样的？"

def test():
    client = OpenAI(
        api_key=os.getenv("OPENAI_API_KEY"),
        base_url=os.getenv("OPENAI_BASE_URL")
    )
    
    driver = GraphDatabase.driver(
        os.getenv("NEO4J_URI", "bolt://localhost:7687"),
        auth=(os.getenv("NEO4J_USER", "neo4j"), 
              os.getenv("NEO4J_PASSWORD", "password"))
    )
    
    print(f"测试问题: {test_question}")
    print("="*80)
    
    # 模拟已收集的信息（Step 1之后）
    collected = {
        "functions": [
            {"name": "ggml_backend_hexagon_reg", "file": "ggml-hexagon.cpp", "text": "static ggml_backend_reg_t ggml_backend_hexagon_reg() {...}", "score": 0.45, "source": "embedding"},
            {"name": "ggml_backend_hexagon_init", "file": "ggml-hexagon.cpp", "text": "static ggml_backend_t ggml_backend_hexagon_init(...) {...}", "score": 0.42, "source": "embedding"},
        ],
        "issues": [],
        "steps": [
            {"step": 1, "tool": "semantic_search", "found": 5, "new": 5}
        ],
        "call_chains": [],
        "code_snippets": [],
        "variables": [],
        "attributes": [],
        "tool_calls": []
    }
    
    print("\n当前已收集信息:")
    print(f"  函数: {[f['name'] for f in collected['functions']]}")
    
    print("\n调用 react_decide (Step 2)...")
    print("-"*80)
    
    decision = react_decide(client, test_question, collected, step=2)
    
    print(f"\n决策结果:")
    print(f"  thought: {decision.get('thought', 'N/A')}")
    print(f"  action: {decision.get('action', 'N/A')}")
    print(f"  target: {decision.get('target', 'N/A')}")
    print(f"  sufficient: {decision.get('sufficient', False)}")
    
    driver.close()

if __name__ == "__main__":
    test()
