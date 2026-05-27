#!/usr/bin/env python3
"""
Issue Benchmark - React Search Baseline (包含 Issue 检索)
修复：初始检索也包含 issue 节点
"""
from __future__ import annotations

import sys
import json
import argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))

from config import LLM_MODEL
from tools.core import (
    get_neo4j_driver, close_neo4j_driver,
    call_llm_json, generate_answer
)
from tools.search import (
    search_functions_by_text, expand_call_chain,
    search_issues, extract_entities_from_question, grep_codebase,
    convert_grep_to_function_results, search_module_functions
)
from tools.search.semantic_search import _load_rag_index, get_embedding, cosine_similarity

MAX_STEPS = 5
FALLBACK_THRESHOLD = 0.5


def search_all_by_text(query_text: str, top_k: int = 5) -> list:
    """语义搜索：同时搜索函数和 issue"""
    idx = _load_rag_index()
    if idx is None:
        return []
    
    query_emb = get_embedding(query_text)
    
    # 搜索所有类型的 chunk（不过滤）
    scores = []
    for i, chunk in enumerate(idx["chunks"]):
        sim = cosine_similarity(query_emb, idx["embeddings"][i])
        scores.append((sim, chunk))
    
    scores.sort(key=lambda x: -x[0])
    
    results = []
    for sim, chunk in scores[:top_k]:
        meta = chunk.get("meta", {})
        item = {
            'name': meta.get('name', chunk.get('id', '')),
            'file': meta.get('file', ''),
            'text': chunk.get('text', ''),
            'score': sim,
            'type': chunk.get('type', 'unknown'),
            'start_line': meta.get('start_line', meta.get('line', 0)),
            'end_line': meta.get('end_line', meta.get('line', 0))
        }
        results.append(item)
    
    return results


def initial_search(driver, client, question: str) -> dict:
    """初始检索：语义搜索(函数+issue) + Grep Fallback + Issue搜索"""
    # 语义搜索（不过滤类型，同时搜函数和issue）
    all_results = search_all_by_text(question, top_k=8)
    funcs = [r for r in all_results if r.get('type') == 'function'][:5]
    issues_from_emb = [r for r in all_results if r.get('type') == 'issue'][:3]
    
    max_score = max([f.get('score', 0) for f in funcs], default=0)
    fallback_triggered = False
    
    if max_score < FALLBACK_THRESHOLD:
        fallback_triggered = True
        entities = extract_entities_from_question(question)
        for entity in entities[:2]:
            if '-' in entity or entity.islower():
                module_funcs = search_module_functions(entity, limit=5)
                for fn in module_funcs:
                    if not any(f.get('name') == fn['name'] for f in funcs):
                        funcs.append(fn)
            grep_results = grep_codebase(entity, limit=3)
            if grep_results:
                new_funcs = convert_grep_to_function_results(grep_results)
                for fn in new_funcs:
                    if not any(f.get('name') == fn['name'] for f in funcs):
                        funcs.append(fn)
    
    # Issue搜索（Neo4j）
    issues = search_issues(question, top_k=3)
    
    steps = [{
        "step": 1,
        "action": "initial_search",
        "found_functions": len(funcs),
        "found_issues_emb": len(issues_from_emb),
        "fallback_triggered": fallback_triggered
    }]
    
    return {
        "functions": funcs,
        "issues": issues,
        "issues_from_emb": issues_from_emb,
        "steps": steps,
        "call_chains": [],
        "tool_calls": [],
        "fallback_triggered": fallback_triggered
    }


def react_decide(client, question: str, collected: dict, step: int) -> dict:
    funcs = collected.get("functions", [])
    chains = collected.get("call_chains", [])
    
    expanded = [c['from'] for c in chains]
    context_lines = [f"问题: {question}", f"\n【已收集函数】(共{len(funcs)}个):"]
    for i, f in enumerate(funcs[:5]):
        source = f.get('source', 'embedding') if f.get('source') else 'embedding'
        score = f.get('score', 0)
        marker = " [已扩展]" if f.get('name', '') in expanded else ""
        context_lines.append(f"{i+1}. {f.get('name', '')} ({source}, {score:.3f}){marker}")
    
    prompt = f"""{'\n'.join(context_lines)}

你是代码检索专家。请根据当前已收集的信息和问题类型，选择最合适的下一步行动。

【可用工具】
1. expand_callers - 扩展某个函数的调用者
2. expand_callees - 扩展某个函数的被调用者
3. sufficient - 信息充足，可以生成答案

返回JSON:
{{
    "thought": "分析",
    "sufficient": false,
    "action": "expand_callers|expand_callees|sufficient",
    "target": "目标函数名"
}}

只输出JSON:"""
    
    result = call_llm_json(messages=[{"role": "user", "content": prompt}], max_tokens=150)
    if result is None or step >= 4:
        return {"sufficient": True, "action": "sufficient", "target": ""}
    
    if result.get("sufficient") or result.get("action") == "sufficient":
        return {"sufficient": True, "action": "sufficient", "target": ""}
    
    action = result.get("action", "")
    target = result.get("target", "")
    
    valid = [f.get('name', '') for f in funcs[:8]]
    if target not in valid:
        for f in funcs[:5]:
            if f.get('name', '') not in expanded:
                target = f.get('name', '')
                break
        else:
            target = funcs[0].get('name', '') if funcs else ""
    
    final_action = action if action in ["expand_callers", "expand_callees"] else "expand_callees"
    return {"thought": result.get("thought", ""), "sufficient": False, "action": final_action, "target": target}


def react_search(driver, client, question: str) -> dict:
    collected = initial_search(driver, client, question)
    info_gain_history = []
    
    for step in range(2, MAX_STEPS + 1):
        decision = react_decide(client, question, collected, step)
        action = decision.get("action")
        target = decision.get("target", "")
        
        if decision.get("sufficient") or action == "sufficient":
            break
        
        if action in ["expand_callers", "expand_callees"] and target:
            direction = "callers" if action == "expand_callers" else "callees"
            chain = expand_call_chain(target, direction)
            new_count = 0
            for fn in chain["functions"]:
                if not any(f.get('name') == fn['name'] for f in collected["functions"]):
                    fn['score'] = 0.5
                    fn['source'] = f'{direction}_of_{target}'
                    collected["functions"].append(fn)
                    new_count += 1
            
            collected["call_chains"].append({"from": target, "direction": direction, "found": len(chain["functions"]), "new": new_count})
            collected["steps"].append({"step": step, "action": action, "target": target, "found": len(chain["functions"]), "new": new_count})
            info_gain_history.append(new_count)
            if step >= 3 and len(info_gain_history) >= 2:
                if all(g <= 1 for g in info_gain_history[-2:]):
                    break
    
    return collected


def build_context_for_issue(collected: dict) -> str:
    """构建包含 issue 的 context"""
    parts = []
    
    # 函数
    funcs = collected.get("functions", [])
    if funcs:
        parts.append("【相关函数】")
        for i, f in enumerate(funcs, 1):
            parts.append(f"[{i}] {f.get('name', '')} ({f.get('file', '')})\n{f.get('text', '')[:800]}")
    
    # 从 embedding 检索到的 issue
    issues_emb = collected.get("issues_from_emb", [])
    if issues_emb:
        parts.append("\n【相关 Issue (Embedding检索)】")
        for i, issue in enumerate(issues_emb, 1):
            parts.append(f"[{i}] {issue.get('name', '')}\n{issue.get('text', '')[:1000]}")
    
    # 从 Neo4j 检索到的 issue
    issues = collected.get("issues", [])
    if issues:
        parts.append("\n【相关 Issue (Neo4j检索)】")
        for i, issue in enumerate(issues, 1):
            title = issue.get('title', '')
            body = issue.get('body', '')
            parts.append(f"[{i}] #{issue.get('number', '')}: {title[:80]}\n{body[:1000]}")
    
    return "\n\n---\n\n".join(parts)


def generate_answer_for_issue(client, question: str, collected: dict) -> tuple[str, dict]:
    """生成答案（包含 issue context）"""
    from tools.core.prompt_loader import load_prompt
    context = build_context_for_issue(collected)
    prompt = load_prompt("answer_generation", context=context, question=question)
    
    try:
        from openai import OpenAI
        from config import OPENAI_API_KEY, OPENAI_BASE_URL
        client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)
        kwargs = {
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "timeout": 120,
        }
        if LLM_MODEL.startswith("gpt-5") or LLM_MODEL.startswith("o1") or LLM_MODEL.startswith("o3"):
            kwargs["max_completion_tokens"] = 1000
        else:
            kwargs["max_tokens"] = 1000
        resp = client.chat.completions.create(**kwargs)
        answer = (resp.choices[0].message.content or "").strip()
        usage = resp.usage.model_dump() if resp.usage else {}
        return answer, usage
    except Exception as e:
        return f"[生成失败: {e}]", {}


def process_single(driver, client, item: dict, idx: int) -> dict:
    import time
    start_time = time.time()
    question = item.get('question', '')
    
    try:
        collected = react_search(driver, client, question)
        answer, usage = generate_answer_for_issue(client, question, collected)
        latency = time.time() - start_time
        
        return {
            "index": idx,
            "id": item.get('id', f"issue_{idx}"),
            "issue_number": item.get('issue_number'),
            "difficulty": item.get('difficulty'),
            "question_type": item.get('question_type'),
            "question": question,
            "answer": item.get('answer', ''),
            "generated": answer,
            "retrieval": {
                "function_count": len(collected.get("functions", [])),
                "issue_count": len(collected.get("issues", [])),
                "issue_emb_count": len(collected.get("issues_from_emb", [])),
                "step_count": len(collected.get("steps", []))
            },
            "latency_s": latency,
            "token_usage": usage
        }
    except Exception as e:
        import traceback
        return {
            "index": idx,
            "id": item.get('id', f"issue_{idx}"),
            "question": question,
            "generated": f"处理失败: {str(e)}\n{traceback.format_exc()}",
            "error": str(e),
            "latency_s": time.time() - start_time
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, default=Path("datasets/llama_cpp_issue_benchmark.with_answers.json"))
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("-w", "--workers", type=int, default=30)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    
    with open(args.benchmark, encoding="utf-8") as f:
        data = json.load(f)
    questions = data.get("questions", [])
    if args.limit > 0:
        questions = questions[:args.limit]
    
    print(f"Issue Benchmark (With Issues): {len(questions)} 题")
    
    driver = get_neo4j_driver()
    from tools.core.llm_client import get_llm_client
    client = get_llm_client()
    
    results = [None] * len(questions)
    completed = 0
    
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(process_single, driver, client, q, i): i
            for i, q in enumerate(questions)
        }
        for future in as_completed(futures):
            i = futures[future]
            try:
                res = future.result()
                results[i] = res
                completed += 1
                if completed % 10 == 0 or completed == len(questions):
                    print(f"  [{completed}/{len(questions)}] 已完成")
                    sorted_results = sorted([r for r in results if r], key=lambda x: x.get('index', 0))
                    with open(args.output, 'w', encoding='utf-8') as f:
                        json.dump(sorted_results, f, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f"  处理失败: {e}")
                completed += 1
    
    sorted_results = sorted([r for r in results if r], key=lambda x: x.get('index', 0))
    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(sorted_results, f, ensure_ascii=False, indent=2)
    
    print(f"\n完成！结果保存至: {args.output}")
    close_neo4j_driver()


if __name__ == "__main__":
    main()
