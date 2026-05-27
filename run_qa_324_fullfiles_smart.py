#!/usr/bin/env python3
"""
324 题 benchmark 智能 full-files 实验

改进点：
1. LLM 先看函数片段，决定需要看哪些完整文件（不无脑全看）
2. 读取完整文件时，不按字符位置粗暴截断
3. 如果文件太大，改为提取该文件中的完整函数片段（函数级截断）

模块拆分：
- tools/core/full_file_selector.py  —— LLM 决策 + 函数级收集逻辑
- prompts/full_file_decision.txt    —— LLM 决策 prompt

不影响 baseline 的 react_search 流程，只在 generate_answer 前按需调用 full_file_selector。

数据集: datasets/llama_cpp_QA_cleaned.json
模型: deepseek-v4-pro
"""
from __future__ import annotations

import os
import sys
import json
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

os.environ['LLM_MODEL'] = 'deepseek-v4-pro'

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

from tools.core import get_neo4j_driver, close_neo4j_driver, generate_answer
from tools.core.llm_client import reset_usage_stats, get_usage_stats
from tools.core.full_file_selector import collect_full_files_smart
from tools.core.hierarchical_answer_generator import generate_answer_hierarchical
from experiments.module_expansion.run_qa_v8_react_ablation import react_search
from config import NEO4J_DATABASE, REPO_ROOT


def process_single(
    driver,
    row: dict,
    idx: int,
    retrievers: set,
    repo_root: str,
    model: str,
    provider: str,
    max_full_files: int = 10,
    max_tokens_budget: int = 400000,
) -> dict:
    if idx % 20 == 0:
        print(f"[{idx}] {row.get('question', 'N/A')[:50]}...")

    reset_usage_stats()
    start_time = time.time()
    question = row.get('question', '')

    try:
        trace = {"index": idx, "question": question}

        # === 阶段1：baseline 检索流程（完全不变）===
        collected, trace = react_search(
            driver, question, trace, retrievers, repo_root, model, provider
        )

        # === 阶段2：智能 full-files 收集（新增，不影响 baseline）===
        collected = collect_full_files_smart(
            collected,
            question=question,
            model=model,
            provider=provider,
            max_files=max_full_files,
            max_tokens=max_tokens_budget,
        )
        trace["full_files_count"] = len(collected.get("full_files", {}))

        decision = collected.get("full_files_decision", {})
        trace["full_files_decision"] = {
            "need": decision.get("need_full_files", False),
            "reason": decision.get("reason", ""),
            "files_requested": decision.get("files", []),
        }

        # 估算实际注入的字符数
        total_full_chars = sum(
            len(c) for c in collected.get("full_files", {}).values()
        )
        trace["full_files_chars"] = total_full_chars
        trace["full_files_est_tokens"] = int(total_full_chars / 2.5)

        # === 阶段3：分层生成答案（方案2）===
        answer, gen_meta = generate_answer_hierarchical(
            question=question,
            collected=collected,
            generate_fn=generate_answer,
            max_tokens=8192,
            model=model,
            provider=provider,
            max_supplement_files=3,
            max_supplement_tokens=150000,
        )
        trace["hierarchical_meta"] = gen_meta

        usage = get_usage_stats()
        latency = time.time() - start_time

        return {
            "index": idx,
            "id": row.get('id', f'qa_{idx}'),
            "question": question,
            "reference": row.get('answer', ''),
            "generated": answer,
            "router": "V8_DeepSeek_SmartFullFiles",
            "retrievers": list(retrievers),
            "retrieval": {
                "function_count": len(collected.get("functions", [])),
                "issue_count": len(collected.get("issues", [])),
                "step_count": len(trace.get("react_steps", [])),
                "full_files_count": trace.get("full_files_count", 0),
                "full_files_chars": trace.get("full_files_chars", 0),
                "full_files_est_tokens": trace.get("full_files_est_tokens", 0),
                "llm_decided_need_full": decision.get("need_full_files", False),
                "llm_decided_files": decision.get("files", []),
            },
            "trace": trace,
            "latency_s": latency,
            "usage": usage,
            "tool_stats": {
                "initial_search": 1,
                "expand_callers": sum(
                    1 for s in trace.get("react_steps", [])
                    if s.get("action") == "expand_callers"
                ),
                "expand_callees": sum(
                    1 for s in trace.get("react_steps", [])
                    if s.get("action") == "expand_callees"
                ),
            },
        }

    except Exception as e:
        import traceback
        return {
            "index": idx,
            "id": row.get('id', f'qa_{idx}'),
            "question": question,
            "reference": row.get('answer', ''),
            "generated": f"处理失败: {str(e)}\n{traceback.format_exc()}",
            "router": "V8_DeepSeek_SmartFullFiles",
            "error": str(e),
            "latency_s": time.time() - start_time,
        }


def main():
    # 加载 benchmark
    with open('datasets/llama_cpp_QA_cleaned.json', 'r', encoding='utf-8') as f:
        benchmark = json.load(f)
    questions = benchmark['questions']

    print("=" * 60)
    print("324 题智能 full-files 实验")
    print("=" * 60)
    print(f"数据集: datasets/llama_cpp_QA_cleaned.json ({len(questions)} 题)")
    print(f"模型: deepseek-v4-pro")
    print(f"策略: LLM 按需决策 + 函数级截断")
    print(f"  - LLM 先看函数片段，决定需要看哪些完整文件")
    print(f"  - 超大文件不按字符截断，改为提取完整函数片段")
    print(f"  - Token 预算: 400K")
    print(f"并行: 30 workers")
    print(f"估算耗时: ~{len(questions) * 60 / 30 / 60:.0f} 分钟")
    print("=" * 60)
    print()

    driver = get_neo4j_driver()
    retrievers = {"embedding", "issue", "grep", "graph"}
    repo_root = str(REPO_ROOT)
    model = 'deepseek-v4-pro'
    provider = 'deepseek'

    results = []
    completed = 0

    with ThreadPoolExecutor(max_workers=30) as executor:
        futures = {
            executor.submit(
                process_single, driver, q, i, retrievers, repo_root,
                model, provider, 10, 400000,
            ): i
            for i, q in enumerate(questions)
        }

        for future in as_completed(futures):
            try:
                result = future.result()
                results.append(result)
                completed += 1

                if completed % 20 == 0 or completed == len(questions):
                    print(f"  已完成 {completed}/{len(questions)} 题...")
                    sorted_results = sorted(results, key=lambda x: x.get('index', 0))
                    with open(
                        'results/v8_deepseek_324_hierarchical_fullfiles.json',
                        'w', encoding='utf-8',
                    ) as f:
                        json.dump(sorted_results, f, ensure_ascii=False, indent=2)

            except Exception as e:
                print(f"  处理题目时出错: {e}")
                completed += 1

    sorted_results = sorted(results, key=lambda x: x.get('index', 0))
    with open('results/v8_deepseek_324_hierarchical_fullfiles.json', 'w', encoding='utf-8') as f:
        json.dump(sorted_results, f, ensure_ascii=False, indent=2)

    # 统计
    success = sum(1 for r in results if 'error' not in r)
    avg_latency = sum(r.get('latency_s', 0) for r in results) / len(results)
    avg_full_files = sum(
        r.get('retrieval', {}).get('full_files_count', 0) for r in results
    ) / len(results)
    avg_est_tokens = sum(
        r.get('retrieval', {}).get('full_files_est_tokens', 0) for r in results
    ) / len(results)
    need_full_ratio = sum(
        1 for r in results
        if r.get('retrieval', {}).get('llm_decided_need_full', False)
    ) / len(results)

    # 分层生成统计
    phase3_ratio = sum(
        1 for r in results
        if r.get('trace', {}).get('hierarchical_meta', {}).get('phase3_used', False)
    ) / len(results)
    avg_supplement_files = sum(
        len(r.get('trace', {}).get('hierarchical_meta', {}).get('supplement_files', []))
        for r in results
    ) / len(results)

    print(f"\n{'=' * 60}")
    print("实验完成")
    print(f"{'=' * 60}")
    print(f"成功处理: {success}/{len(results)} 题")
    print(f"平均时延: {avg_latency:.1f}s")
    print(f"LLM 决定需要 full-files 的比例: {need_full_ratio * 100:.1f}%")
    print(f"平均完整文件数: {avg_full_files:.1f}")
    print(f"平均注入 tokens: {avg_est_tokens:.0f}")
    print(f"进入阶段3（补充生成）的比例: {phase3_ratio * 100:.1f}%")
    print(f"阶段3平均补充文件数: {avg_supplement_files:.1f}")
    print(f"结果保存至: results/v8_deepseek_324_hierarchical_fullfiles.json")

    close_neo4j_driver()


if __name__ == "__main__":
    main()
