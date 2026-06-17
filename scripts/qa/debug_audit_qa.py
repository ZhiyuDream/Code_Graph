#!/usr/bin/env python3
from __future__ import annotations

"""
Audit QA 单题调试：详细分析 ReAct 每一步的输入输出。

用法:
    python scripts/qa/debug_audit_qa.py --qa-id audit_001
    python scripts/qa/debug_audit_qa.py --qa-id audit_004 --verbose
"""

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.core import get_neo4j_driver, close_neo4j_driver, call_llm, call_llm_json, generate_answer
from src.core.llm_client import get_llm_client
from src.core.prompt_loader import load_prompt
from src.search import (
    search_functions_by_text, expand_call_chain,
    search_issues, extract_entities_from_question, grep_codebase,
    convert_grep_to_function_results, search_module_functions,
    expand_same_file, expand_same_class,
)
from src.search.code_reader import enrich_function_with_code
from src.search.semantic_search import _load_rag_index

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
    """从 RAG index 按函数名直接查找"""
    idx = _load_rag_index()
    if not idx:
        return []
    candidates = []
    for c in idx['chunks']:
        if c['type'] == 'function' and c.get('meta', {}).get('name') == func_name:
            candidates.append(c)
    if not candidates:
        return []
    by_file = {}
    for c in candidates:
        f = c['meta'].get('file', '')
        if f not in by_file or len(c.get('text', '')) > len(by_file[f].get('text', '')):
            by_file[f] = c
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


def _add_new_funcs(collected, new_funcs, source_tag):
    """去重并添加新函数"""
    new_count = 0
    for fn in new_funcs:
        if not any(f['name'] == fn['name'] for f in collected["functions"]):
            fn['score'] = fn.get('score', 0.5)
            fn['source'] = source_tag
            if len(fn.get('text', '')) < 200:
                enrich_function_with_code(fn)
            collected["functions"].append(fn)
            new_count += 1
    return new_count


def initial_search(driver, client, question: str, verbose: bool = False) -> dict:
    """初始检索，带详细日志"""
    logs = []

    # 1. 目标函数直接查找
    target_name = _extract_target_function(question)
    funcs = []
    if target_name:
        funcs = _lookup_by_name(target_name)
        if funcs:
            logs.append(f"[Step 1] 直接查找目标函数: {target_name} -> {len(funcs)} 个")
            for f in funcs:
                logs.append(f"       - {f['name']} @ {f['file']}:{f.get('start_line', 0)}")
        else:
            logs.append(f"[Step 1] 直接查找目标函数: {target_name} -> 未找到")

    # 2. 语义搜索
    semantic_funcs = search_functions_by_text(question, top_k=5)
    for f in semantic_funcs:
        if not any(existing['name'] == f['name'] for existing in funcs):
            funcs.append(f)
    logs.append(f"[Step 1] 语义搜索 -> {len(semantic_funcs)} 个，去重后共 {len(funcs)} 个")

    # 检查是否需要 Grep Fallback
    max_score = max([f.get('score', 0) for f in funcs], default=0)
    fallback_triggered = False

    if max_score < FALLBACK_THRESHOLD:
        fallback_triggered = True
        logs.append(f"[Step 1] Embedding最高相似度 {max_score:.2f} < {FALLBACK_THRESHOLD}，触发 Grep Fallback")

        entities = extract_entities_from_question(question)
        logs.append(f"[Step 1] 提取实体: {entities}")

        for entity in entities[:2]:
            if '-' in entity or entity.islower():
                module_funcs = search_module_functions(entity, limit=5)
                if module_funcs:
                    logs.append(f"[Step 1] 模块搜索 '{entity}' -> {len(module_funcs)} 个")
                    for fn in module_funcs:
                        if not any(f['name'] == fn['name'] for f in funcs):
                            funcs.append(fn)

            grep_results = grep_codebase(entity, limit=3)
            if grep_results:
                logs.append(f"[Step 1] Grep搜索 '{entity}' -> {len(grep_results)} 个结果")
                new_funcs = convert_grep_to_function_results(grep_results)
                for fn in new_funcs:
                    if not any(f['name'] == fn['name'] for f in funcs):
                        funcs.append(fn)
    else:
        logs.append(f"[Step 1] Embedding最高相似度 {max_score:.2f} >= {FALLBACK_THRESHOLD}，无需 Fallback")

    # Issue 搜索
    issues = search_issues(question, top_k=3)
    logs.append(f"[Step 1] Issue搜索 -> {len(issues)} 个")

    # 去重
    deduped = {}
    for f in funcs:
        name = f.get('name', '')
        if name not in deduped or len(f.get('text', '')) > len(deduped[name].get('text', '')):
            deduped[name] = f
    funcs = list(deduped.values())

    # 补充完整代码
    for f in funcs:
        if len(f.get('text', '')) < 200:
            enrich_function_with_code(f)

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
        "fallback_triggered": fallback_triggered,
        "logs": logs,
    }


def react_decide_detailed(client, question: str, collected: dict, step: int) -> tuple[dict, list]:
    """ReAct决策，带详细日志"""
    logs = []
    funcs = collected.get("functions", [])
    chains = collected.get("call_chains", [])
    issues = collected.get("issues", [])

    expanded = [c['from'] for c in chains]
    func_items = []
    for i, f in enumerate(funcs[:8]):
        source = f.get('source', 'embedding') if f.get('source') else 'embedding'
        score = f.get('score', 0)
        marker = " [已扩展]" if f['name'] in expanded else ""
        func_items.append(f"{i+1}. {f['name']} ({source}, {score:.3f}){marker}")

    context_lines = [f"问题: {question}"]
    if issues:
        context_lines.append(f"\n【相关Issue】(共{len(issues)}个):")
        for i, issue in enumerate(issues[:2]):
            context_lines.append(f"{i+1}. #{issue['number']}: {issue['title'][:50]}")

    if chains:
        context_lines.append(f"\n【已扩展调用链】(共{len(chains)}条):")
        for c in chains[-3:]:
            context_lines.append(f"  - {c['from']}: {c['direction']} (找到{c['found']}个, 新增{c['new']}个)")

    context = '\n'.join(context_lines)

    prompt = load_prompt(
        "react_decide_v2",
        question=question,
        context=context,
        function_count=len(funcs),
        function_list="\n".join(func_items),
        issue_count=len(issues),
        issue_list="\n".join([f"{i+1}. #{issue['number']}: {issue['title'][:80]}" for i, issue in enumerate(issues[:3])]) if issues else "无",
        chain_count=len(chains),
        chain_list="\n".join([f"  - {c['from']}: {c['direction']} (找到{c['found']}个, 新增{c['new']}个)" for c in chains[-3:]]) if chains else "无",
        actions="expand_callers: 扩展调用者\nexpand_callees: 扩展被调用者\nsufficient: 信息充足",
        action_choices="expand_callers|expand_callees|sufficient"
    )

    logs.append(f"\n[Step {step}] ReAct Decision Prompt:")
    logs.append("-" * 60)
    logs.append(prompt[:2000])
    logs.append("-" * 60)

    result = call_llm_json(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=150
    )

    logs.append(f"[Step {step}] LLM Response: {json.dumps(result, ensure_ascii=False, indent=2)}")

    if result is None:
        logs.append(f"[Step {step}] LLM 返回 None，默认 sufficient")
        return {"sufficient": True, "action": "sufficient", "target": "", "thought": ""}, logs

    if step >= 4:
        logs.append(f"[Step {step}] 强制停止（step >= 4）")
        return {"sufficient": True, "action": "sufficient", "target": "", "thought": result.get("thought", "")}, logs

    if result.get("sufficient") or result.get("action") == "sufficient":
        logs.append(f"[Step {step}] LLM 决定 sufficient")
        return {"sufficient": True, "action": "sufficient", "target": "", "thought": result.get("thought", "")}, logs

    action = result.get("action", "")
    target = result.get("target", "")

    # 验证 target
    valid = [f['name'] for f in funcs[:8]]
    if target not in valid:
        for f in funcs[:5]:
            if f['name'] not in expanded:
                target = f['name']
                break
        else:
            target = funcs[0]['name'] if funcs else ""
        logs.append(f"[Step {step}] target '{result.get('target', '')}' 无效，自动选择: {target}")

    valid_actions = ["expand_callers", "expand_callees", "expand_same_file",
                     "expand_same_class", "grep_search", "semantic_search", "sufficient"]
    final_action = action if action in valid_actions else "sufficient"

    return {
        "thought": result.get("thought", ""),
        "sufficient": final_action == "sufficient",
        "action": final_action,
        "target": target
    }, logs


def react_search_detailed(driver, client, question: str) -> dict:
    """ReAct 迭代检索，带详细日志"""
    collected = initial_search(driver, client, question, verbose=True)
    all_logs = collected.pop("logs", [])

    info_gain_history = []

    for step in range(2, MAX_STEPS + 1):
        decision, logs = react_decide_detailed(client, question, collected, step)
        all_logs.extend(logs)

        action = decision.get("action")
        target = decision.get("target", "")

        if decision.get("sufficient") or action == "sufficient":
            all_logs.append(f"[Step {step}] 决策: sufficient，停止检索")
            break

        all_logs.append(f"[Step {step}] 决策: {action}(target={target})")

        new_count = 0
        found = 0

        if action in ["expand_callers", "expand_callees"] and target:
            direction = "callers" if action == "expand_callers" else "callees"
            chain = expand_call_chain(target, direction)
            found = len(chain["functions"])
            new_count = _add_new_funcs(collected, chain["functions"], f'{direction}_of_{target}')

            if direction == "callers" and found == 0:
                grep_results = grep_codebase(target, limit=5)
                if grep_results:
                    grep_funcs = convert_grep_to_function_results(grep_results)
                    grep_funcs = [f for f in grep_funcs if f.get('name') != target]
                    extra = _add_new_funcs(collected, grep_funcs, f'grep_callers_of_{target}')
                    found += len(grep_funcs)
                    new_count += extra
                    if extra:
                        all_logs.append(f"[Step {step}] Grep补充caller: {extra} 个")

            collected["call_chains"].append({
                "from": target, "direction": direction,
                "found": found, "new": new_count
            })
            all_logs.append(f"[Step {step}] {direction}('{target}') -> 找到 {found} 个，新增 {new_count} 个")
            for f in chain["functions"][:5]:
                all_logs.append(f"       - {f.get('name', '')} @ {f.get('file', '')}")

        elif action == "expand_same_file" and target:
            target_func = next((f for f in collected["functions"] if f['name'] == target), None)
            if target_func:
                neighbors = expand_same_file(target, target_func.get('file', ''))
                found = len(neighbors)
                new_count = _add_new_funcs(collected, neighbors, 'file_expansion')
                all_logs.append(f"[Step {step}] same_file('{target}') -> {found} 个")

        elif action == "grep_search" and target:
            grep_results = grep_codebase(target, limit=5)
            found = len(grep_results)
            if grep_results:
                new_funcs = convert_grep_to_function_results(grep_results)
                new_count = _add_new_funcs(collected, new_funcs, 'grep_react')
            all_logs.append(f"[Step {step}] grep('{target}') -> {found} 个")

        collected["steps"].append({
            "step": step, "action": action, "target": target,
            "found": found, "new": new_count
        })

        info_gain_history.append(new_count)

        if step >= 3 and len(info_gain_history) >= 2:
            if all(g <= 1 for g in info_gain_history[-2:]):
                all_logs.append(f"[Step {step}] 递减回报检测触发，停止检索")
                break

    collected["logs"] = all_logs
    return collected


def check_evidence_found(collected: dict, evidence_list: list) -> tuple[bool, list]:
    """检查证据是否被检索到"""
    found_evidence = []
    missing_evidence = []

    func_names = {f['name'] for f in collected.get("functions", [])}
    func_files = {f.get('file', '') for f in collected.get("functions", [])}

    for ev in evidence_list:
        ev_file = ev.get('file', '')
        ev_kind = ev.get('kind', '')

        # 简单检查：证据文件是否在检索到的函数文件中
        if ev_file in func_files:
            found_evidence.append(ev)
        else:
            # 检查是否通过 call_chain 获取到
            found = False
            for f in collected.get("functions", []):
                if f.get('file', '') == ev_file:
                    found = True
                    break
            if found:
                found_evidence.append(ev)
            else:
                missing_evidence.append(ev)

    return len(missing_evidence) == 0, missing_evidence


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--qa-id", required=True, help="QA ID, e.g., audit_001")
    parser.add_argument("--dataset", default="datasets/posthoc_audit_qa.json")
    parser.add_argument("--output", default="results/audit_debug.json")
    args = parser.parse_args()

    # 加载数据集
    with open(args.dataset, encoding="utf-8") as f:
        data = json.load(f)

    item = next((i for i in data["items"] if i["qa_id"] == args.qa_id), None)
    if not item:
        print(f"QA ID {args.qa_id} not found in dataset")
        return

    question = item["question"]
    reference_answer = item["reference_answer"]
    evidence_list = item.get("evidence", [])

    print("=" * 80)
    print(f"QA ID: {args.qa_id}")
    print(f"问题: {question}")
    print(f"证据数量: {len(evidence_list)}")
    for ev in evidence_list:
        print(f"  {ev['id']}: [{ev['kind']:20s}] {ev['file']}:{ev.get('line_range', '')}")
    print("=" * 80)

    driver = get_neo4j_driver()
    client = get_llm_client()

    try:
        # ReAct 检索（带详细日志）
        collected = react_search_detailed(driver, client, question)

        # 打印详细日志
        print("\n" + "=" * 80)
        print("检索详细日志")
        print("=" * 80)
        for log in collected.get("logs", []):
            print(log)

        # 检查证据覆盖
        print("\n" + "=" * 80)
        print("证据覆盖检查")
        print("=" * 80)
        all_found, missing = check_evidence_found(collected, evidence_list)
        print(f"总证据: {len(evidence_list)}")
        print(f"已覆盖: {len(evidence_list) - len(missing)}")
        print(f"缺失: {len(missing)}")
        if missing:
            for ev in missing:
                print(f"  ❌ {ev['id']}: [{ev['kind']}] {ev['file']}")
        else:
            print("  ✅ 所有证据已覆盖")

        # 生成答案
        print("\n" + "=" * 80)
        print("生成答案")
        print("=" * 80)
        answer = generate_answer(question, collected)
        print(answer[:2000])

        # 参考答案
        print("\n" + "=" * 80)
        print("参考答案（前 1000 字符）")
        print("=" * 80)
        print(reference_answer[:1000])

        # 保存结果
        result = {
            "qa_id": args.qa_id,
            "question": question,
            "evidence": evidence_list,
            "evidence_coverage": {
                "total": len(evidence_list),
                "found": len(evidence_list) - len(missing),
                "missing": [{"id": ev["id"], "kind": ev["kind"], "file": ev["file"]} for ev in missing]
            },
            "retrieval": {
                "functions": [{"name": f.get("name"), "file": f.get("file"), "source": f.get("source")}
                             for f in collected.get("functions", [])],
                "steps": collected.get("steps", []),
                "logs": collected.get("logs", []),
            },
            "generated_answer": answer,
            "reference_answer": reference_answer,
        }

        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\n结果已保存到: {args.output}")

    finally:
        driver.close()
        close_neo4j_driver()


if __name__ == "__main__":
    main()
