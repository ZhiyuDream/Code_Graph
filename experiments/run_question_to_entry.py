#!/usr/bin/env python3
"""
Question → Entry 实验。

目标：在没有 gold evidence 提示的情况下，只根据 Question 找到合理的入口文件。

方法：
- embedding: 用 embedding 相似度搜索
- keyword_grep: LLM 提取关键词，然后 grep
- symbol_grep: LLM 提取可能 symbol，然后 grep
- hypothesis_routes: LLM 生成多个假设方向，分别检索后合并

评估：
- Top-1 / Top-5 / Top-10 Entry Hit Rate（候选中是否包含 gold evidence 文件）

用法:
    python experiments/run_question_to_entry.py \
        --benchmark datasets/benchmark_hard.json \
        --range 0,15 \
        --method embedding \
        -o results/question_to_entry_embedding_0_15.json
"""
import json
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.search.grep_search_v2 import grep_files


class EmbeddingRetriever:
    def __init__(self):
        from src.qa.retrievers.embedding import EmbeddingRetriever as BaseEmbeddingRetriever
        self.retriever = BaseEmbeddingRetriever()

    def search(self, question: str, top_k: int = 10) -> list[str]:
        """按函数 chunk 检索，返回 chunk 对应的文件路径（去重保留 top-k 顺序）。"""
        results = self.retriever.retrieve(question, top_k=top_k)
        files = []
        seen = set()
        for r in results:
            fp = r.metadata.get("file_path", "")
            if fp and fp not in seen:
                files.append(fp)
                seen.add(fp)
        return files

    def search_chunks(self, question: str, top_k: int = 10) -> list[dict]:
        """按函数 chunk 检索，返回 chunk 列表（含文件路径和函数名）。"""
        results = self.retriever.retrieve(question, top_k=top_k)
        chunks = []
        seen = set()
        for r in results:
            fp = r.metadata.get("file_path", "")
            name = r.metadata.get("name", "")
            key = f"{fp}:{name}"
            if key not in seen:
                chunks.append({
                    "file_path": fp,
                    "name": name,
                    "score": r.score,
                    "content": r.content[:500],
                })
                seen.add(key)
        return chunks


class LLMKeywordExtractor:
    def __init__(self):
        from src.qa.investigation.base import LLMClient
        self.llm = LLMClient()

    def extract(self, question: str, mode: str = "keyword") -> list[str]:
        if mode == "keyword":
            prompt = (
                f"从下面的问题中提取 3-5 个最可能用于在代码库中搜索的关键词。"
                f"只返回关键词列表，每行一个，不要解释。\n\n问题：{question}\n\n关键词："
            )
        elif mode == "symbol":
            prompt = (
                f"从下面的问题中提取 2-4 个最可能相关的函数名、类名或全局变量名。"
                f"只返回标识符列表，每行一个，不要解释。如果没有明确符号，返回 NONE。\n\n问题：{question}\n\n符号："
            )
        elif mode == "hypotheses":
            prompt = (
                f"从下面的问题出发，生成 3 个可能的研究方向/假设。"
                f"每个方向用 2-4 个关键词概括。只返回方向列表，每行一个。\n\n问题：{question}\n\n方向："
            )
        else:
            raise ValueError(f"Unknown mode: {mode}")

        text = self.llm.call(prompt)
        items = [line.strip() for line in text.split('\n') if line.strip()]
        if mode == "symbol" and items == ["NONE"]:
            return []
        return items


def load_items(bench_path: Path, range_str: str) -> list[dict]:
    with open(bench_path, "r", encoding="utf-8") as f:
        bench = json.load(f)
    items = bench["items"]

    if "," in range_str:
        start, end = map(int, range_str.split(","))
    else:
        start, end = 0, len(items)

    selected = []
    for idx in range(start, end):
        item = items[idx]
        gold_files = sorted(set(ev["file"] for ev in item.get("gold_evidence", [])))
        selected.append({
            "qa_id": item.get("qa_id", f"q{idx}"),
            "question": item.get("question", ""),
            "gold_files": gold_files,
        })
    return selected


def run_embedding(item: dict, retriever: EmbeddingRetriever, top_k: int) -> dict:
    candidates = retriever.search(item["question"], top_k=top_k)
    return evaluate(item, candidates, "embedding")


def run_keyword_grep(item: dict, extractor: LLMKeywordExtractor, top_k: int) -> dict:
    keywords = extractor.extract(item["question"], mode="keyword")
    candidates = []
    for kw in keywords:
        files = grep_files(kw, limit=20)
        candidates.extend(files)
    candidates = candidates[:top_k]
    return evaluate(item, candidates, "keyword_grep")


def run_symbol_grep(item: dict, extractor: LLMKeywordExtractor, top_k: int) -> dict:
    symbols = extractor.extract(item["question"], mode="symbol")
    candidates = []
    for sym in symbols:
        files = grep_files(sym, limit=20)
        candidates.extend(files)
    candidates = candidates[:top_k]
    return evaluate(item, candidates, "symbol_grep")


def run_hypothesis_routes(item: dict, extractor: LLMKeywordExtractor, top_k: int) -> dict:
    hypotheses = extractor.extract(item["question"], mode="hypotheses")
    candidates = []
    for h in hypotheses:
        # Split hypothesis into keywords and search each
        keywords = [w for w in h.replace(",", " ").split() if len(w) >= 3]
        for kw in keywords[:3]:
            files = grep_files(kw, limit=10)
            candidates.extend(files)
    candidates = candidates[:top_k]
    return evaluate(item, candidates, "hypothesis_routes")


def evaluate(item: dict, candidates: list[str], method: str) -> dict:
    gold_set = set(item["gold_files"])
    norm_candidates = [str(Path(c)) for c in candidates]
    hits = [any(g in c for c in norm_candidates) for g in item["gold_files"]]

    return {
        "qa_id": item["qa_id"],
        "method": method,
        "question": item["question"],
        "gold_files": item["gold_files"],
        "candidates": candidates,
        "top1_hit": any(g in norm_candidates[0] for g in gold_set) if candidates else False,
        "top5_hit": any(any(g in c for g in gold_set) for c in norm_candidates[:5]),
        "top10_hit": any(any(g in c for g in gold_set) for c in norm_candidates[:10]),
        "gold_hit_count": sum(hits),
    }


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--range", default="0,15")
    parser.add_argument("--method", choices=["embedding", "keyword_grep", "symbol_grep", "hypothesis_routes"], required=True)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("-w", "--workers", type=int, default=2)
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args()

    items = load_items(args.benchmark, args.range)
    print(f"加载 {len(items)} 题，method={args.method}, top_k={args.top_k}, workers={args.workers}")

    if args.method == "embedding":
        retriever = EmbeddingRetriever()
        worker_fn = lambda item: run_embedding(item, retriever, args.top_k)
    elif args.method == "keyword_grep":
        extractor = LLMKeywordExtractor()
        worker_fn = lambda item: run_keyword_grep(item, extractor, args.top_k)
    elif args.method == "symbol_grep":
        extractor = LLMKeywordExtractor()
        worker_fn = lambda item: run_symbol_grep(item, extractor, args.top_k)
    elif args.method == "hypothesis_routes":
        extractor = LLMKeywordExtractor()
        worker_fn = lambda item: run_hypothesis_routes(item, extractor, args.top_k)

    results = []
    completed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(worker_fn, item): item for item in items}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            completed += 1
            print(f"  [{completed}/{len(items)}] {result['qa_id']}: top1={result['top1_hit']} top5={result['top5_hit']} top10={result['top10_hit']}")

    results.sort(key=lambda x: x["qa_id"])

    total = len(results)
    top1 = sum(1 for r in results if r["top1_hit"]) / total * 100
    top5 = sum(1 for r in results if r["top5_hit"]) / total * 100
    top10 = sum(1 for r in results if r["top10_hit"]) / total * 100

    print(f"\n{'='*60}")
    print(f"Question → Entry ({args.method}) 结果")
    print(f"{'='*60}")
    print(f"总题数: {total}")
    print(f"Top-1 Hit: {top1:.1f}%")
    print(f"Top-5 Hit: {top5:.1f}%")
    print(f"Top-10 Hit: {top10:.1f}%")

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
