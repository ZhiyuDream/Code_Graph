#!/usr/bin/env python3
"""测试宏定义相关问题是否能触发search_variables工具"""

import sys
sys.path.insert(0, '/data/yulin/RUC/Code_Graph')

import os
# 使用 .env 中配置的模型
os.environ['LLM_MODEL'] = os.getenv('LLM_MODEL', 'gpt-4.1-mini')

from scripts.run_qa_v7_p0_improved import react_search
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
    print("="*60)
    
    # 运行ReAct搜索
    result = react_search(driver, client, test_question)
    
    print("\n" + "="*60)
    print("工具调用记录:")
    for call in result.get('tool_calls', []):
        print(f"  Step {call.get('step')}: {call.get('tool')} -> {call.get('results')} 结果")
    
    # 检查是否使用了新工具
    new_tools = ['read_file_lines', 'search_variables', 'search_attributes', 'find_module', 'get_file_functions']
    used_new_tools = [call for call in result.get('tool_calls', []) if call.get('tool') in new_tools]
    
    print(f"\n新工具使用情况: {len(used_new_tools)} 次")
    for call in used_new_tools:
        print(f"  - {call.get('tool')}: params={call.get('params')}")
    
    driver.close()

if __name__ == "__main__":
    test()
