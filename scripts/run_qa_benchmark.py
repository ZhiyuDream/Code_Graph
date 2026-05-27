#!/usr/bin/env python3
"""
可插拔检索的 QA Benchmark 运行脚本。

支持组合使用 grep / embedding / graph 检索器，对 llama_cpp_issue_benchmark 跑 QA，
生成答案后可用 eval_with_model.py 做评判。

用法：
  # 全部检索器
  python run_qa_benchmark.py --retrievers grep,embedding,graph -o results/qa_all.json

  # 仅 graph
  python run_qa_benchmark.py --retrievers graph -o results/qa_graph_only.json

  # 仅 embedding
  python run_qa_benchmark.py --retrievers embedding -o results/qa_emb_only.json

  # 然后评判
  python eval_with_model.py -i results/qa_all.json -o results/qa_all_eval.json -m gpt-4o-mini -w 20
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

from config import (
    NEO4J_DATABASE,
    OPENAI_API_KEY,
    OPENAI_BASE_URL,
    LLM_MODEL,
    REPO_ROOT,
)
from neo4j_writer import get_driver
from openai import OpenAI
from src.core.prompt_loader import load_prompt

# 检索器
from qa_framework.retrievers.grep_retriever import GrepRetriever
from qa_framework.retrievers.graph_retriever import GraphRetriever
from qa_framework.retrievers.embedding_retriever import EmbeddingRetriever

BENCHMARK_PATH = _ROOT / "datasets" / "llama_cpp_issue_benchmark.with_answers.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)



def load_benchmark(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("questions", [])


def build_context(results: list) -> str:
    """把检索结果拼成 context。"""
    parts = []
    for i, r in enumerate(results, 1):
        parts.append(f"[{i}] [{r.source}] {r.type}: {r.id}\n{r.content}")
    return "\n\n---\n\n".join(parts)


def generate_answer(client, question: str, context: str) -> tuple[str, dict]:
    prompt = load_prompt("qa_generation", context=context, question=question)
    try:
        kwargs = {
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "timeout": 120,
        }
        if LLM_MODEL.startswith("gpt-5") or LLM_MODEL.startswith("o1") or LLM_MODEL.startswith("o3"):
            kwargs["max_completion_tokens"] = 1000
        else:
            kwargs["max_tokens"] = 1000
        resp = client.chat.completions.create(**kwargs)
        answer = (resp.choices[0].message.content or "").strip()
        usage = resp.usage.model_dump() if resp.usage else {}
        return answer, usage
    except Exception as e:
        logger.error("LLM generation failed: %s", e)
        return f"[生成失败: {e}]", {}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--retrievers", default="graph", help="启用的检索器，逗号分隔: grep,embedding,graph")
    parser.add_argument("-o", "--output", type=Path, required=True, help="输出结果 JSON")
    parser.add_argument("--benchmark", type=Path, default=None, help="Benchmark JSON 路径（默认: datasets/llama_cpp_issue_benchmark.with_answers.json）")
    parser.add_argument("--top-k", type=int, default=5, help="每个检索器返回 top-k")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条（0=全部）")
    parser.add_argument("--build-emb-index", action="store_true", help="先构建 embedding 索引")
    parser.add_argument("-w", "--workers", type=int, default=20, help="并行 worker 数")
    args = parser.parse_args()

    if not OPENAI_API_KEY:
        print("ERROR: OPENAI_API_KEY 未设置", file=sys.stderr)
        sys.exit(1)

    benchmark_path = args.benchmark or BENCHMARK_PATH
    if not benchmark_path.exists():
        print(f"ERROR: Benchmark 文件不存在: {benchmark_path}", file=sys.stderr)
        sys.exit(1)

    # 加载 benchmark
    questions = load_benchmark(benchmark_path)
    if args.limit > 0:
        questions = questions[:args.limit]
    print(f"Benchmark: {len(questions)} 条 issue")

    # Neo4j driver
    driver = get_driver()
    driver.verify_connectivity()

    repo_root = REPO_ROOT or "/root/data/zzy/llama.cpp"

    # 初始化检索器
    enabled = set(args.retrievers.split(","))
    retrievers = []

    if "grep" in enabled:
        retrievers.append(GrepRetriever(repo_root=repo_root, enabled=True))
        print("✓ Grep retriever enabled")

    if "graph" in enabled:
        retrievers.append(GraphRetriever(
            driver=driver,
            repo_root=repo_root,
            database=NEO4J_DATABASE,
            enabled=True,
            expand_calls_depth=1,
        ))
        print("✓ Graph retriever enabled")

    if "embedding" in enabled:
        emb = EmbeddingRetriever(
            driver=driver,
            repo_root=repo_root,
            database=NEO4J_DATABASE,
            enabled=True,
        )
        if args.build_emb_index:
            print("构建 embedding 索引...")
            emb.build_index(force=True)
        else:
            emb.build_index(force=False)  # 如果不存在会自动构建
        retrievers.append(emb)
        print("✓ Embedding retriever enabled")

    if not retrievers:
        print("ERROR: 没有启用任何检索器", file=sys.stderr)
        sys.exit(1)

    # LLM client
    client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)

    # 跑 QA（并行）
    def _process_one(idx_q):
        i, q = idx_q
        issue_id = q.get("id", f"issue_{q.get('issue_number', i)}")
        question_text = q.get("question", "")
        reference = q.get("answer", "")

        t0 = time.time()

        # 检索
        all_results = []
        for ret in retrievers:
            try:
                r = ret.retrieve(question_text, top_k=args.top_k)
                all_results.extend(r)
            except Exception as e:
                logger.warning("%s retrieval failed: %s", ret.name, e)

        # 按 source 去重（优先 graph，其次 embedding，最后 grep）
        seen_ids = set()
        deduped = []
        source_priority = {"graph": 0, "embedding": 1, "grep": 2}
        all_results.sort(key=lambda x: source_priority.get(x.source, 99))
        for r in all_results:
            if r.id not in seen_ids:
                seen_ids.add(r.id)
                deduped.append(r)

        context = build_context(deduped[:args.top_k * 2])

        # 生成答案
        answer, usage = generate_answer(client, question_text, context)
        latency = round(time.time() - t0, 2)

        return {
            "index": i,
            "id": issue_id,
            "issue_number": q.get("issue_number"),
            "difficulty": q.get("difficulty"),
            "question_type": q.get("question_type"),
            "具体问题": question_text,
            "参考答案": reference,
            "生成答案": answer,
            "检索结果": [r.to_dict() for r in deduped[:args.top_k * 2]],
            "延迟_s": latency,
            "token_usage": usage,
            "错误": None if not answer.startswith("[生成失败") else answer,
        }

    results = [None] * len(questions)
    completed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(_process_one, (i, q)): i for i, q in enumerate(questions, 1)}
        for future in as_completed(futures):
            i = futures[future]
            try:
                res = future.result()
                results[i - 1] = res
                completed += 1
                if completed % 10 == 0 or completed == len(questions):
                    print(f"[{completed}/{len(questions)}] 已完成...")
            except Exception as e:
                logger.error("处理 issue %d 失败: %s", i, e)
                results[i - 1] = {
                    "index": i,
                    "id": f"issue_{i}",
                    "错误": str(e),
                    "生成答案": "[处理失败]",
                }

    driver.close()

    # 保存
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"\n完成！结果保存至: {args.output}")
    print(f"共 {len(results)} 条，下一步运行:")
    print(f"  python tools/eval_with_model.py -i {args.output} -o {args.output.with_suffix('.eval.json')} -m gpt-4o-mini -w 20")


if __name__ == "__main__":
    main()
