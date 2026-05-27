#!/usr/bin/env python3
"""
跑 llamacpp benchmark v2 (50题) - DeepSeek + 完整文件上下文
记录指标：时延、token、工具调用、正确率、证据检索覆盖率、证据引用准确率
"""
from __future__ import annotations

import os
import sys
import json
import re
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

os.environ['LLM_MODEL'] = 'deepseek-v4-pro'

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

from tools.core import get_neo4j_driver, close_neo4j_driver, generate_answer
from tools.core.llm_client import reset_usage_stats, get_usage_stats
from tools.search.code_reader import read_full_file
from experiments.module_expansion.run_qa_v8_react_ablation import (
    react_search, _collect_full_files, _compute_file_priority
)
from config import NEO4J_DATABASE, REPO_ROOT


def _normalize_path(path: str) -> str:
    """统一路径格式：反斜杠变正斜杠。"""
    return path.replace('\\', '/')


def _get_alt_extensions(path: str) -> set[str]:
    """获取同名不同扩展名的文件路径。"""
    alts = {path}
    if '.' not in path:
        return alts
    base = path.rsplit('.', 1)[0]
    for ext in ['.cpp', '.c', '.h', '.hpp']:
        alts.add(base + ext)
    return alts


def _basename_matches(path1: str, path2: str) -> bool:
    """检查两个路径的 basename（不含扩展名）是否匹配。"""
    import os
    b1 = os.path.basename(path1)
    b2 = os.path.basename(path2)
    # 去掉扩展名比较
    b1_base = b1.rsplit('.', 1)[0] if '.' in b1 else b1
    b2_base = b2.rsplit('.', 1)[0] if '.' in b2 else b2
    return b1_base == b2_base


def parse_evidence_files(evidence_text: str) -> list[str]:
    """从关键证据文本中提取文件路径列表。
    
    改进：
    1. 支持反引号包裹的路径（如 `path/file.cpp:186`）
    2. 支持无反引号的路径（如 E1 src\\file.cpp:2543）
    3. 支持行号范围（如 :87-207）
    4. 统一路径分隔符（反斜杠->正斜杠）
    """
    files = set()
    
    # 模式1: 反引号包裹的路径
    for m in re.findall(r'`([^`]+?)`', evidence_text):
        if ':' in m and ('/' in m or '\\\\' in m):
            fp, line_part = m.rsplit(':', 1)
            if re.match(r'^\d+(-\d+)?$', line_part):
                fp = _normalize_path(fp)
                files.add(fp)
    
    # 模式2: 无反引号的路径（如 "E1 src\\file.cpp:2543 ..."）
    # 匹配形如 "word path/to/file.cpp:line" 或 "path/to/file.cpp:line [tag]"
    for m in re.findall(r'(?:^|[\s；])((?:[\w\-]+[/\\\\])+[\w\-]+\.(?:cpp|c|h|hpp)):(\d+)', evidence_text):
        fp = _normalize_path(m[0])
        files.add(fp)
    
    return sorted(files)


def compute_evidence_coverage(collected: dict, evidence_files: list[str]) -> dict:
    """计算证据检索覆盖率。
    
    改进：
    1. 路径归一化后匹配
    2. 同名不同扩展名（.cpp/.h/.hpp）视为覆盖
    """
    full_files = collected.get("full_files", {})
    funcs = collected.get("functions", [])
    
    # 归一化 full_files 和 func_files 的键
    norm_full_files = {_normalize_path(k): v for k, v in full_files.items()}
    
    func_files = set()
    for fn in funcs:
        fp = _normalize_path(fn.get('file', ''))
        if fp:
            func_files.add(fp)
    
    # 检查 full_files 中命中了多少证据文件（严格匹配，仅路径归一化）
    full_files_hit = set()
    for ef in evidence_files:
        if ef in norm_full_files:
            full_files_hit.add(ef)
    
    # 检查 functions 中命中了多少证据文件（严格匹配，仅路径归一化）
    func_files_hit = set()
    for ef in evidence_files:
        if ef in func_files:
            func_files_hit.add(ef)
    
    return {
        "evidence_files_total": len(evidence_files),
        "evidence_files": evidence_files,
        "full_files_hit": len(full_files_hit),
        "full_files_hit_list": sorted(full_files_hit),
        "func_files_hit": len(func_files_hit),
        "func_files_hit_list": sorted(func_files_hit),
        "full_file_coverage": len(full_files_hit) / len(evidence_files) if evidence_files else 0,
        "func_file_coverage": len(func_files_hit) / len(evidence_files) if evidence_files else 0,
    }


def process_single_v2(driver, row: dict, idx: int, retrievers: set, repo_root: str, max_full_files: int = 10) -> dict:
    """处理单题 v2 benchmark。"""
    if idx % 10 == 0:
        print(f"[{idx}] {row.get('question', 'N/A')[:50]}...")
    
    reset_usage_stats()
    start_time = time.time()
    question = row.get('question', '')
    evidence = row.get('evidence', '')
    
    try:
        trace = {"index": idx, "question": question}
        collected, trace = react_search(driver, question, trace, retrievers, repo_root, 'deepseek-v4-pro', 'deepseek')
        
        # 收集完整文件内容
        collected = _collect_full_files(collected, max_files=max_full_files)
        trace["full_files_count"] = len(collected.get("full_files", {}))
        
        # 计算证据检索覆盖率
        evidence_files = parse_evidence_files(evidence)
        coverage = compute_evidence_coverage(collected, evidence_files)
        trace["evidence_coverage"] = coverage
        
        answer = generate_answer(
            question=question,
            collected=collected,
            max_tokens=8192,
            model='deepseek-v4-pro',
            provider='deepseek',
        )
        latency = time.time() - start_time
        
        # 工具调用统计
        tool_stats = {}
        for step in collected.get("steps", []):
            action = step.get("action", "")
            if action not in tool_stats:
                tool_stats[action] = {"count": 0, "total_found": 0, "total_new": 0}
            tool_stats[action]["count"] += 1
            tool_stats[action]["total_found"] += step.get("found", 0)
            tool_stats[action]["total_new"] += step.get("new", 0)
        
        usage_stats = get_usage_stats()
        
        return {
            "index": idx,
            "id": row.get('id', f'qa_{idx}'),
            "question": question,
            "reference": row.get('answer', ''),
            "evidence": evidence,
            "scoring_criteria": row.get('scoring_criteria', ''),
            "dimension_1": row.get('dimension_1', ''),
            "dimension_2": row.get('dimension_2', ''),
            "generated": answer,
            "router": "V8_ReAct_Agent",
            "retrievers": list(retrievers),
            "retrieval": {
                "function_count": len(collected.get("functions", [])),
                "issue_count": len(collected.get("issues", [])),
                "step_count": len(collected.get("steps", [])),
                "full_files_count": len(collected.get("full_files", {})),
            },
            "trace": trace,
            "latency_s": latency,
            "usage": usage_stats,
            "tool_stats": tool_stats,
            "evidence_coverage": coverage,
        }
    except Exception as e:
        import traceback
        usage_stats = get_usage_stats()
        return {
            "index": idx,
            "id": row.get('id', f'qa_{idx}'),
            "question": question,
            "reference": row.get('answer', ''),
            "evidence": evidence,
            "scoring_criteria": row.get('scoring_criteria', ''),
            "dimension_1": row.get('dimension_1', ''),
            "dimension_2": row.get('dimension_2', ''),
            "generated": f"处理失败: {str(e)}\n{traceback.format_exc()}",
            "router": "V8_ReAct_Agent",
            "retrievers": list(retrievers),
            "retrieval": {
                "function_count": 0,
                "issue_count": 0,
                "step_count": 0,
                "full_files_count": 0,
            },
            "trace": trace if 'trace' in dir() else {},
            "latency_s": time.time() - start_time,
            "usage": usage_stats,
            "tool_stats": {},
            "error": str(e),
        }


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, default="datasets/llamacpp_benchmark_v2.json")
    parser.add_argument("--retrievers", default="embedding,issue,grep,graph")
    parser.add_argument("-o", "--output", type=Path, default="results/v2_deepseek_fullfiles.json")
    parser.add_argument("--max-full-files", type=int, default=10)
    parser.add_argument("-w", "--workers", type=int, default=10)
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条")
    parser.add_argument("--offset", type=int, default=0, help="从第 N 条开始跑")
    args = parser.parse_args()
    
    with open(args.benchmark, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    rows = data.get('questions', [])
    if args.offset > 0:
        rows = rows[args.offset:]
    if args.limit > 0:
        rows = rows[:args.limit]
    retrievers = set(args.retrievers.split(","))
    repo_root = REPO_ROOT or "/root/data/zzy/llama.cpp"
    
    print(f"llamacpp benchmark v2 - DeepSeek")
    print(f"Benchmark: {args.benchmark} ({len(rows)} 题)")
    print(f"检索器: {', '.join(retrievers)}")
    print(f"模型: deepseek-v4-pro (deepseek)")
    print(f"完整文件: 每题最多 {args.max_full_files} 个")
    print(f"并行: {args.workers} workers")
    print()
    
    driver = get_neo4j_driver()
    results = []
    completed = 0
    
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(process_single_v2, driver, row, i, retrievers, repo_root, args.max_full_files): i
            for i, row in enumerate(rows)
        }
        
        for future in as_completed(futures):
            try:
                result = future.result()
                results.append(result)
                completed += 1
                
                if completed % 10 == 0 or completed == len(rows):
                    print(f"  已完成 {completed}/{len(rows)} 题...")
                    sorted_results = sorted(results, key=lambda x: x.get('index', 0))
                    args.output.parent.mkdir(parents=True, exist_ok=True)
                    with open(args.output, 'w', encoding='utf-8') as f:
                        json.dump(sorted_results, f, ensure_ascii=False, indent=2)
                        
            except Exception as e:
                print(f"  处理题目时出错: {e}")
                completed += 1
    
    sorted_results = sorted(results, key=lambda x: x.get('index', 0))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(sorted_results, f, ensure_ascii=False, indent=2)
    
    print(f"\n完成！共处理 {len(results)}/{len(rows)} 题")
    print(f"结果保存至: {args.output}")
    
    # 打印证据覆盖率摘要
    total_full_cov = 0
    total_func_cov = 0
    count = 0
    for r in sorted_results:
        cov = r.get("evidence_coverage", {})
        if cov:
            total_full_cov += cov.get("full_file_coverage", 0)
            total_func_cov += cov.get("func_file_coverage", 0)
            count += 1
    
    if count > 0:
        print(f"\n平均证据文件覆盖率:")
        print(f"  full_files: {total_full_cov/count*100:.1f}%")
        print(f"  func_files: {total_func_cov/count*100:.1f}%")
    
    close_neo4j_driver()


if __name__ == "__main__":
    main()
