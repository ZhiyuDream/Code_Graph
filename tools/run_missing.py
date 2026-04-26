#!/usr/bin/env python3
"""补跑丢失的题目，合并到原结果文件中"""
import sys
import json
import csv
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from tools.core import get_neo4j_driver, close_neo4j_driver
from experiments.module_expansion.run_qa_v8_with_grep_v2 import process_single

# 读取已有结果
d = json.load(open("results/v8_grep_v2_360.json", "r", encoding="utf-8"))
existing_indices = set(x['index'] for x in d)
print(f"已有结果: {len(d)} 题，索引: {sorted(existing_indices)[:10]}...{sorted(existing_indices)[-5:]}")

# 读取CSV，找出丢失的题目
rows = []
with open("results/qav2_test.csv", "r", encoding="utf-8") as f:
    reader = csv.DictReader(f)
    for i, row in enumerate(reader):
        if i not in existing_indices:
            rows.append((i, row))

print(f"需要补跑: {len(rows)} 题")
if not rows:
    print("无需补跑")
    sys.exit(0)

# 连接
driver = get_neo4j_driver()
from tools.core.llm_client import get_llm_client
client = get_llm_client()

# 并行补跑
new_results = []
completed = 0

with ThreadPoolExecutor(max_workers=20) as executor:
    futures = {
        executor.submit(process_single, driver, client, row, idx, True): idx
        for idx, row in rows
    }
    
    for future in as_completed(futures):
        try:
            result = future.result()
            new_results.append(result)
            completed += 1
            if completed % 10 == 0:
                print(f"  已完成 {completed}/{len(rows)} 题...")
        except Exception as e:
            idx = futures[future]
            print(f"  [{idx}] 出错: {e}")
            completed += 1

# 合并并保存
all_results = d + new_results
all_results = sorted(all_results, key=lambda x: x.get('index', 0))

with open("results/v8_grep_v2_360.json", "w", encoding="utf-8") as f:
    json.dump(all_results, f, ensure_ascii=False, indent=2)

print(f"\n合并完成！总计: {len(all_results)}/360 题")

close_neo4j_driver()
