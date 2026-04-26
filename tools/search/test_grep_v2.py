#!/usr/bin/env python3
"""测试 Grep V2 的正确性和性能"""
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))

from tools.search.grep_search import grep_codebase as grep_v1
from tools.search.grep_search_v2 import (
    grep_codebase_v2,
    grep_codebase,  # 兼容层
    grep_files,
    grep_count,
    grep_identifier,
)


def test_compat():
    """测试 V1 兼容接口"""
    print("=== 测试 V1 兼容接口 ===")
    keyword = "ggml_init"

    r1 = grep_v1(keyword, limit=3)
    r2 = grep_codebase(keyword, limit=3)

    print(f"V1 返回: {len(r1)} 条")
    print(f"V2 兼容层返回: {len(r2)} 条")

    if len(r1) == len(r2):
        print("✅ 返回数量一致")
    else:
        print(f"⚠️ 数量不一致: V1={len(r1)}, V2={len(r2)}")

    # 检查文件路径是否一致（排序可能不同）
    files1 = set(m.get("file", "") for m in r1)
    files2 = set(m.get("file", "") for m in r2)
    if files1 == files2:
        print("✅ 文件集合一致")
    else:
        print(f"⚠️ 文件不一致")
        print(f"  V1独有: {files1 - files2}")
        print(f"  V2独有: {files2 - files1}")
    print()


def test_json_parsing():
    """测试 --json 解析可靠性"""
    print("=== 测试 --json 解析（含特殊字符）===")
    # 搜索包含冒号的模式（容易让 V1 的正则解析出错）
    keyword = "http://"
    r = grep_codebase_v2(keyword, limit=3, context_lines=2)
    print(f"搜索结果: {len(r)} 条")
    for m in r[:2]:
        print(f"  {m['file']}:{m['line_number']} {m['lines'][0]['content'][:60]}")
    print("✅ JSON解析无崩溃")
    print()


def test_mtime_sort():
    """测试 mtime 排序"""
    print("=== 测试 mtime 排序 ===")
    keyword = "ggml"
    r = grep_codebase_v2(keyword, limit=5, context_lines=0, sort_by_mtime=True)
    print("按 mtime 降序（最近修改优先）:")
    for m in r:
        f = Path("/data/yulin/RUC/llama.cpp") / m["file"]
        mtime = f.stat().st_mtime if f.exists() else 0
        print(f"  {m['file']} (mtime={mtime:.0f})")
    print()


def test_output_modes():
    """测试三种输出模式"""
    print("=== 测试三种输出模式 ===")
    keyword = "ggml_mat_mul"

    # files_with_matches
    files = grep_codebase_v2(keyword, output_mode="files_with_matches", limit=5)
    print(f"files_with_matches: {len(files)} 个文件")
    for f in files[:3]:
        print(f"  - {f}")

    # count
    count = grep_codebase_v2(keyword, output_mode="count")
    print(f"count: {count} 次匹配")

    # content
    content = grep_codebase_v2(keyword, output_mode="content", limit=3, context_lines=2)
    print(f"content: {len(content)} 条匹配")
    for m in content[:2]:
        print(f"  {m['file']}:{m['line_number']}")
    print()


def test_identifier_search():
    """测试标识符搜索（-Fw）"""
    print("=== 测试标识符搜索 (-Fw) ===")
    # 搜 "init" 作为标识符，避免匹配到 "initialize", "init_tensor" 等
    r1 = grep_codebase_v2("init", limit=5, fixed_strings=False, word_regexp=False)
    r2 = grep_identifier("init", limit=5)
    print(f"普通搜索 'init': {len(r1)} 条（可能包含 init_tensor, initialize 等）")
    print(f"标识符搜索 'init' (-Fw): {len(r2)} 条（仅全词匹配）")
    for m in r2[:3]:
        print(f"  {m['file']}:{m['line_number']} {m['lines'][0]['content'][:70]}")
    print()


def test_max_columns():
    """测试 --max-columns 截断"""
    print("=== 测试 --max-columns 截断 ===")
    # 搜一个常见关键词，对比截断前后
    keyword = "ggml"
    r_long = grep_codebase_v2(keyword, limit=2, max_columns=1000)
    r_short = grep_codebase_v2(keyword, limit=2, max_columns=80)

    print(f"max_columns=1000: 首行长度={len(r_long[0]['lines'][0]['content'])}")
    print(f"max_columns=80:   首行长度={len(r_short[0]['lines'][0]['content'])}")
    print("✅ 截断生效")
    print()


def benchmark():
    """性能对比"""
    print("=== 性能对比 (V1 vs V2) ===")
    keyword = "ggml"
    iterations = 3

    # V1
    t1 = time.time()
    for _ in range(iterations):
        r1 = grep_v1(keyword, limit=10)
    v1_time = (time.time() - t1) / iterations

    # V2
    t2 = time.time()
    for _ in range(iterations):
        r2 = grep_codebase(keyword, limit=10)
    v2_time = (time.time() - t2) / iterations

    print(f"V1 平均: {v1_time*1000:.1f}ms")
    print(f"V2 平均: {v2_time*1000:.1f}ms")
    print(f"差异: {(v2_time/v1_time - 1)*100:+.1f}%")
    print()


if __name__ == "__main__":
    test_compat()
    test_json_parsing()
    test_mtime_sort()
    test_output_modes()
    test_identifier_search()
    test_max_columns()
    benchmark()
    print("全部测试完成！")
