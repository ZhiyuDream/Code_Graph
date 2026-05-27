"""答案生成器 - 基于检索结果生成最终答案"""
from __future__ import annotations

from typing import Dict
from .llm_client import call_llm
from .prompt_loader import load_prompt


def build_context(collected: Dict) -> str:
    """构建上下文信息"""
    funcs = collected.get("functions", [])
    issues = collected.get("issues", [])
    steps = collected.get("steps", [])
    
    lines = []
    
    # 按来源分类
    embedding_funcs = [f for f in funcs if f.get('source') in ('', 'embedding')]
    chain_funcs = [f for f in funcs if 'caller' in f.get('source', '') or 'callee' in f.get('source', '')]
    grep_funcs = [f for f in funcs if f.get('source') == 'grep_fallback']
    file_exp_funcs = [f for f in funcs if f.get('source') == 'file_expansion']
    
    # 高相关函数（embedding，相似度>0.5）
    high_rel = [f for f in embedding_funcs if f.get('score', 0) > 0.5]
    if high_rel:
        lines.append("【高相关函数】(Embedding检索，相似度>0.5)")
        for i, fn in enumerate(high_rel[:5], 1):
            lines.append(f"{i}. {fn['name']} @ {fn['file']}")
            if fn.get('text'):
                lines.append(f"   代码: {fn['text'][:150]}")
    
    # Grep fallback 函数
    if grep_funcs:
        lines.append(f"\n【Grep搜索补充函数】")
        for i, fn in enumerate(grep_funcs[:3], 1):
            lines.append(f"{i}. {fn['name']} @ {fn['file']}")
            if fn.get('text'):
                lines.append(f"   代码: {fn['text'][:150]}")
    
    # 文件级扩展的函数
    if file_exp_funcs:
        lines.append(f"\n【同文件相关函数】(文件级扩展发现)")
        for i, fn in enumerate(file_exp_funcs[:10], 1):  # 增加到前10个
            lines.append(f"{i}. {fn['name']} @ {fn['file']}")
            if fn.get('text'):
                # 增加代码片段长度，避免丢失关键信息
                code = fn['text'][:500]  # 从100增加到500字符
                if len(fn['text']) > 500:
                    code += "...(截断)"
                lines.append(f"   代码: {code}")
    
    # 调用链扩展的函数
    if chain_funcs:
        lines.append(f"\n【调用链相关函数】(通过ReAct扩展发现)")
        for i, fn in enumerate(chain_funcs[:5], 1):
            lines.append(f"{i}. {fn['name']} [{fn.get('source')}]")
    
    # Issue信息
    if issues:
        lines.append(f"\n【相关Issue/PR】")
        for i, issue in enumerate(issues[:3], 1):
            lines.append(f"{i}. #{issue['number']}: {issue['title']}")
            if issue.get('body'):
                lines.append(f"   {issue['body'][:250]}")
    
    # ReAct探索过程
    if steps:
        lines.append(f"\n【检索过程】({len(steps)}轮)")
        for step in steps[:3]:
            action = step.get('action', '')
            if action == 'initial_search':
                lines.append(f"  - 初始检索: 发现{step.get('found', 0)}个函数")
            elif action in ['expand_callers', 'expand_callees']:
                lines.append(f"  - 扩展{action.split('_')[1]}: {step.get('target', '')}")
    
    return '\n'.join(lines)


def generate_answer(
    question: str,
    collected: Dict,
    max_tokens: int = 1000,
    model: str = None,
    provider: str = None
) -> str:
    """基于收集的信息生成答案"""
    context = build_context(collected)
    
    prompt = load_prompt("answer_generation", context=context, question=question)
    
    return call_llm(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        model=model,
        provider=provider
    )
