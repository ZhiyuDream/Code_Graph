#!/usr/bin/env python3
"""
V7 Final - 简化版QA脚本
采用模块化架构，只保留核心工具
"""
from __future__ import annotations

import sys
import json
import csv
import argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# 添加项目根目录到路径
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.core import (
    get_neo4j_driver, close_neo4j_driver, run_cypher,
    call_llm, call_llm_json, generate_answer
)
from src.core.prompt_loader import load_prompt
from src.search import (
    search_functions_by_text, expand_call_chain,
    search_issues, extract_entities_from_question, grep_codebase,
    convert_grep_to_function_results, search_module_functions,
    expand_same_file, expand_same_class
)

# 常量
MAX_STEPS = 5
FALLBACK_THRESHOLD = 0.5


def _extract_target_function(question: str) -> str:
    """从问题中提取目标函数名（反引号内的内容）"""
    if '`' in question:
        parts = question.split('`')
        if len(parts) >= 2:
            return parts[1]
    return ''


def _lookup_by_name(func_name: str) -> list:
    """从 RAG index 按函数名直接查找，去重并优先保留代码更长的版本"""
    from src.search.semantic_search import _load_rag_index
    from src.search.code_reader import enrich_function_with_code
    idx = _load_rag_index()
    if not idx:
        return []
    # 收集所有匹配的 chunk
    candidates = []
    for c in idx['chunks']:
        if c['type'] == 'function' and c.get('meta', {}).get('name') == func_name:
            candidates.append(c)
    if not candidates:
        return []
    # 去重：按文件分组，每个文件保留 text 最长的
    by_file = {}
    for c in candidates:
        f = c['meta'].get('file', '')
        if f not in by_file or len(c.get('text', '')) > len(by_file[f].get('text', '')):
            by_file[f] = c
    # 优先 .cpp 文件
    results = []
    for f, c in sorted(by_file.items(), key=lambda x: (not x[0].endswith('.cpp'), -len(x[1].get('text', '')))):
        func = {
            'name': func_name,
            'file': c['meta'].get('file', ''),
            'text': c.get('text', ''),
            'score': 1.0,
            'source': 'name_lookup',
            'start_line': c['meta'].get('start_line', 0),
            'end_line': c['meta'].get('end_line', 0),
        }
        func = enrich_function_with_code(func)
        results.append(func)
    return results


def initial_search(driver, client, question: str) -> dict:
    """初始检索：目标函数直接查找 + 语义搜索 + Grep Fallback + Issue搜索"""
    # 1. 如果问题提到具体函数名，直接按名字查找
    target_name = _extract_target_function(question)
    funcs = []
    if target_name:
        funcs = _lookup_by_name(target_name)
        if funcs:
            print(f"      直接查找到目标函数: {target_name}")

    # 2. 语义搜索
    semantic_funcs = search_functions_by_text(question, top_k=5)
    for f in semantic_funcs:
        if not any(existing['name'] == f['name'] for existing in funcs):
            funcs.append(f)
    
    # 检查是否需要Grep Fallback
    max_score = max([f.get('score', 0) for f in funcs], default=0)
    fallback_triggered = False
    
    if max_score < FALLBACK_THRESHOLD:
        fallback_triggered = True
        print(f"      Embedding最高相似度{max_score:.2f} < {FALLBACK_THRESHOLD}，触发主动搜索...")
        
        entities = extract_entities_from_question(question)
        print(f"      提取实体: {entities}")
        
        for entity in entities[:2]:
            # 尝试模块搜索（对于模块名如 ggml-blas）
            if '-' in entity or entity.islower():
                print(f"      模块搜索: {entity}...")
                module_funcs = search_module_functions(entity, limit=5)
                if module_funcs:
                    print(f"        找到 {len(module_funcs)} 个模块函数")
                    for fn in module_funcs:
                        if not any(f['name'] == fn['name'] for f in funcs):
                            funcs.append(fn)
            
            # Grep搜索
            print(f"      Grep搜索: {entity}...")
            grep_results = grep_codebase(entity, limit=3)
            if grep_results:
                print(f"        找到 {len(grep_results)} 个相关函数")
                new_funcs = convert_grep_to_function_results(grep_results)
                for fn in new_funcs:
                    if not any(f['name'] == fn['name'] for f in funcs):
                        funcs.append(fn)
    
    # Issue搜索
    issues = search_issues(question, top_k=3)

    # 去重：同名函数保留 text 最长的版本
    deduped = {}
    for f in funcs:
        name = f.get('name', '')
        if name not in deduped or len(f.get('text', '')) > len(deduped[name].get('text', '')):
            deduped[name] = f
    funcs = list(deduped.values())

    # 补充所有函数的完整代码（不只是目标函数）
    from src.search.code_reader import enrich_function_with_code
    for f in funcs:
        if len(f.get('text', '')) < 200:
            enrich_function_with_code(f)

    # 记录初始步骤
    steps = [{
        "step": 1,
        "action": "initial_search",
        "found": len(funcs),
        "fallback_triggered": fallback_triggered
    }]

    return {
        "functions": funcs,
        "issues": issues,
        "steps": steps,
        "call_chains": [],
        "tool_calls": [],
        "fallback_triggered": fallback_triggered
    }


_REACT_DECIDE_PROMPT_TEMPLATE = "react_decide"

def react_decide(client, question: str, collected: dict, step: int, prompt_template: str = None) -> dict:
    """ReAct决策：选择下一步行动"""
    funcs = collected.get("functions", [])
    chains = collected.get("call_chains", [])
    issues = collected.get("issues", [])
    
    # 统计变量（用于调试）
    if not hasattr(react_decide, 'stats'):
        react_decide.stats = {"total": 0, "default_used": 0, "actions": {}}
    
    # 构建当前上下文（参考P0版本格式）
    context_lines = [f"问题: {question}"]

    # 已扩展的函数
    expanded = [c['from'] for c in chains]
    func_items = []
    for i, f in enumerate(funcs[:8]):
        source = f.get('source', 'embedding') if f.get('source') else 'embedding'
        score = f.get('score', 0)
        marker = " [已扩展]" if f['name'] in expanded else ""
        func_items.append(f"{i+1}. {f['name']} ({source}, {score:.3f}){marker}")
    
    if issues:
        context_lines.append(f"\n【相关Issue】(共{len(issues)}个):")
        for i, issue in enumerate(issues[:2]):
            context_lines.append(f"{i+1}. #{issue['number']}: {issue['title'][:50]}")
    
    if chains:
        context_lines.append(f"\n【已扩展调用链】(共{len(chains)}条):")
        for c in chains[-3:]:
            context_lines.append(f"  - {c['from']}: {c['direction']} (找到{c['found']}个, 新增{c['new']}个)")
    
    context = '\n'.join(context_lines)
    
    template = prompt_template or _REACT_DECIDE_PROMPT_TEMPLATE
    
    # 如果模板是内置的 baseline prompt，直接硬编码
    if template == "react_decide_baseline":
        prompt = f"""{context}

---

你是代码检索专家。请根据当前已收集的信息和问题类型，选择最合适的下一步行动。

【可用工具】
1. expand_callers - 扩展某个函数的调用者（谁调用了它）
   适用：调用链、依赖关系、流程分析问题
   
2. expand_callees - 扩展某个函数的被调用者（它调用了谁）
   适用：执行流程、内部实现细节问题
   
3. sufficient - 信息充足，可以生成答案
   适用：已有足够证据回答问题

【决策原则】
- 调用链/依赖问题 → 用 expand_callers/callees
- 信息已足够 → 用 sufficient

【重要】不要重复扩展已标记[已扩展]的函数。

返回JSON:
{{
    "thought": "分析现有信息和下一步计划",
    "sufficient": false,
    "action": "expand_callers|expand_callees|sufficient",
    "target": "目标函数名"
}}

只输出JSON:"""
    else:
        # 从外部文件加载 prompt
        # 构建 issue_list 和 chain_list
        issue_lines = []
        for i, issue in enumerate(issues[:3]):
            issue_lines.append(f"{i+1}. #{issue['number']}: {issue['title'][:80]}")
        issue_list = "\n".join(issue_lines) if issue_lines else "无"

        chain_lines = []
        for c in chains[-3:]:
            chain_lines.append(f"  - {c['from']}: {c['direction']} (找到{c['found']}个, 新增{c['new']}个)")
        chain_list = "\n".join(chain_lines) if chain_lines else "无"

        from src.core.prompt_loader import format_actions_for_prompt
        actions = format_actions_for_prompt()

        prompt = load_prompt(
            template,
            question=question,
            context=context,
            function_count=len(funcs),
            function_list="\n".join(func_items),
            issue_count=len(issues),
            issue_list=issue_list,
            chain_count=len(chains),
            chain_list=chain_list,
            actions=actions,
            action_choices="expand_callers|expand_callees|sufficient"
        )
    
    result = call_llm_json(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=150
    )
    
    if result is None:
        # 出错时默认sufficient
        return {"sufficient": True, "action": "sufficient", "target": ""}
    
    # 强制停止条件
    if step >= 4:
        return {"sufficient": True, "action": "sufficient", "target": ""}
    
    if result.get("sufficient") or result.get("action") == "sufficient":
        return {"sufficient": True, "action": "sufficient", "target": ""}
    
    action = result.get("action", "")
    target = result.get("target", "")
    
    # 验证target
    valid = [f['name'] for f in funcs[:8]]
    if target not in valid:
        # 智能选择未扩展的函数
        for f in funcs[:5]:
            if f['name'] not in expanded:
                target = f['name']
                break
        else:
            target = funcs[0]['name'] if funcs else ""
    
    # 统计
    valid_actions = ["expand_callers", "expand_callees", "expand_same_file",
                     "expand_same_class", "grep_search", "semantic_search", "sufficient"]
    react_decide.stats["total"] += 1
    final_action = action if action in valid_actions else "sufficient"
    if final_action != action:
        react_decide.stats["default_used"] += 1
    react_decide.stats["actions"][final_action] = react_decide.stats["actions"].get(final_action, 0) + 1

    return {
        "thought": result.get("thought", ""),
        "sufficient": final_action == "sufficient",
        "action": final_action,
        "target": target
    }


def _add_new_funcs(collected, new_funcs, source_tag):
    """去重并添加新函数到 collected，返回新增数量。自动 enrich 缺少代码的函数。"""
    from src.search.code_reader import enrich_function_with_code
    new_count = 0
    for fn in new_funcs:
        if not any(f['name'] == fn['name'] for f in collected["functions"]):
            fn['score'] = fn.get('score', 0.5)
            fn['source'] = source_tag
            # 补充完整代码
            if len(fn.get('text', '')) < 200:
                enrich_function_with_code(fn)
            collected["functions"].append(fn)
            new_count += 1
    return new_count


def react_search(driver, client, question: str, prompt_template: str = None) -> dict:
    """ReAct迭代检索主流程"""
    # 初始检索（已包含Grep Fallback）
    collected = initial_search(driver, client, question)

    info_gain_history = []

    # ReAct迭代
    for step in range(2, MAX_STEPS + 1):
        decision = react_decide(client, question, collected, step, prompt_template)
        action = decision.get("action")
        target = decision.get("target", "")

        if decision.get("sufficient") or action == "sufficient":
            break

        new_count = 0
        found = 0

        if action in ["expand_callers", "expand_callees"] and target:
            direction = "callers" if action == "expand_callers" else "callees"
            chain = expand_call_chain(target, direction)
            found = len(chain["functions"])
            new_count = _add_new_funcs(collected, chain["functions"], f'{direction}_of_{target}')

            # 如果 Neo4j 没找到 caller，用 grep 补充搜索
            if direction == "callers" and found == 0:
                grep_results = grep_codebase(target, limit=5)
                if grep_results:
                    grep_funcs = convert_grep_to_function_results(grep_results)
                    # 排除目标函数自身
                    grep_funcs = [f for f in grep_funcs if f.get('name') != target]
                    extra = _add_new_funcs(collected, grep_funcs, f'grep_callers_of_{target}')
                    found += len(grep_funcs)
                    new_count += extra
                    if extra:
                        print(f"      Grep补充caller: {extra} 个")

            collected["call_chains"].append({
                "from": target, "direction": direction,
                "found": found, "new": new_count
            })

        elif action == "expand_same_file" and target:
            # 找到目标函数所在的文件
            target_func = next((f for f in collected["functions"] if f['name'] == target), None)
            if target_func:
                file_path = target_func.get('file', '')
                neighbors = expand_same_file(target, file_path)
                found = len(neighbors)
                new_count = _add_new_funcs(collected, neighbors, 'file_expansion')

        elif action == "expand_same_class" and target:
            target_func = next((f for f in collected["functions"] if f['name'] == target), None)
            if target_func:
                file_path = target_func.get('file', '')
                class_members = expand_same_class(target, file_path)
                found = len(class_members)
                new_count = _add_new_funcs(collected, class_members, 'class_expansion')

        elif action == "grep_search" and target:
            grep_results = grep_codebase(target, limit=5)
            found = len(grep_results)
            if grep_results:
                new_funcs = convert_grep_to_function_results(grep_results)
                new_count = _add_new_funcs(collected, new_funcs, 'grep_react')

        elif action == "semantic_search" and target:
            new_funcs = search_functions_by_text(target, top_k=5)
            found = len(new_funcs)
            new_count = _add_new_funcs(collected, new_funcs, 'semantic_react')

        # 记录步骤
        collected["steps"].append({
            "step": step, "action": action, "target": target,
            "found": found, "new": new_count
        })

        info_gain_history.append(new_count)

        # 递减回报检测
        if step >= 3 and len(info_gain_history) >= 2:
            if all(g <= 1 for g in info_gain_history[-2:]):
                break

    return collected


def process_single(driver, client, row: dict, idx: int, prompt_template: str = None) -> dict:
    """处理单个问题"""
    print(f"[{idx}] {row.get('具体问题', 'N/A')[:50]}...")
    
    import time
    start_time = time.time()
    
    question = row.get('具体问题', '')
    
    try:
        # ReAct检索
        collected = react_search(driver, client, question, prompt_template)
        
        # 生成答案
        answer = generate_answer(question, collected)
        
        latency = time.time() - start_time
        
        # 记录每步召回的函数详情
        from src.core.llm_client import get_usage_stats
        usage = get_usage_stats()

        return {
            "index": idx,
            "具体问题": question,
            "参考答案": row.get('答案', ''),
            "生成答案": answer,
            "路由类型": "V7_Final",
            "检索详情": {
                "steps": collected.get("steps", []),
                "functions": [
                    {"name": f.get("name"), "file": f.get("file"),
                     "source": f.get("source"), "score": round(f.get("score", 0), 3),
                     "text_len": len(f.get("text", ""))}
                    for f in collected.get("functions", [])
                ],
                "issues": [
                    {"number": i.get("number"), "title": i.get("title")}
                    for i in collected.get("issues", [])
                ],
                "call_chains": collected.get("call_chains", []),
            },
            "延迟_s": latency,
            "token_usage": usage
        }
        
    except Exception as e:
        return {
            "index": idx,
            "具体问题": question,
            "生成答案": f"处理失败: {str(e)}",
            "路由类型": "V7_Final",
            "错误": str(e),
            "延迟_s": time.time() - start_time
        }


def main():
    parser = argparse.ArgumentParser(description="V7 Final - Simplified QA")
    parser.add_argument("--csv", type=Path, required=True, help="输入CSV文件")
    parser.add_argument("--output", type=Path, required=True, help="输出JSON文件")
    parser.add_argument("--prompt", type=str, default=None, help="ReAct决策prompt模板名 (如 react_decide_gpt54_style)")
    parser.add_argument("--workers", type=int, default=20, help="并行数")
    args = parser.parse_args()
    
    # 读取CSV
    rows = []
    with open(args.csv, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    
    prompt_template = args.prompt
    if prompt_template:
        print(f"使用自定义prompt模板: {prompt_template}")
    print(f"共 {len(rows)} 题需要处理")
    
    # 连接Neo4j
    driver = get_neo4j_driver()
    
    # 从core模块导入client
    from src.core.llm_client import get_llm_client
    client = get_llm_client()
    
    # 并行处理
    results = []
    completed = 0
    
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(process_single, driver, client, row, i, prompt_template): i
            for i, row in enumerate(rows)
        }
        
        for future in as_completed(futures):
            try:
                result = future.result()
                results.append(result)
                completed += 1
                
                if completed % 10 == 0 or completed == len(rows):
                    print(f"  已完成 {completed}/{len(rows)} 题...")
                    # 排序保存
                    sorted_results = sorted(results, key=lambda x: x.get('index', 0))
                    with open(args.output, 'w', encoding='utf-8') as f:
                        json.dump(sorted_results, f, ensure_ascii=False, indent=2)
                        
            except Exception as e:
                print(f"  处理题目时出错: {e}")
                completed += 1
    
    # 最终保存
    sorted_results = sorted(results, key=lambda x: x.get('index', 0))
    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(sorted_results, f, ensure_ascii=False, indent=2)
    
    print(f"\n完成！共处理 {len(results)}/{len(rows)} 题，结果保存至: {args.output}")
    
    # 关闭连接
    close_neo4j_driver()


if __name__ == "__main__":
    main()
