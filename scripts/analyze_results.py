#!/usr/bin/env python3
"""分析 QA 结果文件，输出统计报告"""

import json
import sys
from pathlib import Path
from collections import Counter

def analyze_results(result_path: str):
    with open(result_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    if isinstance(data, list):
        results = data
    elif isinstance(data, dict):
        results = data.get('results', data.get('questions', []))
    else:
        print(f"未知格式: {type(data)}")
        return
    
    total = len(results)
    if total == 0:
        print("无结果")
        return
    
    print(f"=" * 60)
    print(f"结果分析: {result_path}")
    print(f"=" * 60)
    print(f"总题数: {total}")
    
    # 正确率（支持多种字段名）
    def _is_correct(r):
        for key in ['eval_binary_correct', 'is_correct', 'correct']:
            if key in r:
                return bool(r[key])
        return False
    correct = sum(1 for r in results if _is_correct(r))
    print(f"正确率: {correct}/{total} = {correct/total*100:.1f}%")
    
    # 时延统计
    latencies = [r.get('latency_s', 0) for r in results if 'latency_s' in r]
    if latencies:
        print(f"\n时延统计:")
        print(f"  平均: {sum(latencies)/len(latencies):.1f}s")
        print(f"  最小: {min(latencies):.1f}s")
        print(f"  最大: {max(latencies):.1f}s")
        print(f"  总耗时: {sum(latencies)/60:.1f}分钟")
    
    # Token 统计
    usages = [r.get('usage', {}) for r in results if 'usage' in r]
    if usages:
        total_prompt = sum(u.get('prompt_tokens', 0) for u in usages)
        total_completion = sum(u.get('completion_tokens', 0) for u in usages)
        total_tokens = sum(u.get('total_tokens', 0) for u in usages)
        total_reasoning = sum(u.get('reasoning_tokens', 0) for u in usages)
        total_calls = sum(u.get('call_count', 0) for u in usages)
        
        print(f"\nToken 统计:")
        print(f"  总调用次数: {total_calls}")
        print(f"  总 prompt_tokens: {total_prompt:,}")
        print(f"  总 completion_tokens: {total_completion:,}")
        print(f"  总 reasoning_tokens: {total_reasoning:,}")
        print(f"  总 tokens: {total_tokens:,}")
        print(f"  平均每题 tokens: {total_tokens/total:,.0f}")
    
    # ReAct 轮次
    step_counts = []
    for r in results:
        retrieval = r.get('retrieval', {})
        if 'step_count' in retrieval:
            step_counts.append(retrieval['step_count'])
        elif 'trace' in r:
            steps = r['trace'].get('react_steps', [])
            step_counts.append(len(steps))
    
    if step_counts:
        print(f"\nReAct 轮次:")
        print(f"  平均: {sum(step_counts)/len(step_counts):.1f}")
        print(f"  分布: {dict(Counter(step_counts))}")
    
    # 工具调用统计
    all_tool_stats = {}
    for r in results:
        ts = r.get('tool_stats', {})
        for action, stats in ts.items():
            if action not in all_tool_stats:
                all_tool_stats[action] = {"count": 0, "total_found": 0, "total_new": 0}
            all_tool_stats[action]["count"] += stats.get("count", 0)
            all_tool_stats[action]["total_found"] += stats.get("total_found", 0)
            all_tool_stats[action]["total_new"] += stats.get("total_new", 0)
    
    if all_tool_stats:
        print(f"\n工具调用统计:")
        for action, stats in sorted(all_tool_stats.items()):
            print(f"  {action}: {stats['count']} 次, found={stats['total_found']}, new={stats['total_new']}")
    
    # 失败统计
    errors = [r for r in results if 'error' in r or r.get('generated', '').startswith('处理失败')]
    if errors:
        print(f"\n失败统计: {len(errors)}/{total}")
        for e in errors[:3]:
            print(f"  [{e.get('index', '?')}] {e.get('error', 'unknown')[:100]}")
    
    print(f"=" * 60)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <result.json>")
        sys.exit(1)
    
    analyze_results(sys.argv[1])
