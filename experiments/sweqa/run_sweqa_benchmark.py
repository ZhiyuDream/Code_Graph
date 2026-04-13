#!/usr/bin/env python3
"""
SWE-QA 全量评测脚本。
对所有 15 个 SWE-QA 项目运行 Graph-Agent 评测并评分，
结果保存在 experiments/sweqa/ 目录。
"""
import argparse
import json
import os
import sys
import time
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
load_dotenv(_ROOT / ".env")

from config import NEO4J_DATABASE, OPENAI_API_KEY, OPENAI_BASE_URL, LLM_MODEL, EMBEDDING_MODEL
from neo4j_writer import get_driver
from openai import OpenAI

SWE_QA_REPOS_TXT = _ROOT.parent / "SWE-QA-Bench" / "repos.txt"
SWE_QA_QUESTIONS = _ROOT.parent / "SWE-QA-Bench" / "SWE-QA-Bench" / "datasets" / "questions"
EXP_DIR = _ROOT / "experiments" / "sweqa"

# 15 个 SWE-QA 项目
SWE_QA_PROJECTS = [
    "astropy", "django", "flask", "matplotlib", "pylint",
    "pytest", "requests", "scikit-learn", "sphinx", "sqlfluff",
    "sympy", "xarray", "conan", "reflex", "streamlink",
]


@dataclass
class ProjectResult:
    project: str
    status: str  # "success", "error", "skipped"
    n_questions: int = 0
    scored: int = 0
    avg_total: float = 0.0
    avg_correctness: float = 0.0
    avg_completeness: float = 0.0
    avg_relevance: float = 0.0
    avg_clarity: float = 0.0
    avg_reasoning: float = 0.0
    error: str = ""
    latency_s: float = 0.0


def load_repos_txt() -> dict[str, tuple[str, str]]:
    """解析 repos.txt，返回 {project_name: (url, commit)}"""
    repos = {}
    with open(SWE_QA_REPOS_TXT) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            url, commit = parts[0], parts[1]
            # 提取项目名
            parts_url = url.rstrip("/").replace("https://github.com/", "").split("/")
            project = parts_url[-1]
            repos[project] = (url, commit)
    return repos


def clone_repo(project: str, repos: dict[str, tuple[str, str]]) -> bool:
    """克隆单个项目仓库"""
    if project not in repos:
        print(f"  [WARN] {project}: not found in repos.txt")
        return False

    repo_url, commit = repos[project]
    clone_dir = _ROOT / "data" / "swe_repos" / project

    if clone_dir.exists():
        print(f"  {project}: already cloned")
        return True

    print(f"  {project}: cloning {repo_url}...")
    result = subprocess.run(
        ["git", "clone", "--quiet", repo_url, str(clone_dir)],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        print(f"  {project}: clone failed: {result.stderr[:200]}")
        return False

    if commit:
        result = subprocess.run(
            ["git", "checkout", "--quiet", commit],
            cwd=clone_dir, capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            print(f"  {project}: checkout failed: {result.stderr[:200]}")
            return False

    print(f"  {project}: cloned and checked out {commit[:8]}")
    return True


def build_graph(project: str) -> bool:
    """构建项目图谱并写入 Neo4j"""
    repo_path = _ROOT / "data" / "swe_repos" / project
    if not repo_path.exists():
        return False

    # 使用 swe_qa_pipeline.py 的逻辑
    from parsers import create_parser
    from graph_builder import build_graph as _build_graph

    parser = create_parser("python")
    tu_results = parser.collect_all_tus(repo_root=repo_path)
    graph = _build_graph(tu_results, repo_root=str(repo_path))

    # 写入 Neo4j
    driver = get_driver()
    try:
        with driver.session(database=NEO4J_DATABASE) as s:
            # 清空现有数据
            s.run("MATCH (n) DETACH DELETE n")

            for label, nodes in graph["nodes"].items():
                if not nodes:
                    continue
                for node in nodes:
                    props = {k: v for k, v in node.items() if v is not None}
                    s.run(
                        f"MERGE (n:{label} {{id: $id}}) SET n += $props",
                        id=props.get("id"), props=props
                    )

            for rel_type, edges in graph["edges"].items():
                if not edges:
                    continue
                for src, tgt, props in edges:
                    s.run(
                        f"MATCH (a {{id: $src}}), (b {{id: $tgt}}) MERGE (a)-[r:{rel_type}]->(b)",
                        src=str(src), tgt=str(tgt)
                    )
        driver.close()
        return True
    except Exception as e:
        print(f"  {project}: graph build error: {e}")
        driver.close()
        return False


def build_rag_index(project: str) -> bool:
    """构建项目 RAG embedding 索引"""
    driver = get_driver()
    client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)

    try:
        with driver.session(database=NEO4J_DATABASE) as s:
            # 函数
            r = s.run("MATCH (f:Function) RETURN f.name AS name, f.file_path AS file, f.signature AS sig")
            chunks = []
            for rec in r:
                text = f"函数: {rec['name']}\n文件: {rec['file']}\n签名: {rec.get('sig', '')}"
                chunks.append({
                    "id": f"func::{rec['name']}::{rec['file']}",
                    "type": "function",
                    "text": text,
                    "meta": {"name": rec["name"], "file": rec["file"]},
                })
            # 类
            r = s.run("MATCH (c:Class) RETURN c.name AS name, c.file_path AS file")
            for rec in r:
                text = f"类: {rec['name']}\n文件: {rec['file']}"
                chunks.append({
                    "id": f"class::{rec['name']}::{rec['file']}",
                    "type": "class",
                    "text": text,
                    "meta": {"name": rec["name"], "file": rec["file"]},
                })

        if not chunks:
            return False

        # 计算 embeddings
        BATCH = 64
        all_emb = []
        for i in range(0, len(chunks), BATCH):
            batch = [c["text"] for c in chunks[i : i + BATCH]]
            resp = client.embeddings.create(model=EMBEDDING_MODEL, input=batch)
            all_emb.extend([e.embedding for e in resp.data])

        index = {"chunks": chunks, "embeddings": all_emb}
        idx_path = _ROOT / "data" / f"{project}_rag_index.json"
        with open(idx_path, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False)

        driver.close()
        return True
    except Exception as e:
        print(f"  {project}: RAG index build error: {e}")
        driver.close()
        return False


def run_evaluation(project: str) -> bool:
    """运行 Graph-Agent 评测"""
    dataset_path = SWE_QA_QUESTIONS / f"{project}.jsonl"
    if not dataset_path.exists():
        print(f"  {project}: dataset not found")
        return False

    # 加载问题
    questions = []
    with open(dataset_path) as f:
        for line in f:
            questions.append(json.loads(line.strip()))

    os.environ["RAG_INDEX_NAME"] = f"{project}_rag_index.json"

    from tools.agent_qa import run_agent
    driver = get_driver()
    results = []

    for i, item in enumerate(questions):
        q = item["question"]
        gt = item.get("ground_truth", "")
        print(f"    [{i+1}/{len(questions)}] {q[:50]}...", flush=True)
        t0 = time.time()
        try:
            answer, traj, steps, tokens = run_agent(driver, q)
            elapsed = time.time() - t0
            results.append({
                "question": q,
                "final_answer": answer,
                "ground_truth": gt,
                "steps": steps,
                "latency_s": round(elapsed, 2),
            })
        except Exception as e:
            results.append({
                "question": q,
                "final_answer": f"ERROR: {e}",
                "ground_truth": gt,
                "error": str(e),
            })

    driver.close()

    out_path = EXP_DIR / f"{project}_results.jsonl"
    with open(out_path, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"  {project}: {len(results)} questions saved")
    return True


def score_results(project: str) -> Optional[ProjectResult]:
    """对评测结果评分"""
    results_path = EXP_DIR / f"{project}_results.jsonl"
    scored_path = EXP_DIR / f"{project}_scored.jsonl"

    if not results_path.exists():
        return None

    records = []
    with open(results_path, encoding="utf-8") as f:
        for line in f:
            records.append(json.loads(line))

    client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)

    scored = []
    for i, rec in enumerate(records):
        q = rec.get("question", "")
        ref = rec.get("ground_truth", "")
        cand = rec.get("final_answer", "")

        print(f"    [{i+1}/{len(records)}] scoring...", flush=True)
        scores = _score_single(client, q, ref, cand)
        if scores:
            total = sum(scores.values())
            scored.append({**rec, **scores, "total_score": total})

    if not scored:
        return None

    with open(scored_path, "w", encoding="utf-8") as f:
        for r in scored:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # 汇总
    n = len(scored)
    result = ProjectResult(
        project=project,
        status="success",
        n_questions=len(records),
        scored=n,
        avg_total=sum(r["total_score"] for r in scored) / n,
        avg_correctness=sum(r["correctness"] for r in scored) / n,
        avg_completeness=sum(r["completeness"] for r in scored) / n,
        avg_relevance=sum(r["relevance"] for r in scored) / n,
        avg_clarity=sum(r["clarity"] for r in scored) / n,
        avg_reasoning=sum(r["reasoning"] for r in scored) / n,
    )
    return result


def _score_single(client, question: str, reference: str, candidate: str) -> Optional[dict]:
    """LLM 评判单条答案"""
    prompt = f"""You are a professional evaluator. Rate the candidate answer against the reference.

Evaluation Criteria (each scored 1 to 10):
1. Correctness: 10=completely correct, 1=completely wrong
2. Completeness: 10=covers all key points, 1=almost none
3. Relevance: 10=fully focused, 1=mostly irrelevant
4. Clarity: 10=very clear, 1=very confusing
5. Reasoning: 10=logical and well-structured, 1=chaotic

Question:{question}
Reference Answer:{reference}
Candidate Answer:{candidate}

Output ONLY valid JSON: {{"correctness":1-10,"completeness":1-10,"relevance":1-10,"clarity":1-10,"reasoning":1-10}}"""

    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            timeout=60,
        )
        content = resp.choices[0].message.content.strip()
        if content.startswith("```json"):
            content = content[7:]
        if content.endswith("```"):
            content = content[:-3]
        scores = json.loads(content.strip())
        for k in ["correctness", "completeness", "relevance", "clarity", "reasoning"]:
            if k not in scores or not (1 <= scores[k] <= 10):
                return None
        return scores
    except Exception:
        return None


def run_all(projects: list[str], steps: set[str]) -> list[ProjectResult]:
    """运行全流程"""
    repos = load_repos_txt()
    results = []

    for project in projects:
        print(f"\n{'='*60}")
        print(f"Project: {project}")
        print(f"{'='*60}")

        t0 = time.time()
        proj_result = ProjectResult(project=project, status="skipped")

        # 1. Clone
        if "clone" in steps:
            if not clone_repo(project, repos):
                proj_result.status = "error"
                proj_result.error = "clone failed"
                results.append(proj_result)
                continue

        # 2. Build Graph
        if "graph" in steps:
            print(f"  Building graph...")
            if not build_graph(project):
                proj_result.status = "error"
                proj_result.error = "graph build failed"
                results.append(proj_result)
                continue

        # 3. Build RAG Index
        if "rag" in steps:
            print(f"  Building RAG index...")
            if not build_rag_index(project):
                proj_result.status = "error"
                proj_result.error = "RAG index build failed"
                results.append(proj_result)
                continue

        # 4. Run Evaluation
        if "eval" in steps:
            print(f"  Running Graph-Agent evaluation...")
            if not run_evaluation(project):
                proj_result.status = "error"
                proj_result.error = "evaluation failed"
                results.append(proj_result)
                continue

        # 5. Score
        if "score" in steps:
            print(f"  Scoring...")
            scored = score_results(project)
            if scored:
                proj_result = scored
            else:
                proj_result.status = "error"
                proj_result.error = "scoring failed"

        proj_result.latency_s = time.time() - t0
        results.append(proj_result)
        print(f"  Done: {proj_result.status} ({proj_result.latency_s:.0f}s)")

        # 保存中间结果
        _save_summary(results)

    return results


def _save_summary(results: list[ProjectResult]):
    """保存汇总结果"""
    summary_path = EXP_DIR / "summary.json"

    # 汇总所有项目
    successful = [r for r in results if r.status == "success"]
    overall_avg = sum(r.avg_total for r in successful) / len(successful) if successful else 0

    data = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_projects": len(results),
        "successful": len(successful),
        "overall_avg_total": round(overall_avg, 2),
        "results": [
            {
                "project": r.project,
                "status": r.status,
                "n_questions": r.n_questions,
                "scored": r.scored,
                "avg_total": round(r.avg_total, 2),
                "avg_correctness": round(r.avg_correctness, 2),
                "avg_completeness": round(r.avg_completeness, 2),
                "avg_relevance": round(r.avg_relevance, 2),
                "avg_clarity": round(r.avg_clarity, 2),
                "avg_reasoning": round(r.avg_reasoning, 2),
                "error": r.error,
                "latency_s": round(r.latency_s, 1),
            }
            for r in results
        ],
    }

    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"\nSummary saved to {summary_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--projects", nargs="+", default=SWE_QA_PROJECTS, help="指定项目列表")
    parser.add_argument("--steps", nargs="+", default=["clone", "graph", "rag", "eval", "score"],
                        choices=["clone", "graph", "rag", "eval", "score"],
                        help="指定运行的步骤")
    parser.add_argument("--resume", action="store_true", help="跳过已完成步骤（仅运行缺失步骤）")
    args = parser.parse_args()

    EXP_DIR.mkdir(parents=True, exist_ok=True)

    print(f"SWE-QA Benchmark")
    print(f"Projects: {', '.join(args.projects)}")
    print(f"Steps: {', '.join(args.steps)}")
    print()

    results = run_all(args.projects, set(args.steps))

    # 最终汇总
    successful = [r for r in results if r.status == "success"]
    if successful:
        overall = sum(r.avg_total for r in successful) / len(successful)
        print(f"\n{'='*60}")
        print(f"FINAL RESULTS ({len(successful)}/{len(results)} projects)")
        print(f"{'='*60}")
        print(f"{'Project':<20} {'Total':>8} {'Corr':>6} {'Comp':>6} {'Rel':>6} {'Clar':>6} {'Reas':>6}")
        print("-" * 60)
        for r in sorted(successful, key=lambda x: -x.avg_total):
            print(f"{r.project:<20} {r.avg_total:>7.1f} {r.avg_correctness:>5.1f} "
                  f"{r.avg_completeness:>5.1f} {r.avg_relevance:>5.1f} "
                  f"{r.avg_clarity:>5.1f} {r.avg_reasoning:>5.1f}")
        print("-" * 60)
        print(f"{'OVERALL':<20} {overall:>7.1f}")
        print(f"\nResults saved to {EXP_DIR}/")


if __name__ == "__main__":
    main()
