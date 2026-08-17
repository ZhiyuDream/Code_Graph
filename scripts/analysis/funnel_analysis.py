"""失败边界漏斗分析：进入正确文件之后，Agent 在哪一步断掉？

对每道 det<100% 的题、每个 gold 文件，计算：
  pool      gold 文件是否在初始召回池
  seen      gold 文件是否出现在任何工具结果（files_accessed/observation）
  listed    是否对 gold 文件执行过 list_functions 或对其目录 scan_directory
  func_seen gold 证据行所属函数名是否出现在任何 observation
  read      gold 文件是否被真正读过（eval 口径 extract_read_files）
  relevant  gold 文件内是否有函数被标记相关
  cited     gold 文件是否被 det 判定引用

gold 证据行：从 benchmark reference 里的 `file:line` 提取。
函数名解析：data/qa_embedding_index.json 的 chunks meta（name/file/start/end）。

用法: .venv/bin/python scripts/analysis/funnel_analysis.py [results.json] [eval.json]
"""
import json
import os
import re
import sys

sys.path.insert(0, 'evals')
from eval_v2 import extract_read_files

RES = sys.argv[1] if len(sys.argv) > 1 else 'results/qa_v52_full.json'
EV = sys.argv[2] if len(sys.argv) > 2 else 'results/eval_v52_full_det.json'

results = {x['qa_id']: x for x in json.load(open(RES))}
evals = {e['qa_id']: e for e in json.load(open(EV))}
bench = json.load(open('datasets/hard_benchmark.json'))
bench_items = bench['items'] if isinstance(bench, dict) and 'items' in bench else bench
ref = {it['qa_id']: it for it in bench_items}

# 函数索引：file -> [(start, end, name)]
idx = json.load(open('data/qa_embedding_index.json'))
chunks = idx['chunks'] if isinstance(idx, dict) and 'chunks' in idx else idx
file_funcs = {}
for ch in chunks:
    meta = ch.get('meta') or {}
    fp, name = meta.get('file_path'), meta.get('name')
    s, e = meta.get('start_line'), meta.get('end_line')
    if fp and name and s and e:
        file_funcs.setdefault(fp, []).append((s, e, name))


def gold_func_at(fp, line):
    best = None
    for s, e, name in file_funcs.get(fp, []):
        if s <= line <= e and (best is None or (e - s) < (best[1] - best[0])):
            best = (s, e, name)
    return best[2] if best else None


rows = []
for qid, e in sorted(evals.items()):
    cov = e['eval_citation']['coverage_ratio']
    if cov >= 0.999 or qid not in results:
        continue
    q = results[qid]
    item = ref.get(qid, {})
    reference = item.get('reference_answer') or item.get('reference') or e.get('reference', '')
    gold_lines = re.findall(r'(ggml/[\w./-]+|src/[\w./-]+|common/[\w./-]+|tools/[\w./-]+|examples/[\w./-]+|vendor/[\w./-]+):(\d+)', reference)
    read_files = extract_read_files(q)
    all_obs = '\n'.join(s.get('observation') or '' for s in q['steps'])
    accessed = set()
    for s in q['steps']:
        accessed.update(s.get('files_accessed', []))
    listed_files = set()
    scanned_dirs = set()
    for s in q['steps']:
        if s['action'] == 'list_functions':
            fp = (s['action_input'].get('file_path') or '').split(':')[0]
            listed_files.add(fp)
        if s['action'] == 'scan_directory':
            scanned_dirs.add((s['action_input'].get('directory') or '').rstrip('/'))
    pool_files = {f['file_path'] for f in q['initial_functions']}
    pool_names = {f['name'] for f in q['initial_functions']}

    for gf in e['gold_files']:
        if gf in e['eval_citation']['cited_files']:
            continue
        gfuncs = sorted({gold_func_at(fp, int(ln)) for fp, ln in gold_lines if fp == gf} - {None})
        stage = {
            'pool': gf in pool_files or any(n in pool_names for n in gfuncs),
            'seen': gf in accessed or os.path.basename(gf) in all_obs,
            'listed': gf in listed_files or os.path.dirname(gf) in scanned_dirs,
            'func_seen': any(g in all_obs for g in gfuncs) if gfuncs else None,
            'read': gf in read_files,
            'relevant': any(k.split('@', 1)[-1] == gf and v is True for k, v in q['visited_functions'].items()),
        }
        rows.append((qid[-3:], cov, gf, gfuncs, stage))

print(f"{'题':>4} {'det':>4} {'gold 文件':<46} {'pool':>4} {'seen':>4} {'list':>4} {'fn↑':>4} {'read':>4} {'rel':>4}  gold函数")
for qid3, cov, gf, gfuncs, st in rows:
    fs = lambda b: ('✓' if b else '✗') if b is not None else '-'
    print(f"{qid3:>4} {cov:>4.0%} {gf:<46} {fs(st['pool']):>4} {fs(st['seen']):>4} {fs(st['listed']):>4} "
          f"{fs(st['func_seen']):>4} {fs(st['read']):>4} {fs(st['relevant']):>4}  {', '.join(gfuncs[:3])}")

# 汇总：每个阶段的存活率
print('\n=== 漏斗汇总（每个 gold 漏引文件一个样本，共 %d 个）===' % len(rows))
stages = ['pool', 'seen', 'listed', 'func_seen', 'read', 'relevant']
for st in stages:
    n = sum(1 for _, _, _, _, s in rows if s[st])
    valid = sum(1 for _, _, _, _, s in rows if s[st] is not None)
    print(f"{st:>10}: {n}/{valid}")
# 关键断点：第一个失败阶段
print('\n=== 首个断点分布 ===')
from collections import Counter
first_fail = Counter()
for _, _, _, _, st in rows:
    for st_name in stages:
        if st[st_name] is False:
            first_fail[st_name] += 1
            break
    else:
        first_fail['全部通过(但没引)'] += 1
for k, v in first_fail.most_common():
    print(f"  {k}: {v}")
