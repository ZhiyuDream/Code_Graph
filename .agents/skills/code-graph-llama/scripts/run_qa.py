#!/usr/bin/env python3
"""
code-graph-llama skill wrapper
-------------------------------
一个轻量级封装脚本，调用 Code_Graph v7 的底层能力，
但应用 skill 级别的策略优化：
1. 条件触发 callers/callees（避免 80% 无效调用）
2. 文件结构问题定向处理（绕过 embedding 盲区）
3. 增强版停止条件（避免过早停止）
4. 证据审计式答案生成（减少脑补）

用法:
    python run_qa.py --input questions.json --output results.json
    python run_qa.py --question "ggml_alloc 的调用流程是什么"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# 定位到 Code_Graph 项目根目录
SKILL_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
sys.path.insert(0, str(SKILL_ROOT / "src"))
sys.path.insert(0, str(SKILL_ROOT))

from config import LLM_MODEL, OPENAI_API_KEY, OPENAI_BASE_URL
from openai import OpenAI

# 导入 Code_Graph 现有工具
from tools.agent_qa import (
    _load_rag_index,
    _load_issue_index,
    _cosine_sim,
    tool_get_callers,
    tool_get_callees,
    tool_get_file_functions,
    tool_get_function_detail,
    tool_search_issues_by_embedding,
    tool_search_functions,
    tool_read_file_lines,
)
from src.neo4j_writer import get_driver

# ---------------------------------------------------------------------------
# Skill 级别策略（来自 references/tool-selection-guide.md）
# ---------------------------------------------------------------------------

CALL_CHAIN_KEYWORDS = {
    '调用', 'caller', 'callee', '调用链', 'call chain',
    '流程', 'flow', '执行顺序', '执行过程',
    '依赖', 'depend', 'dependency',
    '影响分析', '上游', '下游',
    '谁调用', '被谁调用', '哪里调用'
}

FILE_STRUCTURE_KEYWORDS = {
    '包含哪些', '有哪些函数', '文件中', '代码结构',
    '定义了哪些', 'inside', 'in file', 'contains',
    '有哪些类', '有哪些结构体', '有哪些宏'
}

BUG_ISSUE_KEYWORDS = {
    '遇到了', '出现了', '报错', '错误', 'bug', 'crash',
    '性能问题', 'performance', 'illegal memory', 'segfault',
    'feature', '建议', 'enhancement'
}


def needs_call_chain_expansion(question: str) -> bool:
    q = question.lower()
    return any(kw in q for kw in CALL_CHAIN_KEYWORDS)


def is_file_structure_question(question: str) -> bool:
    return any(kw in question.lower() for kw in FILE_STRUCTURE_KEYWORDS)


def needs_issue_search(question: str) -> bool:
    return any(kw in question.lower() for kw in BUG_ISSUE_KEYWORDS)


def should_trigger_fallback(semantic_results: list) -> bool:
    if not semantic_results:
        return True
    max_score = max(f.get('score', 0) for f in semantic_results)
    top10 = semantic_results[:10]
    top10_avg = sum(f.get('score', 0) for f in top10) / len(top10)
    return max_score < 0.5 or (len(semantic_results) >= 5 and top10_avg < 0.4)


def should_stop(info_gain_history, current_functions, top_score, question_type):
    recent = info_gain_history[-2:]
    if len(recent) < 2:
        return False
    # 条件 A：连续低增益 + 已有足够信息
    if (all(g <= 1 for g in recent)
            and len(current_functions) >= 5
            and top_score >= 0.6):
        return True
    # 条件 B：连续 2 轮 0 增益
    if all(g == 0 for g in recent):
        return True
    # 条件 C：函数上限
    if len(current_functions) >= 12:
        return True
    return False


# ---------------------------------------------------------------------------
# 轻量级检索封装
# ---------------------------------------------------------------------------

_rag_index = None
_issue_index = None


def get_embedding_cached(client, query):
    cache_key = query[:200]
    # 简单进程内缓存
    if not hasattr(get_embedding_cached, '_cache'):
        get_embedding_cached._cache = {}
    if cache_key not in get_embedding_cached._cache:
        try:
            resp = client.embeddings.create(
                model=os.environ.get('EMBEDDING_MODEL', 'text-embedding-3-small'),
                input=[query[:500]]
            )
            get_embedding_cached._cache[cache_key] = resp.data[0].embedding
        except Exception:
            return None
    return get_embedding_cached._cache.get(cache_key)


def semantic_search(client, query, top_k=5):
    global _rag_index
    if _rag_index is None:
        _rag_index = _load_rag_index()
    if _rag_index is None:
        return []
    q_emb = get_embedding_cached(client, query)
    if q_emb is None:
        return []
    scores = []
    for i, chunk in enumerate(_rag_index['chunks']):
        if chunk['type'] == 'function':
            sim = _cosine_sim(q_emb, _rag_index['embeddings'][i])
            scores.append((sim, chunk))
    scores.sort(key=lambda x: -x[0])
    results = []
    for sim, chunk in scores[:top_k]:
        meta = chunk.get('meta', {})
        results.append({
            'name': meta.get('name', ''),
            'file': meta.get('file', ''),
            'score': sim,
            'text': chunk.get('text', '')[:400],
            'source': 'embedding'
        })
    return results


def issue_search(client, query, top_k=3):
    global _issue_index
    if _issue_index is None:
        _issue_index = _load_issue_index()
    if _issue_index is None:
        return []
    q_emb = get_embedding_cached(client, query)
    if q_emb is None:
        return []
    issues = _issue_index.get('issues', [])
    embeddings = _issue_index.get('embeddings', [])
    scores = []
    for i, issue in enumerate(issues):
        if i < len(embeddings):
            sim = _cosine_sim(q_emb, embeddings[i])
            scores.append((sim, issue))
    scores.sort(key=lambda x: -x[0])
    results = []
    for sim, issue in scores[:top_k]:
        results.append({
            'number': issue.get('number', ''),
            'title': issue.get('title', ''),
            'score': sim,
            'body': (issue.get('body') or '')[:300]
        })
    return results


# ---------------------------------------------------------------------------
# 主流程：应用 skill 策略的 ReAct QA
# ---------------------------------------------------------------------------

def run_single_question(driver, client, question: str) -> dict:
    """对单个问题执行 skill 优化后的检索与回答。"""
    collected = {
        'functions': [],
        'issues': [],
        'call_chains': [],
        'explored_files': set(),
        'steps': [],
        'tool_calls': []
    }
    info_gain_history = []

    # Step 1: 初始检索（带 skill 策略）
    print(f"[Skill] 处理问题: {question[:60]}...")

    # 1a. 语义搜索（始终执行）
    funcs = semantic_search(client, question, top_k=5)
    collected['functions'] = funcs

    # 1b. Issue 搜索（条件触发）
    if needs_issue_search(question):
        issues = issue_search(client, question, top_k=3)
        collected['issues'] = issues
        print(f"  [Issue] 找到 {len(issues)} 个相关 Issue")

    # 1c. 文件结构问题 → 直接用 get_file_functions
    if is_file_structure_question(question):
        # 尝试从问题中提取文件名（简化版：找 .c/.cpp/.h 后缀的 token）
        import re
        file_match = re.search(r'([\w\-/]+\.(?:c|cpp|h|hpp))', question.lower())
        if file_match:
            file_path = file_match.group(1)
            raw = tool_get_file_functions(driver, file_path, limit=15)
            file_funcs = []
            for line in raw.split('\n')[1:]:
                if line.strip() and 'fan_in=' in line:
                    parts = line.split()
                    if parts:
                        file_funcs.append({
                            'name': parts[0],
                            'file': file_path,
                            'score': 0.5,
                            'source': 'file_structure'
                        })
            collected['functions'].extend(file_funcs)
            print(f"  [FileStructure] {file_path} 中找到 {len(file_funcs)} 个函数")

    # 1d. Grep fallback
    if should_trigger_fallback(funcs):
        print("  [Fallback] 触发 Grep 补充搜索...")
        # 简化：用 search_functions 做补充
        import re
        entities = re.findall(r'\b[a-zA-Z_][a-zA-Z0-9_]*\b', question)
        # 过滤掉常见停用词
        stopwords = {'the', 'a', 'an', 'in', 'on', 'at', 'to', 'for', 'of', 'and', 'or', 'is', 'are', 'was', 'what', 'how', 'why', 'where', 'which', 'who', 'function', 'file', 'module', 'code', 'llama', 'cpp'}
        for ent in entities[:2]:
            if len(ent) >= 3 and ent.lower() not in stopwords:
                try:
                    raw = tool_search_functions(driver, ent, limit=3)
                    for line in raw.split('\n'):
                        if '(' in line:
                            name = line.split('(')[0].strip()
                            if name and not any(f['name'] == name for f in collected['functions']):
                                collected['functions'].append({
                                    'name': name,
                                    'score': 0.5,
                                    'source': 'grep_fallback'
                                })
                except Exception:
                    pass

    # 记录 Step 1
    collected['steps'].append({
        'step': 1,
        'action': 'initial_search',
        'found': len(collected['functions']),
        'issues': len(collected['issues'])
    })

    # Step 2-4: ReAct 扩展（带 skill 条件触发）
    enable_call_chain = needs_call_chain_expansion(question)
    if enable_call_chain:
        print(f"  [CallChain] 启用调用链扩展")

    for step in range(2, 5):
        func_count_before = len(collected['functions'])

        # Skill 简化版决策：不再调用 LLM 做路由，直接用规则
        action = 'sufficient'
        target = ''

        # 规则 1：函数太少 → 继续探索
        if len(collected['functions']) <= 3:
            action = 'explore_file'
            # 选最高相关度的函数所在文件
            if collected['functions']:
                target = collected['functions'][0].get('file', '')
        # 规则 2：需要调用链且未扩展 → 扩展
        elif enable_call_chain and not collected['call_chains']:
            action = 'expand_callers'
            target = collected['functions'][0].get('name', '')
        # 规则 3：满足停止条件 → 停止
        elif should_stop(info_gain_history, collected['functions'],
                         max([f.get('score', 0) for f in collected['functions']], default=0),
                         'call_chain' if enable_call_chain else 'general'):
            action = 'sufficient'
        else:
            # 默认：如果有未探索的高相关函数，扩展其 callers
            if enable_call_chain and collected['functions']:
                for f in collected['functions'][:3]:
                    if not any(c['from'] == f['name'] for c in collected['call_chains']):
                        action = 'expand_callers'
                        target = f['name']
                        break
            else:
                action = 'sufficient'

        if action == 'sufficient':
            print(f"  [Step {step}] 判定信息充足，停止")
            collected['steps'].append({'step': step, 'action': 'stop_sufficient'})
            break

        if action in ('expand_callers', 'expand_callees') and target:
            direction = 'callers' if action == 'expand_callers' else 'callees'
            try:
                raw = tool_get_callers(driver, target, limit=5) if direction == 'callers' else tool_get_callees(driver, target, limit=5)
                new_funcs = []
                for line in raw.split('\n')[1:]:
                    if '(' in line:
                        name = line.split('(')[0].strip()
                        file = line.split('(')[1].split(')')[0] if '(' in line else ""
                        if name and name != target and not any(f['name'] == name for f in collected['functions']):
                            new_funcs.append({'name': name, 'file': file, 'score': 0.5, 'source': f'{direction}_of_{target}'})
                collected['functions'].extend(new_funcs)
                collected['call_chains'].append({'from': target, 'direction': direction, 'found': len(new_funcs)})
                print(f"  [Step {step}] {direction} of {target}: +{len(new_funcs)} 个函数")
            except Exception as e:
                print(f"  [Step {step}] 扩展失败: {e}")

        elif action == 'explore_file' and target:
            try:
                raw = tool_get_file_functions(driver, target, limit=10)
                new_funcs = []
                for line in raw.split('\n')[1:]:
                    if 'fan_in=' in line:
                        parts = line.split()
                        if parts:
                            name = parts[0]
                            if not any(f['name'] == name for f in collected['functions']):
                                new_funcs.append({'name': name, 'file': target, 'score': 0.4, 'source': f'file_{target}'})
                collected['functions'].extend(new_funcs)
                collected['explored_files'].add(target)
                print(f"  [Step {step}] 探索文件 {target}: +{len(new_funcs)} 个函数")
            except Exception as e:
                print(f"  [Step {step}] 文件探索失败: {e}")

        info_gain = len(collected['functions']) - func_count_before
        info_gain_history.append(info_gain)
        collected['steps'].append({
            'step': step,
            'action': action,
            'target': target,
            'info_gain': info_gain
        })

    # Step 5: 生成答案（带 evidence audit 要求）
    answer = generate_answer(client, question, collected)
    return {
        'question': question,
        'answer': answer,
        'collected': {
            'function_count': len(collected['functions']),
            'issue_count': len(collected['issues']),
            'steps': len(collected['steps']),
            'call_chains': len(collected['call_chains'])
        },
        'skill_version': 'code-graph-llama-v1'
    }


def generate_answer(client, question: str, collected: dict) -> str:
    """应用 skill 级别的证据审计要求生成答案。"""
    funcs = collected['functions']
    issues = collected['issues']

    # 构建证据上下文
    func_lines = []
    for f in funcs[:8]:
        func_lines.append(f"- {f.get('name', '')} @ {f.get('file', '')} (来源: {f.get('source', '')}, 分数: {f.get('score', 0):.3f})")
        if f.get('text'):
            func_lines.append(f"  代码片段: {f['text'][:200]}")

    issue_lines = []
    for i in issues[:3]:
        issue_lines.append(f"- Issue #{i.get('number', '')}: {i.get('title', '')}")
        if i.get('body'):
            issue_lines.append(f"  描述: {i['body'][:250]}")

    evidence = f"""【检索到的函数】({len(funcs)}个)
{chr(10).join(func_lines) if func_lines else "无"}

【检索到的 Issue】({len(issues)}个)
{chr(10).join(issue_lines) if issue_lines else "无"}
"""

    prompt = f"""基于以下检索到的证据，回答用户的问题。

=== CRITICAL: 证据审计规则 ===
在最终答案前，你必须在 <evidence_audit> 标签中完成以下检查：
1. 列出你计划回答的每一个要点
2. 为每个要点标明支持它的具体证据（函数名、文件路径、Issue 编号、代码片段）
3. 如果某个要点没有直接证据支持，将其标记为 [UNVERIFIED] 并从最终答案中删除或明确标注为"无法确认"

=== 禁止行为 ===
- MUST NOT 基于函数名推测实现
- MUST NOT 将无关函数强行解释为答案
- MUST NOT 使用"推测"、"可能"来替代证据

用户问题: {question}

检索证据:
{evidence}

请在 <evidence_audit>...</evidence_audit> 之后，用中文输出清晰、结构化的最终答案。
"""

    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{'role': 'user', 'content': prompt}],
            max_tokens=1500,
            temperature=0.3
        )
        text = resp.choices[0].message.content or "(无答案)"
        # 可选：提取 <evidence_audit> 之后的最终答案
        # 这里为了简单直接返回全部
        return text.strip()
    except Exception as e:
        return f"生成答案失败: {e}"


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='code-graph-llama skill wrapper')
    parser.add_argument('--question', type=str, help='单个问题')
    parser.add_argument('--input', type=str, help='输入 JSON 文件（包含问题列表）')
    parser.add_argument('--output', type=str, default='skill_results.json', help='输出 JSON 文件')
    args = parser.parse_args()

    client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)
    driver = get_driver()

    if args.question:
        result = run_single_question(driver, client, args.question)
        print("\n=== 答案 ===")
        print(result['answer'])
        print("\n=== 统计 ===")
        print(json.dumps(result['collected'], ensure_ascii=False, indent=2))
    elif args.input:
        with open(args.input, encoding='utf-8') as f:
            data = json.load(f)
        questions = data if isinstance(data, list) else data.get('questions', [])
        results = []
        for item in questions:
            q = item if isinstance(item, str) else item.get('question', '')
            print(f"\n[{len(results)+1}/{len(questions)}] {q[:50]}...")
            result = run_single_question(driver, client, q)
            results.append(result)
        with open(args.output, 'w', encoding='utf-8') as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"\n完成！结果已保存到 {args.output}")
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
