"""Query-expansion worklist investigation.

For each question we ask a cheap LLM to produce 3-5 English search queries
that capture the technical concepts (modules, symbols, behaviors). We then
retrieve files for every expanded query, merge them with the original Top-K
pool, and run the deterministic multi-hop directory-expansion worklist.

This tests whether LLM-driven query expansion can recover files that the
original Chinese/abstract embedding query misses.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import OPENAI_API_KEY, OPENAI_BASE_URL, get_repo_root
from openai import OpenAI
from src.qa.retrievers.fast_embedding import FastEmbeddingRetriever
from src.qa.candidate_pool import build_function_pool
from src.search.code_reader import read_file_lines

BENCH_PATH = ROOT / "datasets" / "benchmark_hard.json"
RESULT_DIR = ROOT / "results"
REPO_ROOT = Path(get_repo_root())

client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)
QUERY_EXPANSION_MODEL = "gpt-4.1-mini"


def normalize_path(path: str) -> str:
    if path.startswith(str(REPO_ROOT)):
        path = path[len(str(REPO_ROOT)):].lstrip("/")
    return path.lstrip("./").lstrip("/")


def file_coverage(gold_files, visited_files):
    if not gold_files:
        return 1.0
    g = {normalize_path(f) for f in gold_files}
    v = {normalize_path(f) for f in visited_files}
    return len(g & v) / len(g)


def expand_queries(question: str, num_queries: int = 4) -> list[str]:
    """Use a cheap LLM to generate English keyword search queries."""
    prompt = f"""You are helping a code-search system find relevant C/C++ source files in the llama.cpp repository.

The user asks a question in Chinese about code behavior. Generate {num_queries} short English search queries (2-6 words each) that capture the key technical concepts, modules, symbols, or behaviors mentioned.

Question:
{question}

Output ONLY a JSON array of strings, e.g.: ["sycl device switch", "ggml backend init"]
"""
    try:
        resp = client.chat.completions.create(
            model=QUERY_EXPANSION_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=300,
            response_format={"type": "json_object"},
        )
        text = resp.choices[0].message.content.strip()
        data = json.loads(text)
        if isinstance(data, list):
            return [q.strip() for q in data if q.strip()]
        if isinstance(data, dict):
            for k in ("queries", "search_queries", "expanded_queries", "result"):
                if k in data and isinstance(data[k], list):
                    return [q.strip() for q in data[k] if q.strip()]
        return []
    except Exception as e:
        print(f"Query expansion failed: {e}")
        return []


def main(
    lines_per_file: int = 100,
    top_k_files: int = 20,
    top_m_functions: int = 5,
    expand_per_dir: int = 5,
    max_expand_files_per_hop: int = 15,
    max_hops: int = 2,
    num_expanded_queries: int = 4,
    expanded_top_k: int = 5,
):
    with open(BENCH_PATH) as f:
        bench = json.load(f)["items"]

    retriever = FastEmbeddingRetriever()

    dir_files = {}
    for ch in retriever.chunks:
        fp = ch.get("meta", {}).get("file_path", "")
        if not fp:
            continue
        fp = normalize_path(fp)
        d = str(Path(fp).parent)
        dir_files.setdefault(d, set()).add(fp)

    per_item = []
    for item in bench:
        qa_id = item["qa_id"]
        question = item["question"]

        query_emb = retriever.encode_queries([question])
        base_pool = build_function_pool(
            query_emb,
            retriever,
            top_k_files=top_k_files,
            top_m_functions=top_m_functions,
        )

        # Query expansion
        expanded_queries = expand_queries(question, num_expanded_queries)
        expanded_files = []
        if expanded_queries:
            expanded_embs = retriever.encode_queries(expanded_queries)
            seen_files = set()
            for i, eq in enumerate(expanded_queries):
                results = retriever.retrieve(
                    expanded_embs[i : i + 1],
                    top_k=expanded_top_k,
                )
                for r in results:
                    fp = normalize_path(r["metadata"].get("file_path", ""))
                    if fp and fp not in seen_files:
                        seen_files.add(fp)
                        expanded_files.append({"file_path": fp, "query": eq, "score": r["score"]})

        # Merge pools: base first, then expanded files
        seen = set()
        unique_files = []
        for c in base_pool:
            fp = normalize_path(c["file_path"])
            if fp and fp not in seen:
                seen.add(fp)
                unique_files.append(fp)
        for ef in expanded_files:
            fp = ef["file_path"]
            if fp and fp not in seen:
                seen.add(fp)
                unique_files.append(fp)

        visited_files = []
        for fp in unique_files:
            try:
                read_file_lines(fp, 1, lines_per_file)
                visited_files.append(fp)
            except Exception as e:
                print(f"[{qa_id}] failed to read {fp}: {e}")

        # Multi-hop directory expansion
        expansion_added = []
        current_visited = list(visited_files)
        for hop in range(max_hops):
            visited_dirs = {str(Path(v).parent) for v in current_visited}
            hop_expanded = []
            for d in sorted(visited_dirs):
                candidates = dir_files.get(d, set()) - seen
                if not candidates:
                    continue
                results = retriever.retrieve(query_emb, top_k=expand_per_dir, file_filter=candidates)
                for r in results:
                    fp = normalize_path(r["metadata"].get("file_path", ""))
                    if fp and fp not in seen:
                        seen.add(fp)
                        hop_expanded.append(fp)
                        if len(hop_expanded) >= max_expand_files_per_hop:
                            break
                if len(hop_expanded) >= max_expand_files_per_hop:
                    break

            for fp in hop_expanded:
                try:
                    read_file_lines(fp, 1, lines_per_file)
                    visited_files.append(fp)
                    current_visited.append(fp)
                    expansion_added.append(fp)
                except Exception as e:
                    print(f"[{qa_id}] failed to read expanded {fp}: {e}")

        gold_files = [
            ev["file"]
            for ev in item["gold_evidence"]
            if not ev["file"].endswith((".h", ".hpp"))
        ]
        cov = file_coverage(gold_files, visited_files)

        per_item.append({
            "qa_id": qa_id,
            "question": question,
            "expanded_queries": expanded_queries,
            "expanded_files_meta": expanded_files,
            "unique_files": unique_files,
            "expansion_added": expansion_added,
            "visited_files": visited_files,
            "gold_files": gold_files,
            "coverage": cov,
        })

    full = sum(1 for x in per_item if x["coverage"] >= 1.0)
    avg = sum(x["coverage"] for x in per_item) / len(per_item)
    summary = {
        "total": len(per_item),
        "full_coverage": full,
        "full_coverage_rate": full / len(per_item),
        "avg_coverage": avg,
        "lines_per_file": lines_per_file,
        "top_k_files": top_k_files,
        "top_m_functions": top_m_functions,
        "expand_per_dir": expand_per_dir,
        "max_expand_files_per_hop": max_expand_files_per_hop,
        "max_hops": max_hops,
        "num_expanded_queries": num_expanded_queries,
        "expanded_top_k": expanded_top_k,
    }

    out = {"summary": summary, "per_item": per_item}
    out_path = RESULT_DIR / (
        f"qexp_worklist_hard_L{lines_per_file}_K{top_k_files}_Q{num_expanded_queries}_"
        f"E{expand_per_dir}_M{max_expand_files_per_hop}_H{max_hops}.json"
    )
    with open(out_path, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Saved to {out_path}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--lines", type=int, default=100)
    parser.add_argument("--top-k-files", type=int, default=20)
    parser.add_argument("--top-m-functions", type=int, default=5)
    parser.add_argument("--expand-per-dir", type=int, default=5)
    parser.add_argument("--max-expand-files", type=int, default=15)
    parser.add_argument("--max-hops", type=int, default=2)
    parser.add_argument("--num-queries", type=int, default=4)
    parser.add_argument("--expanded-top-k", type=int, default=5)
    args = parser.parse_args()
    main(
        args.lines,
        args.top_k_files,
        args.top_m_functions,
        args.expand_per_dir,
        args.max_expand_files,
        args.max_hops,
        args.num_queries,
        args.expanded_top_k,
    )
