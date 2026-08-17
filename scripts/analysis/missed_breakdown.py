"""逐版本漏引分解：没搜到 / 搜到没读 / 读了没引 / 编造型引用。

对每对 (结果, 评估) 文件，把每个漏引 gold 文件分类：
  C 没搜到  —— 从未进过视野（不在初始池、未出现在任何工具结果/observation）
  A 搜到没读 —— 进过视野（池内/工具结果出现过文件名或 gold 函数名）但从未真正读过
  B 读了没引 —— det 判"读过但答案未引用"
  D 编造引用 —— det 判"答案引用但从未读过"

用法: .venv/bin/python scripts/analysis/missed_breakdown.py
"""
import json
import os
import sys

sys.path.insert(0, 'evals')
from eval_v2 import extract_read_files

PAIRS = [
    ('基线', 'results/qa_earlyfinish_full.json', 'results/eval_qa_earlyfinish_full_det2.json'),
    ('V48', 'results/qa_react_v48_full.json', 'results/eval_react_v48_full.json'),
    ('V49', 'results/qa_react_v49_full.json', 'results/eval_react_v49_full.json'),
    ('V51', 'results/qa_v51_full.json', 'results/eval_v51_full_det.json'),
    ('V52-r1', 'results/qa_v52_full.json', 'results/eval_qa_v52_full_det2.json'),
    ('V52-r2', 'results/qa_v52_full_r2.json', 'results/eval_qa_v52_full_r2_det2.json'),
    ('V53-r1', 'results/qa_v53_full.json', 'results/eval_qa_v53_full_det2.json'),
    ('V53-r2', 'results/qa_v53_full_r2.json', 'results/eval_qa_v53_full_r2_det2.json'),
    ('V56', 'results/qa_v56_full.json', 'results/eval_v56_full_det.json'),
    ('V59-r1', 'results/qa_v59_full.json', 'results/eval_v59_full_det.json'),
    ('V59-r2', 'results/qa_v59_full_r2.json', 'results/eval_v59_full_r2_det.json'),
    ('V62-r1', 'results/qa_v62_full.json', 'results/eval_v62_full_det.json'),
    ('V62-r2', 'results/qa_v62_full_r2.json', 'results/eval_v62_full_r2_det.json'),
    # 子集轮
    ('V50-子集', 'results/qa_v50_smoke.json', 'results/eval_v50_smoke_det.json'),
    ('V54-子集', 'results/qa_v54_smoke.json', 'results/eval_v54_smoke_det.json'),
    ('V55-子集', 'results/qa_v55_smoke.json', 'results/eval_v55_smoke_det.json'),
    ('V58-子集', 'results/qa_v58_smoke.json', 'results/eval_v58_smoke_det.json'),
    ('V60-子集', 'results/qa_v60_smoke.json', 'results/eval_v60_smoke_det.json'),
    ('V61-子集', 'results/qa_v61_smoke.json', 'results/eval_v61_smoke_det.json'),
]


def load_eval(p):
    d = json.load(open(p))
    out = {}
    for e in d:
        det = e.get('eval_citation_det') or e.get('eval_citation') or {}
        out[e['qa_id']] = (det, e.get('gold_files', []))
    return out


def classify(q, gf, reason):
    if '读过但答案未引用' in reason:
        return 'B'
    if '编造型' in reason:
        return 'D'
    # 未读且未引用 → 看进没进过视野
    if any(f['file_path'] == gf for f in q.get('initial_functions', [])):
        return 'A'
    for s in q.get('steps', []):
        if gf in s.get('files_accessed', []):
            return 'A'
    allobs = '\n'.join(s.get('observation') or '' for s in q.get('steps', []))
    if os.path.basename(gf) in allobs:
        return 'A'
    return 'C'


print(f"{'版本':>9} | {'漏引文件数':>5} | {'C 没搜到':>4} {'A 搜到没读':>5} {'B 读了没引':>5} {'D 编造引用':>5}")
for tag, rp, ep in PAIRS:
    if not (os.path.exists(rp) and os.path.exists(ep)):
        continue
    res = {x['qa_id']: x for x in json.load(open(rp))}
    ev = load_eval(ep)
    from collections import Counter
    c = Counter()
    detail = []
    for qid, (det, gold) in ev.items():
        if qid not in res:
            continue
        for gf, reason in (det.get('missing_reasons') or {}).items():
            cls = classify(res[qid], gf, reason)
            c[cls] += 1
            detail.append((qid[-3:], gf, cls))
    total = sum(c.values())
    print(f"{tag:>9} | {total:>5} | {c['C']:>4} {c['A']:>7} {c['B']:>7} {c['D']:>7}")
