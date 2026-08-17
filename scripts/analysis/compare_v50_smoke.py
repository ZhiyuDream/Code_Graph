"""V50 冒烟对比：12 道失败题 基线(earlyfinish) vs V50。

用法: .venv/bin/python scripts/analysis/compare_v50_smoke.py [v50_results.json] [v50_eval.json]
"""
import json
import sys

V50 = sys.argv[1] if len(sys.argv) > 1 else 'results/qa_v50_smoke.json'
V50EV = sys.argv[2] if len(sys.argv) > 2 else 'results/eval_v50_smoke_det.json'

base_qa = {x['qa_id']: x for x in json.load(open('results/qa_earlyfinish_full.json'))}
base_ev = {e['qa_id']: e for e in json.load(open('results/eval_earlyfinish_det.json'))}
v50_qa = {x['qa_id']: x for x in json.load(open(V50))}
v50_ev = {e['qa_id']: e for e in json.load(open(V50EV))}


def waste(q):
    steps = q['steps']
    rej = sum(1 for s in steps if s.get('rejected'))
    mem = sum(1 for s in steps if not s.get('rejected') and '记忆命中' in (s.get('observation') or '')
              and s['action'] in ('read_function', 'read_lines'))
    replay = sum(1 for s in steps if '【回放' in (s.get('observation') or ''))
    empty = sum(1 for s in steps if not s.get('rejected') and s['action'] == 'search_symbol'
                and ('没有找到' in (s.get('observation') or '') or '搜索符号为空' in (s.get('observation') or '')))
    scan = sum(1 for s in steps if s['action'] == 'scan_directory')
    phantom = sum(len(n.get('phantom_keywords', [])) for n in q.get('supervisor_notes', []))
    return dict(n=len(steps), rej=rej, mem=mem, replay=replay, empty=empty, scan=scan, phantom=phantom)


print(f"{'题':>4} | {'基线det':>6} {'V50det':>6} | {'基线浪费(拒/读/搜)':>16} {'V50(拒/回放/搜)':>16} {'scan':>4} {'幻觉词':>4}")
tb = dict(rej=0, mem=0, empty=0); tv = dict(rej=0, replay=0, empty=0, scan=0, phantom=0)
base_sum = v50_sum = 0.0
for qid in sorted(base_qa):
    if qid not in v50_qa:
        continue
    b, v = base_qa[qid], v50_qa[qid]
    be, ve = base_ev[qid]['eval_citation'], v50_ev[qid]['eval_citation']
    wb, wv = waste(b), waste(v)
    base_sum += be['coverage_ratio']; v50_sum += ve['coverage_ratio']
    for k in ('rej', 'mem', 'empty'): tb[k] += wb[k]
    for k in ('rej', 'replay', 'empty', 'scan', 'phantom'): tv[k] += wv[k]
    delta = ve['coverage_ratio'] - be['coverage_ratio']
    flag = '▲' if delta > 0 else '▼' if delta < 0 else '='
    print(f"{qid[-3:]:>4} | {be['coverage_ratio']:>6.0%} {ve['coverage_ratio']:>6.0%} {flag} | "
          f"{wb['rej']:>4}/{wb['mem']:>3}/{wb['empty']:>3} {wv['rej']:>10}/{wv['replay']:>3}/{wv['empty']:>3} "
          f"{wv['scan']:>4} {wv['phantom']:>4}")
n = len(v50_qa)
print(f"\n平均 det: 基线 {base_sum/n:.1%} → V50 {v50_sum/n:.1%}")
print(f"浪费合计: 基线 拒{tb['rej']}/重读{tb['mem']}/空搜{tb['empty']} → V50 拒{tv['rej']}/回放{tv['replay']}/空搜{tv['empty']}")
print(f"V50 scan_directory 调用 {tv['scan']} 次, 监督幻觉词被过滤 {tv['phantom']} 个")
