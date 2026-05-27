import os, sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))
os.environ['LLM_MODEL'] = 'deepseek-v4-pro'

from experiments.module_expansion.run_qa_v8_deepseek_react_fixed import *

# Monkey-patch react_decide to use v2 prompt
_orig_react_decide = react_decide

def react_decide_v2(question, collected, step):
    funcs = collected.get("functions", [])
    chains = collected.get("call_chains", [])
    expanded = [c['from'] for c in chains]
    func_lines = []
    for i, f in enumerate(funcs[:8]):
        source = f.get('source', 'embedding') if f.get('source') else 'embedding'
        score = f.get('score', 0)
        marker = " [已扩展]" if f['name'] in expanded else ""
        func_lines.append(f"{i+1}. {f['name']} ({source}, {score:.3f}){marker}")
    if len(funcs) > 8:
        func_lines.append(f"   ... 还有 {len(funcs)-8} 个函数")
    issue_lines = []
    if collected.get("issues"):
        for i, issue in enumerate(collected["issues"][:2]):
            issue_lines.append(f"{i+1}. #{issue['number']}: {issue['title'][:50]}")
    else:
        issue_lines.append("无")
    chain_lines = []
    if chains:
        for c in chains[-3:]:
            chain_lines.append(f"  - {c['from']}: {c['direction']} (找到{c['found']}个, 新增{c['new']}个)")
    else:
        chain_lines.append("无")
    actions_text = format_actions_for_prompt()
    action_names = get_action_names()
    action_choices = "|".join(action_names)
    prompt = load_prompt(
        "react_decide_v2",
        question=question,
        function_count=len(funcs),
        function_list="\n".join(func_lines),
        issue_count=len(collected.get("issues", [])),
        issue_list="\n".join(issue_lines),
        chain_count=len(chains),
        chain_list="\n".join(chain_lines),
        actions=actions_text,
        action_choices=action_choices,
    )
    result = call_llm_json(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=300,
        timeout=600,
        model='deepseek-v4-pro',
        provider='deepseek'
    )
    decision_trace = {
        "step": step,
        "prompt": prompt[:500],
        "raw_response": result,
    }
    if result is None:
        decision_trace["fallback"] = "json_parse_failed"
        return {"sufficient": True, "action": "sufficient", "target": ""}, decision_trace
    if step >= 4:
        decision_trace["fallback"] = "max_steps_reached"
        return {"sufficient": True, "action": "sufficient", "target": ""}, decision_trace
    if result.get("sufficient") or result.get("action") == "sufficient":
        return {"sufficient": True, "action": "sufficient", "target": ""}, decision_trace
    action = result.get("action", "")
    target = result.get("target", "")
    return {"sufficient": False, "action": action, "target": target}, decision_trace

# Replace the function
import experiments.module_expansion.run_qa_v8_deepseek_react_fixed as v8_mod
v8_mod.react_decide = react_decide_v2

# Now run main
if __name__ == "__main__":
    rows = []
    with open('results/qav2_test_cleaned.csv', 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    rows = rows[:324]
    print(f"V8 + V2 Prompt 快速验证: {len(rows)} 题")
    print(f"模型: deepseek-v4-pro")
    print(f"并行: 50 workers")
    print(f"估算耗时: ~{len(rows) * 60 / 50 / 60:.0f} 分钟")
    print()
    driver = get_neo4j_driver()
    from src.core.llm_client import get_llm_client
    client = get_llm_client(provider='deepseek')
    results = []
    completed = 0
    with ThreadPoolExecutor(max_workers=50) as executor:
        futures = {
            executor.submit(v8_mod.process_single, driver, client, row, i): i
            for i, row in enumerate(rows)
        }
        for future in as_completed(futures):
            try:
                result = future.result()
                results.append(result)
                completed += 1
                if completed % 20 == 0 or completed == len(rows):
                    print(f"  已完成 {completed}/{len(rows)} 题...")
                    sorted_results = sorted(results, key=lambda x: x.get('index', 0))
                    with open('results/v8_quick_324_v2.json', 'w', encoding='utf-8') as f:
                        json.dump(sorted_results, f, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f"  处理题目时出错: {e}")
                completed += 1
    sorted_results = sorted(results, key=lambda x: x.get('index', 0))
    with open('results/v8_quick_324_v2.json', 'w', encoding='utf-8') as f:
        json.dump(sorted_results, f, ensure_ascii=False, indent=2)
    print(f"\n完成！共处理 {len(results)}/{len(rows)} 题")
    print(f"结果保存至: results/v8_quick_324_v2.json")
    close_neo4j_driver()
