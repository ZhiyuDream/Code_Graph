#!/usr/bin/env python3
"""测试扩展工具集是否能正常工作"""

import sys
sys.path.insert(0, '/data/yulin/RUC/Code_Graph')

from tools.agent_qa import (
    tool_read_file_lines,
    tool_search_variables,
    tool_search_attributes,
    tool_find_module_by_keyword,
    tool_get_file_functions
)
from neo4j import GraphDatabase
import os

def test_tools():
    # 连接Neo4j
    driver = GraphDatabase.driver(
        os.getenv("NEO4J_URI", "bolt://localhost:7687"),
        auth=(os.getenv("NEO4J_USER", "neo4j"), 
              os.getenv("NEO4J_PASSWORD", "password"))
    )
    
    print("="*60)
    print("测试扩展工具集")
    print("="*60)
    
    # Test 1: read_file_lines
    print("\n1. 测试 tool_read_file_lines")
    print("-"*40)
    try:
        result = tool_read_file_lines(driver, "synchronize.c", 1, 30)
        print(f"   返回类型: {type(result)}")
        print(f"   返回值: {str(result)[:200]}...")
        if isinstance(result, dict):
            print(f"   文件: {result.get('file')}")
        else:
            print(f"   ✓ 返回字符串（工具内部已格式化）")
    except Exception as e:
        print(f"   ✗ 失败: {e}")
        import traceback
        traceback.print_exc()
    
    # Test 2: search_variables
    print("\n2. 测试 tool_search_variables")
    print("-"*40)
    try:
        results = tool_search_variables(driver, "MAX", limit=3)
        print(f"   返回类型: {type(results)}")
        print(f"   返回值: {results}")
        if isinstance(results, list):
            print(f"   找到 {len(results)} 个变量/宏")
        else:
            print(f"   ✓ 返回字符串（工具内部已格式化）")
    except Exception as e:
        print(f"   ✗ 失败: {e}")
        import traceback
        traceback.print_exc()
    
    # Test 3: search_attributes
    print("\n3. 测试 tool_search_attributes")
    print("-"*40)
    try:
        results = tool_search_attributes(driver, "flags", limit=3)
        print(f"   返回类型: {type(results)}")
        print(f"   返回值: {results}")
        if isinstance(results, list):
            print(f"   找到 {len(results)} 个属性")
        else:
            print(f"   ✓ 返回字符串（工具内部已格式化）")
    except Exception as e:
        print(f"   ✗ 失败: {e}")
        import traceback
        traceback.print_exc()
    
    # Test 4: find_module_by_keyword
    print("\n4. 测试 tool_find_module_by_keyword")
    print("-"*40)
    try:
        result = tool_find_module_by_keyword(driver, "synchronize")
        print(f"   返回类型: {type(result)}")
        print(f"   返回值: {result}")
        if isinstance(result, dict):
            print(f"   关键词: {result.get('keyword')}")
        else:
            print(f"   ✓ 返回字符串（工具内部已格式化）")
    except Exception as e:
        print(f"   ✗ 失败: {e}")
        import traceback
        traceback.print_exc()
    
    # Test 5: get_file_functions
    print("\n5. 测试 tool_get_file_functions")
    print("-"*40)
    try:
        results = tool_get_file_functions(driver, "synchronize.c", limit=5)
        print(f"   返回类型: {type(results)}")
        print(f"   返回值: {results}")
        if isinstance(results, list):
            print(f"   找到 {len(results)} 个函数")
        else:
            print(f"   ✓ 返回字符串（工具内部已格式化）")
    except Exception as e:
        print(f"   ✗ 失败: {e}")
        import traceback
        traceback.print_exc()
    
    driver.close()
    print("\n" + "="*60)
    print("工具测试完成")
    print("="*60)

if __name__ == "__main__":
    test_tools()
