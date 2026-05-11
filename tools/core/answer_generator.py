"""答案生成器 - 基于检索结果生成最终答案"""
from __future__ import annotations

from typing import Dict
from .llm_client import call_llm
from .prompt_loader import load_prompt


def _get_func_signature(fn: dict) -> str:
    """提取函数签名或详情。"""
    sig = fn.get('signature', '') or fn.get('detail', '')
    return sig.strip() if sig else ''


def _format_func_code(fn: dict, max_len: int = 2000) -> str:
    """格式化函数代码片段，保留签名+正文。"""
    text = fn.get('text', '')
    if not text:
        return ''
    sig = _get_func_signature(fn)
    # 如果签名在代码开头，避免重复
    if sig and text.strip().startswith(sig.split('(')[0].strip()):
        display = text[:max_len]
    else:
        prefix = f"{sig}\n" if sig else ""
        display = prefix + text[:max_len - len(prefix)]
    if len(text) > max_len:
        display += "...(截断)"
    return display


def build_context(collected: Dict) -> str:
    """构建上下文信息。
    
    改进点:
    1. 所有函数统一提供代码片段（不再让 chain_funcs 裸奔）
    2. 按综合相关性排序，而非按来源分类
    3. 控制总长度避免信息淹没
    4. 增加函数签名展示
    """
    funcs = collected.get("functions", [])
    issues = collected.get("issues", [])
    steps = collected.get("steps", [])
    
    lines = []
    
    # ---- 统一整理所有函数，计算综合优先级 ----
    scored_funcs = []
    for fn in funcs:
        source = fn.get('source', '') or 'embedding'
        score = fn.get('score', 0)
        # 综合优先级: embedding高分 > grep命中 > chain扩展 > file扩展 > 低分embedding
        priority = 0
        if source in ('', 'embedding'):
            priority = score * 100  # 0-100
        elif source == 'grep_fallback':
            priority = 45
        elif 'caller' in source or 'callee' in source:
            priority = 40
        elif source == 'file_expansion':
            priority = 30
        else:
            priority = 20
        scored_funcs.append((priority, fn))
    
    # 按优先级降序，去重（同名函数保留优先级高的）
    scored_funcs.sort(key=lambda x: -x[0])
    seen_names = set()
    unique_funcs = []
    for prio, fn in scored_funcs:
        name = fn.get('name', '')
        if name and name in seen_names:
            continue
        seen_names.add(name)
        unique_funcs.append((prio, fn))
    
    # ---- 函数上下文（统一格式）----
    if unique_funcs:
        lines.append("【相关函数代码】(按相关度排序，含签名与实现)")
        for i, (prio, fn) in enumerate(unique_funcs[:8], 1):  # 最多8个
            source = fn.get('source', '') or 'embedding'
            score_tag = f"[相似度:{fn.get('score', 0):.2f}]" if fn.get('score') else ""
            src_tag = f"[来源:{source}]" if source not in ('', 'embedding') else ""
            lines.append(f"\n--- 函数 {i}: {fn['name']} @ {fn['file']} {score_tag} {src_tag} ---")
            
            sig = _get_func_signature(fn)
            if sig:
                lines.append(f"签名: {sig}")
            
            code = _format_func_code(fn, max_len=2000)
            if code:
                lines.append(f"代码:\n{code}")
    
    # ---- Issue信息 ----
    if issues:
        lines.append(f"\n\n【相关Issue/PR】")
        for i, issue in enumerate(issues[:3], 1):
            lines.append(f"{i}. #{issue['number']}: {issue['title']}")
            if issue.get('body'):
                body = issue['body'][:4000]
                if len(issue['body']) > 4000:
                    body += "...(截断)"
                lines.append(f"   {body}")
    
    # ---- ReAct探索过程（精简）----
    if steps:
        lines.append(f"\n【检索过程摘要】")
        for step in steps[:3]:
            action = step.get('action', '')
            if action == 'initial_search':
                lines.append(f"  - 初始检索发现 {step.get('found', 0)} 个函数")
            elif action in ['expand_callers', 'expand_callees']:
                lines.append(f"  - 扩展{action.split('_')[1]}: {step.get('target', '')}")
            elif action == 'sufficient':
                lines.append("  - 判定信息充足，停止检索")
    
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
