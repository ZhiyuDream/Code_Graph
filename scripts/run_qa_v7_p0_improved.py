#!/usr/bin/env python3
"""
QA V7: ReAct Agent - 迭代深入检索 (P0改进版)

核心改进：
1. LLM主动搜索(Grep Fallback): 当Embedding效果不佳时，主动Grep搜索
2. 智能停止(递减回报检测): 连续低增益时提前停止
3. API重试机制: 指数退避，最多3次

废弃 A/B/C 硬分类，采用统一的 ReAct 决策模式。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Dict, Any

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

from config import (
    NEO4J_DATABASE, OPENAI_API_KEY, OPENAI_BASE_URL, 
    LLM_MODEL, EMBEDDING_MODEL
)
from src.neo4j_writer import get_driver
from tools.agent_qa import (
    _load_rag_index,
    _cosine_sim,
    tool_get_function_detail,
    tool_get_callers,
    tool_get_callees,
    _load_issue_index,
)
from openai import OpenAI

MAX_STEPS = 5
FALLBACK_THRESHOLD = 0.5  # Embedding最高相似度低于此值时触发主动搜索

# 缓存 embedding 查询结果
_query_cache = {}

# 代码库根目录
REPO_ROOT = Path("/data/yulin/RUC/llama.cpp")


def grep_search(entity: str, repo_root: Path = REPO_ROOT, max_results: int = 5) -> List[Dict]:
    """
    使用grep搜索代码库中的实体（函数名、类等）
    返回匹配的文件列表和代码片段
    """
    results = []
    try:
        # 使用rg (ripgrep) 搜索，比grep更快
        cmd = [
            "rg", "-n", "-C", "3", "--type-add", "cpp:*.{c,cpp,h,hpp}", 
            "-tcpp", "-i", entity, str(repo_root)
        ]
        output = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        
        if output.returncode == 0 or output.stdout:
            lines = output.stdout.strip().split("\n")
            current_file = None
            current_match = []
            
            for line in lines:
                if not line.strip():
                    continue
                    
                # 解析rg输出格式: file:line:content
                if ":" in line and not line.startswith("  ") and not line.startswith("--"):
                    parts = line.split(":", 2)
                    if len(parts) >= 2:
                        file_path = parts[0]
                        try:
                            line_num = int(parts[1])
                            content = parts[2] if len(parts) > 2 else ""
                            
                            # 保存之前的匹配
                            if current_file and current_match and len(results) < max_results:
                                results.append({
                                    "file": current_file,
                                    "lines": current_match,
                                    "score": 0.6  # 给grep结果一个基础分
                                })
                            
                            current_file = file_path
                            current_match = [{"line": line_num, "content": content}]
                        except ValueError:
                            pass
                elif current_file and line.startswith(" "):
                    # 上下文行
                    current_match.append({"line": None, "content": line.strip()})
            
            # 保存最后一个匹配
            if current_file and current_match and len(results) < max_results:
                results.append({
                    "file": current_file,
                    "lines": current_match,
                    "score": 0.6
                })
                
    except Exception as e:
        print(f"      Grep搜索失败: {e}")
    
    return results


def extract_entities_from_question(client, question: str) -> List[str]:
    """
    使用LLM从问题中提取关键实体（函数名、类名等）
    """
    prompt = f"""从以下问题中提取关键代码实体（函数名、类名、变量名等）。

问题: {question}

要求:
1. 只提取具体的标识符名称（如函数名、类名）
2. 如果问题问的是"函数xxx"，提取xxx
3. 如果问的是"模块yyy"，提取yyy
4. 最多返回3个最相关的实体

返回JSON格式:
{{"entities": ["entity1", "entity2", "entity3"]}}

只输出JSON:"""
    
    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=100,
            timeout=10
        )
        text = resp.choices[0].message.content.strip()
        
        # 提取JSON
        if '```json' in text:
            text = text.split('```json')[1].split('```')[0]
        elif '```' in text:
            text = text.split('```')[1].split('```')[0]
        
        result = json.loads(text)
        entities = result.get("entities", [])
        
        # 过滤掉太短或太泛的实体
        filtered = [e for e in entities if len(e) >= 3 and not e.lower() in [
            "function", "class", "module", "variable", "code"
        ]]
        return filtered[:3]
        
    except Exception as e:
        print(f"      实体提取失败: {e}")
        # 备用：用正则提取可能的函数名（驼峰或下划线命名）
        pattern = r'\b([a-zA-Z_][a-zA-Z0-9_]*::[a-zA-Z_][a-zA-Z0-9_]*|[a-zA-Z_][a-zA-Z0-9_]*_[a-zA-Z0-9_]+)\b'
        matches = re.findall(pattern, question)
        return list(set(matches))[:3]


def convert_grep_to_function_results(grep_results: List[Dict]) -> List[Dict]:
    """将grep结果转换为函数检索结果格式"""
    functions = []
    
    for result in grep_results:
        file_path = result.get("file", "")
        lines = result.get("lines", [])
        
        if not lines:
            continue
        
        # 尝试从第一行提取函数名（简化处理）
        first_content = lines[0].get("content", "")
        func_name = "unknown"
        
        # 匹配函数定义模式
        func_match = re.search(r'(?:\w+::)?(\w+)\s*\(', first_content)
        if func_match:
            func_name = func_match.group(1)
        
        # 构建代码文本
        code_text = "\n".join([l.get("content", "") for l in lines[:10]])
        
        functions.append({
            "name": func_name,
            "file": file_path,
            "score": result.get("score", 0.5),
            "text": code_text[:400],
            "source": "grep_fallback"
        })
    
    return functions


def get_query_embedding_cached(client, query: str):
    """带缓存的 embedding 查询"""
    cache_key = query[:200]
    if cache_key not in _query_cache:
        try:
            resp = client.embeddings.create(
                model=EMBEDDING_MODEL,
                input=[query[:500]]
            )
            _query_cache[cache_key] = resp.data[0].embedding
        except Exception:
            return None
    return _query_cache.get(cache_key)


def _should_use_call_chain(question: str) -> bool:
    """快速规则：是否需要扩展调用链"""
    q = question.lower()
    return any(kw in q for kw in ['调用', 'caller', 'callee', '链', 'chain', 
                                   '流程', 'flow', '执行', '过程', 'process', '哪里调用'])
_rag_index = None
_issue_index = None


def get_function_code_from_index(func_name: str) -> str:
    """从RAG索引中获取函数代码文本"""
    global _rag_index
    if _rag_index is None:
        return ""
    
    # 查找匹配的函数
    for chunk in _rag_index.get("chunks", []):
        if chunk.get("type") == "function":
            meta = chunk.get("meta", {})
            if meta.get("name") == func_name:
                return chunk.get("text", "")[:300]
    return ""


def search_code_embedding(client, query: str, top_k: int = 5) -> List[Dict]:
    """Embedding 语义搜索代码"""
    global _rag_index
    if _rag_index is None:
        _rag_index = _load_rag_index()
    if _rag_index is None:
        return []
    
    query_emb = get_query_embedding_cached(client, query)
    if query_emb is None:
        return []
    
    scores = []
    for i, chunk in enumerate(_rag_index["chunks"]):
        if chunk["type"] == "function":
            sim = _cosine_sim(query_emb, _rag_index["embeddings"][i])
            scores.append((sim, chunk))
    
    scores.sort(key=lambda x: -x[0])
    
    results = []
    for sim, chunk in scores[:top_k]:
        meta = chunk.get("meta", {})
        results.append({
            "name": meta.get("name", ""),
            "file": meta.get("file", ""),
            "score": sim,
            "text": chunk.get("text", "")[:400],
            "source": "embedding"
        })
    
    return results


def search_issues(client, query: str, top_k: int = 3) -> List[Dict]:
    """Issue 语义搜索"""
    global _issue_index
    if _issue_index is None:
        _issue_index = _load_issue_index()
    if _issue_index is None:
        return []
    
    query_emb = get_query_embedding_cached(client, query)
    if query_emb is None:
        return []
    
    issues = _issue_index.get("issues", [])
    embeddings = _issue_index.get("embeddings", [])
    
    scores = []
    for i, issue in enumerate(issues):
        if i < len(embeddings):
            sim = _cosine_sim(query_emb, embeddings[i])
            scores.append((sim, issue))
    
    scores.sort(key=lambda x: -x[0])
    
    results = []
    for sim, issue in scores[:top_k]:
        results.append({
            "number": issue.get("number", ""),
            "title": issue.get("title", ""),
            "score": sim,
            "body": (issue.get("body", "") or "")[:300]
        })
    
    return results


def react_decide(client, question: str, collected: Dict, step: int) -> Dict:
    """ReAct 决策：智能判断是否需要深入探索"""
    funcs = collected.get('functions', [])
    issues = collected.get('issues', [])
    chains = collected.get('call_chains', [])
    
    # 获取已扩展的函数名集合
    expanded_funcs = set(c['from'] for c in chains)
    
    # 构建详细上下文
    context_parts = [f"问题: {question}"]
    context_parts.append(f"\n【已收集函数】({len(funcs)}个，按相似度排序):")
    for i, fn in enumerate(funcs[:5]):
        source = fn.get('source', 'embedding')
        score = fn.get('score', 0)
        # 标记已扩展的函数
        expanded_mark = " [已扩展]" if fn['name'] in expanded_funcs else ""
        context_parts.append(f"{i+1}. {fn['name']} ({source}, {score:.3f}){expanded_mark}")
    
    if issues:
        context_parts.append(f"\n【相关Issue】({len(issues)}个):")
        for i, issue in enumerate(issues[:2]):
            context_parts.append(f"{i+1}. #{issue['number']}: {issue['title'][:50]}")
    
    if chains:
        context_parts.append(f"\n【已扩展调用链】({len(chains)}条)")
        for c in chains[-3:]:  # 显示最近3条
            context_parts.append(f"  - {c['from']}: {c['direction']} (找到{c['found']}个, 新增{c['new']}个)")
    
    context = "\n".join(context_parts)
    
    prompt = f"""{context}

---

你是代码检索专家。请根据当前已收集的信息和问题类型，选择最合适的下一步行动。

【可用工具说明】
1. expand_callers - 扩展某个函数的调用者（谁调用了它）
   适用：调用链、依赖关系、流程分析问题
   
2. expand_callees - 扩展某个函数的被调用者（它调用了谁）
   适用：执行流程、内部实现细节问题
   
3. sufficient - 信息充足，可以生成答案
   适用：已有足够证据回答问题，无需继续检索

【决策原则】
- 调用链/依赖问题 → 用 expand_callers/callees
- 信息已足够 → 用 sufficient

【重要】不要重复扩展已标记[已扩展]的函数。

返回JSON格式:
{{
    "thought": "分析问题类型和当前信息缺口，说明选择该工具的原因",
    "sufficient": false,
    "action": "expand_callers|expand_callees|sufficient",
    "target": "目标函数名（根据action类型填写）",
    "params": {{}}
}}

只输出JSON:"""
    
    # 使用重试机制调用 LLM
    response_text = call_llm_with_retry(
        client,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=150,
        timeout=15,
        max_retries=3
    )
    
    # 检查是否调用失败
    if response_text.startswith("生成答案失败:"):
        print(f"      LLM 调用失败: {response_text}")
        # 出错时：如果有函数且未超2轮，继续扩展
        if funcs and step < 3:
            for f in funcs[:5]:
                if not any(c['from'] == f['name'] for c in chains):
                    return {"sufficient": False, "action": "expand_callees", "target": f['name']}
        return {"sufficient": True, "action": "sufficient", "target": ""}
    
    try:
        # 提取 JSON
        text = response_text.strip()
        if '```json' in text:
            text = text.split('```json')[1].split('```')[0]
        elif '```' in text:
            text = text.split('```')[1].split('```')[0]
        
        result = json.loads(text)
        
        # 强制停止条件
        if step >= 4:  # 最多4轮决策
            return {"sufficient": True, "action": "sufficient", "target": ""}
        
        if result.get("sufficient") or result.get("action") == "sufficient":
            return {"sufficient": True, "action": "sufficient", "target": ""}
        
        action = result.get("action", "")
        target = result.get("target", "")
        
        # 只保留核心工具
        valid_actions = ["expand_callers", "expand_callees", "sufficient"]
        
        # 如果 action 不在有效列表中，默认使用 expand_callees
        final_action = action if action in valid_actions else "expand_callees"
        
        # expand_callers/expand_callees 需要验证 target 是否是已知的函数
        valid = [f['name'] for f in funcs[:8]]
        if target in valid:
            final_target = target
        else:
            # 智能选择：优先选高相似度且未扩展过的
            final_target = ""
            for f in funcs[:5]:
                if not any(c['from'] == f['name'] for c in chains):
                    final_target = f['name']
                    break
            if not final_target and funcs:
                final_target = funcs[0]['name']
        
        return {
            "thought": result.get("thought", ""),
            "sufficient": False,
            "action": final_action,
            "target": final_target
        }
        
    except Exception as e:
        print(f"      JSON 解析失败: {e}")
        # 出错时：如果有函数且未超2轮，继续扩展
        if funcs and step < 3:
            for f in funcs[:5]:
                if not any(c['from'] == f['name'] for c in chains):
                    return {"sufficient": False, "action": "expand_callees", "target": f['name']}
        return {"sufficient": True, "action": "sufficient", "target": ""}


def expand_call_chain(driver, func_name: str, direction: str) -> Dict:
    """扩展调用链，并尝试获取函数代码"""
    if direction == "callers":
        result = tool_get_callers(driver, func_name, limit=5)
    else:
        result = tool_get_callees(driver, func_name, limit=5)
    
    # 解析结果
    functions = []
    for line in result.split('\n')[1:]:  # 跳过标题
        if '(' in line:
            name = line.split('(')[0].strip()
            file = line.split('(')[1].split(')')[0] if '(' in line else ""
            if name and name != func_name:
                # 尝试从索引获取代码
                code_text = get_function_code_from_index(name)
                functions.append({
                    "name": name, 
                    "file": file,
                    "text": code_text
                })
    
    return {"functions": functions, "raw": result}


def explore_file(driver, file_path: str) -> List[Dict]:
    """探索文件中的其他函数"""
    result = tool_get_file_functions(driver, file_path, limit=10)
    
    functions = []
    for line in result.split('\n')[1:]:
        if 'fan_in=' in line:
            # 解析 "func_name fan_in=x fan_out=y"
            parts = line.split()
            if parts:
                name = parts[0]
                functions.append({"name": name, "file": file_path})
    
    return functions


def react_search(driver, client, question: str) -> Dict:
    """
    ReAct 迭代检索主流程 (P0改进版)
    新增:
    1. Fallback搜索: Embedding效果不佳时主动Grep搜索
    2. 智能停止: 递减回报检测，提前停止无效扩展
    3. 详细工具调用记录
    """
    collected = {
        "functions": [],
        "issues": [],
        "call_chains": [],
        "explored_files": set(),
        "steps": [],
        "tool_calls": []  # 新增: 记录所有工具调用
    }
    
    # Step 1: 初始检索
    print(f"    [Step 1] 初始检索...", flush=True)
    code_results = search_code_embedding(client, question, top_k=5)
    issue_results = search_issues(client, question, top_k=3)
    
    # 记录工具调用
    collected["tool_calls"].append({
        "step": 1,
        "tool": "semantic_search",
        "params": {"top_k": 5},
        "results": len(code_results)
    })
    collected["tool_calls"].append({
        "step": 1,
        "tool": "issue_search", 
        "params": {"top_k": 3},
        "results": len(issue_results)
    })
    
    # P0改进: Fallback搜索 - 当Embedding效果不佳时
    max_score = max([r.get('score', 0) for r in code_results], default=0)
    fallback_triggered = False
    fallback_count = 0
    
    if max_score < FALLBACK_THRESHOLD:
        fallback_triggered = True
        print(f"      Embedding最高相似度{max_score:.2f} < {FALLBACK_THRESHOLD}，触发主动搜索...")
        entities = extract_entities_from_question(client, question)
        print(f"      提取实体: {entities}")
        
        for entity in entities:
            print(f"      Grep搜索: {entity}...")
            grep_results = grep_search(entity, REPO_ROOT, max_results=3)
            if grep_results:
                fallback_funcs = convert_grep_to_function_results(grep_results)
                print(f"        找到 {len(fallback_funcs)} 个相关函数")
                fallback_count += len(fallback_funcs)
                # 合并到code_results，避免重复
                for fn in fallback_funcs:
                    if not any(f['name'] == fn['name'] for f in code_results):
                        code_results.append(fn)
        
        # 记录fallback工具调用
        if fallback_count > 0:
            collected["tool_calls"].append({
                "step": 1,
                "tool": "grep_fallback",
                "params": {"entities": entities},
                "results": fallback_count
            })
    
    collected["functions"] = code_results
    collected["issues"] = issue_results
    collected["steps"].append({
        "step": 1, 
        "action": "initial_search", 
        "found": len(code_results),
        "new": len(code_results),
        "info_gain": len(code_results),
        "fallback_triggered": fallback_triggered
    })
    
    # P0改进: 智能停止 - 记录信息增益历史
    info_gain_history = []
    
    # Step 2-5: 迭代扩展
    for step in range(2, MAX_STEPS + 1):
        print(f"    [Step {step}] ReAct 决策...", flush=True)
        
        # 记录当前函数数
        func_count_before = len(collected["functions"])
        
        decision = react_decide(client, question, collected, step)
        print(f"      思考: {decision.get('thought', '')[:80]}...")
        print(f"      行动: {decision.get('action')} -> {decision.get('target', '')}")
        
        step_record = {
            "step": step,
            "action": decision.get('action'),
            "target": decision.get('target', ''),
            "reasoning": decision.get('thought', '')
        }
        
        if decision.get("sufficient"):
            print(f"      ✓ 信息充足，停止迭代")
            step_record["action"] = "stop_sufficient"
            collected["steps"].append(step_record)
            break
        
        action = decision.get("action")
        target = decision.get("target", "")
        
        if action in ["expand_callers", "expand_callees"] and target:
            direction = "callers" if action == "expand_callers" else "callees"
            tool_name = f"neo4j_{direction}"
            print(f"      扩展 {target} 的{direction}...")
            
            # 记录工具调用
            tool_call_record = {
                "step": step,
                "tool": tool_name,
                "params": {"function": target, "direction": direction},
                "results": 0
            }
            
            chain = expand_call_chain(driver, target, direction)
            
            # 添加新发现的函数
            new_funcs_count = 0
            new_func_names = []
            for fn in chain["functions"]:
                if not any(f['name'] == fn['name'] for f in collected["functions"]):
                    fn['score'] = 0.5  # 扩展发现的函数给一个基础分
                    fn['source'] = f'{direction}_of_{target}'
                    collected["functions"].append(fn)
                    new_funcs_count += 1
                    new_func_names.append(fn.get('name', ''))
            
            print(f"      新增 {new_funcs_count} 个函数")
            
            # 更新工具调用记录
            tool_call_record["results"] = new_funcs_count
            tool_call_record["new_functions"] = new_func_names[:5]
            collected["tool_calls"].append(tool_call_record)
            
            collected["call_chains"].append({
                "from": target,
                "direction": direction,
                "found": len(chain["functions"]),
                "new": new_funcs_count
            })
            
            # 更新步骤记录
            step_record["found"] = len(chain["functions"])
            step_record["new"] = new_funcs_count
            step_record["info_gain"] = new_funcs_count
            step_record["details"] = {
                "target_function": target,
                "direction": direction,
                "new_functions": new_func_names[:5]
            }
            
            # P0改进: 计算信息增益
            info_gain = new_funcs_count
            info_gain_history.append(info_gain)
            
            # P0改进: 智能停止 - 递减回报检测
            if step >= 3 and len(info_gain_history) >= 2:
                recent_gains = info_gain_history[-2:]
                if all(g <= 1 for g in recent_gains):
                    print(f"      ⚠️ 连续{len(recent_gains)}轮信息增益≤1，触发熔断停止")
                    step_record["stop_reason"] = "diminishing_returns"
                    collected["steps"].append(step_record)
                    break
            
        collected["steps"].append(step_record)
    
    return collected


def call_llm_with_retry(client, messages, max_tokens=1000, timeout=20, max_retries=3):
    """带重试机制的 LLM 调用"""
    import time
    
    for attempt in range(max_retries):
        try:
            resp = client.chat.completions.create(
                model=LLM_MODEL,
                messages=messages,
                max_tokens=max_tokens,
                timeout=timeout
            )
            return resp.choices[0].message.content or "(无答案)"
        except Exception as e:
            error_msg = str(e).lower()
            # 判断是否是可重试的错误
            retryable = any(kw in error_msg for kw in [
                'timeout', 'connection', 'rate limit', 'too many requests',
                'temporarily unavailable', 'service unavailable', '503', '502', '504'
            ])
            
            if not retryable or attempt == max_retries - 1:
                # 最后一次或不可重试的错误
                return f"生成答案失败: {e}"
            
            # 指数退避等待
            wait_time = 2 ** attempt  # 1s, 2s, 4s
            print(f"      API 调用失败 (尝试 {attempt+1}/{max_retries}): {e}")
            print(f"      等待 {wait_time}s 后重试...")
            time.sleep(wait_time)
    
    return "生成答案失败: 达到最大重试次数"


def generate_answer(client, question: str, collected: Dict) -> str:
    funcs = collected.get("functions", [])
    issues = collected.get("issues", [])
    steps = collected.get("steps", [])
    
    # 按来源和分数排序函数
    embedding_funcs = [f for f in funcs if f.get('source') == 'embedding']
    chain_funcs = [f for f in funcs if 'caller' in f.get('source', '') or 'callee' in f.get('source', '')]
    
    lines = []
    
    # 高相关函数（embedding，相似度>0.7）
    high_rel = [f for f in embedding_funcs if f.get('score', 0) > 0.7]
    if high_rel:
        lines.append("【高相关函数】(Embedding检索，相似度>0.7)")
        for i, fn in enumerate(high_rel[:5]):
            lines.append(f"{i+1}. {fn['name']} @ {fn['file']}")
            if fn.get('text'):
                lines.append(f"   代码: {fn['text'][:150]}")
    
    # 中相关函数
    mid_rel = [f for f in embedding_funcs if 0.5 <= f.get('score', 0) <= 0.7]
    if mid_rel:
        lines.append(f"\n【相关函数】(相似度0.5-0.7)")
        for i, fn in enumerate(mid_rel[:3]):
            lines.append(f"{i+1}. {fn['name']}")
    
    # 调用链扩展的函数
    if chain_funcs:
        lines.append(f"\n【调用链相关函数】(通过ReAct扩展发现)")
        for i, fn in enumerate(chain_funcs[:5]):
            lines.append(f"{i+1}. {fn['name']} [{fn.get('source')}]")
    
    # Issue 信息
    if issues:
        lines.append(f"\n【相关Issue/PR】")
        for i, issue in enumerate(issues[:3]):
            lines.append(f"{i+1}. #{issue['number']}: {issue['title']}")
            if issue.get('body'):
                lines.append(f"   {issue['body'][:250]}")
    
    # ReAct探索过程
    if steps:
        lines.append(f"\n【ReAct探索过程】({len(steps)}轮迭代)")
        for step in steps:
            action = step.get('action', '')
            target = step.get('target', '')
            if action == 'initial_search':
                lines.append(f"  - 初始检索: 发现{step.get('found', 0)}个函数")
            elif action in ['expand_callers', 'expand_callees']:
                lines.append(f"  - 扩展{action.split('_')[1]}: {target}")
    
    context_text = "\n".join(lines)
    
    prompt = f"""你是llama.cpp资深代码专家。请基于以下通过多轮ReAct迭代检索收集的信息，深入分析问题并给出准确答案。

【检索到的信息】
{context_text}

【用户问题】
{question}

【回答要求】
1. 首先理解问题的核心：是在问实现细节、调用关系、还是设计决策？
2. 优先使用高相关函数(相似度>0.7)的信息
3. 如果涉及调用流程，结合调用链扩展的信息
4. 如果有相关Issue，参考其中的设计讨论
5. 回答要具体、准确，引用函数名和文件位置
6. 如果不确定，说明基于哪些信息推断

请用中文详细回答："""
    
    return call_llm_with_retry(
        client,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1000,
        timeout=20,
        max_retries=3
    )


def process_single(driver, client, row: dict, idx: int) -> dict:
    """处理单个问题"""
    question = row.get("具体问题", "")
    reference = row.get("答案", "")
    
    print(f"[{idx}] {question[:50]}...", flush=True)
    
    t0 = time.time()
    try:
        collected = react_search(driver, client, question)
        answer = generate_answer(client, question, collected)
        latency = time.time() - t0
        
        return {
            "index": idx,
            "具体问题": question,
            "参考答案": reference,
            "生成答案": answer,
            "路由类型": "V7_P0_Improved",
            "检索结果": {
                "function_count": len(collected.get("functions", [])),
                "issue_count": len(collected.get("issues", [])),
                "step_count": len(collected.get("steps", [])),
            },
            "检索过程": collected.get("steps", []),
            "工具调用": collected.get("tool_calls", []),
            "延迟_s": latency,
            "错误": None
        }
    except Exception as e:
        latency = time.time() - t0
        print(f"    ERROR: {e}")
        import traceback
        traceback.print_exc()
        return {
            "index": idx,
            "具体问题": question,
            "参考答案": reference,
            "生成答案": "",
            "路由类型": "V7_P0_Improved",
            "检索结果": {},
            "检索过程": [],
            "工具调用": [],
            "延迟_s": latency,
            "错误": str(e)
        }


def main():
    parser = argparse.ArgumentParser(description="QA V7: ReAct Agent 迭代检索")
    parser.add_argument("--csv", type=Path, required=True, help="输入 CSV 文件")
    parser.add_argument("--output", type=Path, required=True, help="输出 JSON 文件")
    parser.add_argument("--workers", type=int, default=3, help="并行数（ReAct 每题多轮，建议用少一点）")
    args = parser.parse_args()
    
    if not OPENAI_API_KEY:
        print("ERROR: OPENAI_API_KEY 未设置", file=sys.stderr)
        sys.exit(1)
    
    # 读取 CSV
    rows = []
    with open(args.csv, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    print(f"共 {len(rows)} 题需要处理")
    
    # 连接 Neo4j
    driver = get_driver()
    driver.verify_connectivity()
    print("Neo4j 连接成功")
    
    # 预加载索引
    global _rag_index, _issue_index
    _rag_index = _load_rag_index()
    _issue_index = _load_issue_index()
    print("索引加载完成")
    
    # OpenAI 客户端
    client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)
    
    # 并行处理（ReAct 每题多轮，workers 不宜过多）
    results = []
    completed_count = 0
    save_interval = 10  # 每10题保存一次
    
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(process_single, driver, client, row, i): i 
            for i, row in enumerate(rows)
        }
        
        for future in as_completed(futures):
            try:
                result = future.result()
                results.append(result)
                completed_count += 1
                
                # 实时保存，防止丢失
                if completed_count % save_interval == 0 or completed_count == len(rows):
                    print(f"  已完成 {completed_count}/{len(rows)} 题...")
                    # 按index排序后保存
                    sorted_results = sorted(results, key=lambda x: x.get('index', 0))
                    with open(args.output, "w", encoding="utf-8") as f:
                        json.dump(sorted_results, f, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f"  处理题目时出错: {e}")
                completed_count += 1
    
    # 最终保存（确保排序）
    sorted_results = sorted(results, key=lambda x: x.get('index', 0))
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(sorted_results, f, ensure_ascii=False, indent=2)
    
    print(f"\n完成！共处理 {len(results)}/{len(rows)} 题，结果保存至: {args.output}")
    driver.close()


if __name__ == "__main__":
    main()
