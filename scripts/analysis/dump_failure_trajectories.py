"""导出失败题的完整 QA 轨迹为可读 markdown，供人工/模型逐步复盘。

用法: .venv/bin/python scripts/analysis/dump_failure_trajectories.py [results.json] [eval.json] [out_dir]
输出: analysis_output/0809_traces/<qa_id>.md（或指定目录）
"""
import json
import os
import re
import sys

QA = json.load(open(sys.argv[1] if len(sys.argv) > 1 else 'results/qa_earlyfinish_full.json'))
EV = json.load(open(sys.argv[2] if len(sys.argv) > 2 else 'results/eval_earlyfinish_det.json'))
EVM = {e['qa_id']: e for e in EV}
OUT = sys.argv[3] if len(sys.argv) > 3 else 'analysis_output/0809_traces'
os.makedirs(OUT, exist_ok=True)

FAIL_IDS = ['001', '002', '007', '010', '013', '014', '034', '037', '040', '041', '042', '044']
# 命令行第四参数可传逗号分隔的题号后缀覆盖默认清单
if len(sys.argv) > 4:
    FAIL_IDS = sys.argv[4].split(',')
OBS_TRUNC = 700
ANS_TRUNC = 2000


def gold_in_text(text, gold_files):
    hits = [g for g in gold_files if os.path.basename(g) in text or g in text]
    return hits


for suffix in FAIL_IDS:
    qa_id = f'posthoc_public_{suffix}'
    q = next((x for x in QA if x['qa_id'] == qa_id), None)
    e = EVM.get(qa_id)
    if not q or not e:
        print('missing', qa_id)
        continue
    gold = e['gold_files']
    cit = e['eval_citation']
    lines = []
    lines.append(f"# {qa_id} 轨迹复盘\n")
    lines.append(f"**问题**: {q['question']}\n")
    lines.append(f"**类别**: {e.get('category','')}\n")
    lines.append(f"**gold 文件**: {json.dumps(gold, ensure_ascii=False)}\n")
    lines.append(f"**覆盖率**: {cit['coverage_ratio']:.0%} | 引用: {json.dumps(cit['cited_files'], ensure_ascii=False)}")
    lines.append(f" | 漏引: {json.dumps(cit['missing_files'], ensure_ascii=False)}")
    lines.append(f" | 原因: {json.dumps(cit.get('missing_reasons',{}), ensure_ascii=False)}\n")
    lines.append(f"**token**: {json.dumps(q.get('token_usage',{}), ensure_ascii=False)}\n")

    # 初始召回池
    lines.append(f"\n## 初始召回池（{len(q['initial_functions'])} 个候选）\n")
    for i, f in enumerate(q['initial_functions']):
        mark = ' ⭐GOLD' if f['file_path'] in gold else ''
        lines.append(f"{i+1}. `{f['name']}` ({f['file_path']}:{f['start_line']}) score={f.get('score',0):.4f}{mark}")
    gold_in_pool = [f for f in q['initial_functions'] if f['file_path'] in gold]
    lines.append(f"\n池内 gold 文件函数数: {len(gold_in_pool)}\n")

    # 监督记录
    if q.get('supervisor_notes'):
        lines.append("\n## 监督者干预\n")
        for n in q['supervisor_notes']:
            lines.append(f"- step{n.get('step')} [{n.get('direction')}] {n.get('assessment','')}")
            lines.append(f"  - 建议: {n.get('guidance','')} 关键词: {n.get('suggest_keywords','')}")
    if q.get('direction_warnings'):
        lines.append(f"\n方向警告次数: {q['direction_warnings']}")
    if q.get('recall_expansions'):
        lines.append(f"recall 扩展次数: {q['recall_expansions']}")

    # 逐步轨迹
    lines.append(f"\n## 逐步轨迹（{len(q['steps'])} 步）\n")
    for s in q['steps']:
        rej = ' ⛔REJECTED' if s.get('rejected') else ''
        lines.append(f"\n### step {s['step']}: `{s['action']}`{rej}")
        lines.append(f"- thought: {s.get('thought','')}")
        lines.append(f"- input: {json.dumps(s.get('action_input',''), ensure_ascii=False)[:400]}")
        if s.get('reason'):
            lines.append(f"- reason: {s['reason'][:300]}")
        obs = s.get('observation', '') or ''
        ghit = gold_in_text(obs, gold)
        obs_t = obs[:OBS_TRUNC] + ('...[截断]' if len(obs) > OBS_TRUNC else '')
        lines.append(f"- obs({len(obs)}字符){' ⭐含金:'+str(ghit) if ghit else ''}: {obs_t}")
        if s.get('files_accessed'):
            lines.append(f"- files_accessed: {json.dumps(s['files_accessed'], ensure_ascii=False)[:300]}")

    # 收尾
    lines.append(f"\n## 收尾\n")
    lines.append(f"- visited_files: {json.dumps(q['visited_files'], ensure_ascii=False)}")
    vf = q.get('visited_functions', {})
    lines.append(f"- visited_functions({len(vf)}): {json.dumps({k: (v if isinstance(v,str) else str(v)[:80]) for k,v in list(vf.items())[:30]}, ensure_ascii=False)[:800]}")
    fs = q.get('file_status', {})
    if fs:
        lines.append(f"- file_status: {json.dumps(fs, ensure_ascii=False)[:600]}")
    if q.get('backfilled'):
        lines.append(f"- backfilled: {json.dumps(q['backfilled'], ensure_ascii=False)[:300]}")
    if q.get('skipped_unread'):
        lines.append(f"- skipped_unread: {json.dumps(q['skipped_unread'], ensure_ascii=False)[:400]}")
    lines.append(f"\n## 最终答案（截断）\n\n{q['answer'][:ANS_TRUNC]}\n")

    with open(f'{OUT}/{qa_id}.md', 'w') as f:
        f.write('\n'.join(lines))
    print('written', qa_id, len(lines), 'lines')
