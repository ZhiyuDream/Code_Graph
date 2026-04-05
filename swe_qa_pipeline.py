#!/usr/bin/env python3
"""
SWE-QA Benchmark 适配脚本。

用法：
    # 1. 克隆一个 SWE-QA 项目（如 astropy）
    python swe_qa_pipeline.py clone astropy

    # 2. 构建图谱（解析 Python 代码并写入 Neo4j）
    python swe_qa_pipeline.py build astropy

    # 3. 测试单题
    python swe_qa_pipeline.py query astropy "What is the structure of Astropy's unit system?"

    # 4. 批量评测（输出 RAG 对比）
    python swe_qa_pipeline.py benchmark astropy --limit 10
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

from config import NEO4J_DATABASE, NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD
from neo4j_writer import get_driver
from parsers import create_parser
from graph_builder import build_graph


# SWE-QA 数据集路径
SWE_QA_ROOT = Path(__file__).resolve().parent.parent / "SWE-QA-Bench"
SWE_QA_DATASETS = SWE_QA_ROOT / "SWE-QA-Bench" / "datasets"


def cmd_clone(args):
    """克隆 SWE-QA 项目仓库"""
    repo_name = args.repo
    repos_txt = SWE_QA_ROOT / "repos.txt"
    if not repos_txt.exists():
        print(f"ERROR: repos.txt not found at {repos_txt}")
        return

    # 解析 repos.txt，找到对应的 repo URL 和 commit
    target_prefix = f"github.com/{repo_name}" if "/" not in repo_name else repo_name
    repo_url = None
    commit = None
    with open(repos_txt) as f:
        for line in f:
            if target_prefix in line:
                parts = line.strip().split()
                if len(parts) >= 2:
                    repo_url = parts[0]
                    commit = parts[1]
                    break

    if not repo_url:
        print(f"ERROR: Could not find repo {repo_name} in repos.txt")
        return

    clone_dir = _ROOT / "data" / "swe_repos" / repo_name
    if clone_dir.exists():
        print(f"Repo already cloned at {clone_dir}")
        return

    clone_dir.parent.mkdir(parents=True, exist_ok=True)
    print(f"Cloning {repo_url}...")
    os.system(f"git clone {repo_url} {clone_dir}")
    if commit:
        print(f"Checking out commit {commit[:8]}...")
        os.system(f"cd {clone_dir} && git checkout {commit}")


def cmd_build(args):
    """解析 Python 项目并构建图谱"""
    repo_name = args.repo
    repo_path = _ROOT / "data" / "swe_repos" / repo_name

    if not repo_path.exists():
        print(f"ERROR: Repo not found at {repo_path}")
        print(f"Run: python swe_qa_pipeline.py clone {repo_name}")
        return

    print(f"Building graph for {repo_name} at {repo_path}")

    # 创建 Python 解析器
    parser = create_parser("python")

    # 收集所有 TU
    print("Collecting AST...")
    t0 = time.time()
    tu_results = parser.collect_all_tus(repo_root=repo_path)
    print(f"  Collected {len(tu_results)} files in {time.time()-t0:.1f}s")

    # 统计
    total_funcs = sum(len(r["functions"]) for r in tu_results)
    total_classes = sum(len(r["classes"]) for r in tu_results)
    total_calls = sum(len(r["calls"]) for r in tu_results)
    print(f"  Functions: {total_funcs}, Classes: {total_classes}, Calls: {total_calls}")

    # 构建图
    print("Building graph...")
    t0 = time.time()
    graph = build_graph(tu_results, repo_root=str(repo_path))
    print(f"  Graph built in {time.time()-t0:.1f}s")

    # 写入 Neo4j
    print(f"Writing to Neo4j ({NEO4J_DATABASE})...")
    driver = get_driver()
    driver.verify_connectivity()

    with driver.session(database=NEO4J_DATABASE) as s:
        # 清空旧数据（可选）
        if args.clear:
            print("  Clearing existing nodes...")
            s.run("MATCH (n) DETACH DELETE n")

        # 写入节点（MERGE 避免重复）
        for label, nodes in graph["nodes"].items():
            if not nodes:
                continue
            print(f"  Writing {len(nodes)} {label} nodes...")
            for node in nodes:
                props = {k: v for k, v in node.items() if v is not None}
                s.run(
                    f"MERGE (n:{label} {{id: $id}}) SET n += $props",
                    id=props.get("id"), props=props
                )

        # 写入边
        for rel_type, edges in graph["edges"].items():
            if not edges:
                continue
            print(f"  Writing {len(edges)} {rel_type} edges...")
            for src, tgt, props in edges:
                s.run(
                    f"MATCH (a {{id: $src}}), (b {{id: $tgt}}) MERGE (a)-[r:{rel_type}]->(b)",
                    src=str(src), tgt=str(tgt)
                )

    driver.close()
    print("Done!")


def cmd_query(args):
    """用 Graph-Agent 回答单个问题"""
    from tools.agent_qa import run_agent
    from neo4j_writer import get_driver

    driver = get_driver()
    answer, traj, steps, tokens = run_agent(driver, args.question)
    print(f"Question: {args.question}")
    print(f"Answer: {answer}")
    print(f"Steps: {steps}, Tokens: {tokens}")
    driver.close()


def cmd_benchmark(args):
    """在 SWE-QA 数据集上运行 Graph-Agent vs RAG 对比评测"""
    from tools.agent_qa import run_agent
    from tools.classic_rag import cmd_run as rag_run
    import pandas as pd

    repo_name = args.repo
    dataset_path = SWE_QA_DATASETS / "questions" / f"{repo_name}.jsonl"

    if not dataset_path.exists():
        print(f"ERROR: Dataset not found at {dataset_path}")
        return

    # 加载问题
    questions = []
    with open(dataset_path) as f:
        for line in f:
            questions.append(json.loads(line.strip()))

    if args.limit:
        questions = questions[:args.limit]

    print(f"Benchmarking {len(questions)} questions from {repo_name}...")

    driver = get_driver()
    results = []

    for i, item in enumerate(questions):
        question = item["question"]
        reference = item.get("answer", "")

        print(f"[{i+1}/{len(questions)}] {question[:60]}...", end=" ", flush=True)
        try:
            answer, traj, steps, tokens = run_agent(driver, question)
            results.append({
                "question": question,
                "reference": reference,
                "answer": answer,
                "steps": steps,
            })
            print(f"OK steps={steps}")
        except Exception as e:
            print(f"ERROR: {e}")
            results.append({
                "question": question,
                "reference": reference,
                "answer": "",
                "error": str(e),
            })

    driver.close()

    # 保存结果
    output = _ROOT / "results" / f"swe_qa_{repo_name}_{time.strftime('%Y%m%d_%H%M%S')}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"Results saved to {output}")


def main():
    parser = argparse.ArgumentParser(description="SWE-QA Benchmark 适配脚本")
    sub = parser.add_subparsers(dest="cmd")

    clone_p = sub.add_parser("clone", help="克隆 SWE-QA 项目仓库")
    clone_p.add_argument("repo", help="仓库名（如 astropy）")

    build_p = sub.add_parser("build", help="构建图谱")
    build_p.add_argument("repo", help="仓库名")
    build_p.add_argument("--clear", action="store_true", help="清空旧数据")

    query_p = sub.add_parser("query", help="测试单题")
    query_p.add_argument("repo", help="仓库名")
    query_p.add_argument("question", help="问题")

    bench_p = sub.add_parser("benchmark", help="批量评测")
    bench_p.add_argument("repo", help="仓库名")
    bench_p.add_argument("--limit", type=int, default=0, help="限制题目数量")

    args = parser.parse_args()
    if args.cmd == "clone":
        cmd_clone(args)
    elif args.cmd == "build":
        cmd_build(args)
    elif args.cmd == "query":
        cmd_query(args)
    elif args.cmd == "benchmark":
        cmd_benchmark(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
