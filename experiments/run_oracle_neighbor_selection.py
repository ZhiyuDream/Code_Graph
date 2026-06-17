#!/usr/bin/env python3
"""
Oracle Neighbor Selection Experiment.

For each question, augment the Top-K candidate pool with:
- The gold symbol itself (oracle)
- Direct callers of gold symbol
- Direct callees of gold symbol

Then let LLM select multiple symbols and measure coverage.

This tests whether the selection gap is due to missing structural context.
"""
import json
import re
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from config import get_repo_root
from src.qa.retrievers.embedding import EmbeddingRetriever
from src.qa.investigation.base import BaseInvestigator, LLMClient, load_prompt
from src.core.neo4j_client import run_cypher
from src.search.code_reader import read_full_file
from src.search.grep_search_v2 import grep_files


def normalize_path(path: str) -> str:
    repo_root = str(get_repo_root())
    if path.startswith(repo_root):
        path = path[len(repo_root):].lstrip('/')
    return path.lstrip('./')


def file_priority(path: str) -> int:
    p = path.lower()
    if "/src/" in p or p.startswith("src/"):
        return 100
    if "/common/" in p or p.startswith("common/"):
        return 90
    if "/ggml/src/" in p or p.startswith("ggml/src/"):
        return 85
    if "/include/" in p or p.startswith("include/"):
        return 50
    if "/tests/" in p or p.startswith("tests/"):
        return 20
    if "/examples/" in p or p.startswith("examples/"):
        return 10
    if "/tools/" in p or p.startswith("tools/"):
        return 30
    return 40


def select_definition_file(files: list[str]) -> str | None:
    if not files:
        return None
    return max(files, key=lambda f: (file_priority(f), -len(f)))


def expand_from_function(entry_function: str, repo_path: str,
                         callees_top_k: int, files_per_symbol: int) -> set[str]:
    from collections import Counter
    all_occurrences = grep_files(entry_function, repo_path, limit=100)
    visited = {normalize_path(f) for f in all_occurrences}
    definition_file = select_definition_file(list(visited))
    if definition_file:
        try:
            content = read_full_file(definition_file)
        except Exception:
            content = ""
        if content:
            investigator = BaseInvestigator.__new__(BaseInvestigator)
            symbols = investigator.extract_symbols(content)
            counter = Counter(symbols)
            callee_symbols = [sym for sym, _ in counter.most_common(callees_top_k)
                              if sym != entry_function]
            for sym in callee_symbols:
                files = grep_files(sym, repo_path, limit=50)
                selected = {normalize_path(f) for f in files[:files_per_symbol]}
                visited.update(selected)
    return visited


def get_gold_neighbors(symbol: str, limit: int = 5) -> tuple[list[str], list[str]]:
    """Return (callers, callees) of a given function from Neo4j."""
    callers = []
    callees = []
    try:
        r_callers = run_cypher("""
            MATCH (caller:Function)-[:CALLS]->(callee:Function {name: $name})
            RETURN caller.name AS name LIMIT $limit
        """, {"name": symbol, "limit": limit})
        callers = [row["name"] for row in r_callers if row.get("name")]
    except Exception:
        pass
    try:
        r_callees = run_cypher("""
            MATCH (caller:Function {name: $name})-[:CALLS]->(callee:Function)
            RETURN callee.name AS name LIMIT $limit
        """, {"name": symbol, "limit": limit})
        callees = [row["name"] for row in r_callees if row.get("name")]
    except Exception:
        pass
    return callers, callees


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
        gold_files = sorted(set(
            ev["file"] for ev in item.get("gold_evidence", [])
            if not ev["file"].endswith((".h", ".hpp"))
        ))
        selected.append({
            "qa_id": item.get("qa_id", f"q{idx}"),
            "question": item.get("question", ""),
            "gold_files": gold_files,
        })
    return selected


def select_multiple_symbols(question: str, candidates: list[dict], llm: LLMClient) -> list[int]:
    prompt_template = load_prompt("select_multiple_symbols")
    summaries = []
    for i, c in enumerate(candidates, 1):
        tag = ""
        if c.get("is_gold"):
            tag = " [GOLD]"
        elif c.get("is_neighbor"):
            tag = " [neighbor]"
        summaries.append(f"[{i}] {c['file_path']}::{c['name']}(){tag}")
    prompt = prompt_template.format(
        question=question,
        file_summaries="\n\n".join(summaries),
    )
    text = llm.call(prompt).strip()

    try:
        start = text.find('[')
        end = text.rfind(']')
        if start >= 0 and end > start:
            arr = json.loads(text[start:end+1])
            if isinstance(arr, list):
                valid = []
                seen = set()
                for idx in arr:
                    if isinstance(idx, int) and 1 <= idx <= len(candidates) and idx not in seen:
                        valid.append(idx)
                        seen.add(idx)
                return valid
    except Exception:
        pass

    valid = []
    seen = set()
    for m in re.finditer(r'\d+', text):
        idx = int(m.group(0))
        if 1 <= idx <= len(candidates) and idx not in seen:
            valid.append(idx)
            seen.add(idx)
    return valid[:5]


def run_item(item: dict, oracle_item: dict, retriever: EmbeddingRetriever,
             llm: LLMClient, repo_path: str, top_k: int,
             top_n: int, files_per_symbol: int) -> dict:
    results = retriever.retrieve(item["question"], top_k=top_k)
    candidates = []
    seen = set()
    for r in results:
        meta = r.metadata
        key = (meta.get("file_path", ""), meta.get("name", ""))
        if key[0] and key not in seen:
            seen.add(key)
            candidates.append({
                "file_path": meta.get("file_path", ""),
                "name": meta.get("name", ""),
                "is_gold": False,
                "is_neighbor": False,
            })

    # Augment with gold symbol and its neighbors
    gold_symbols = oracle_item.get("gold_symbols", [])
    best_symbol = oracle_item.get("best_symbol")
    for sym in gold_symbols:
        if sym and sym not in seen:
            seen.add(sym)
            # try to find file for this symbol
            try:
                file_result = run_cypher("""
                    MATCH (f:Function {name: $name})
                    RETURN f.file_path AS file LIMIT 1
                """, {"name": sym})
                file_path = file_result[0]["file"] if file_result else ""
            except Exception:
                file_path = ""
            candidates.append({
                "file_path": file_path or "unknown",
                "name": sym,
                "is_gold": True,
                "is_neighbor": False,
            })

    if best_symbol:
        callers, callees = get_gold_neighbors(best_symbol, limit=5)
        for sym in callers + callees:
            if sym and sym not in seen:
                seen.add(sym)
                try:
                    file_result = run_cypher("""
                        MATCH (f:Function {name: $name})
                        RETURN f.file_path AS file LIMIT 1
                    """, {"name": sym})
                    file_path = file_result[0]["file"] if file_result else ""
                except Exception:
                    file_path = ""
                candidates.append({
                    "file_path": file_path or "unknown",
                    "name": sym,
                    "is_gold": False,
                    "is_neighbor": True,
                })

    if not candidates:
        return {"qa_id": item["qa_id"], "error": "no candidates"}

    selected_indices = select_multiple_symbols(item["question"], candidates, llm)
    selected = [candidates[i-1] for i in selected_indices if 1 <= i <= len(candidates)]

    union_visited = set()
    for c in selected:
        visited = expand_from_function(c["name"], repo_path, top_n, files_per_symbol)
        union_visited.update(visited)

    gold_norm = {normalize_path(g) for g in item["gold_files"]}
    union_norm = {normalize_path(v) for v in union_visited}
    coverage = len(gold_norm & union_norm) / len(gold_norm) if gold_norm else 1.0

    selected_gold = [c["name"] for c in selected if c["is_gold"]]
    selected_neighbors = [c["name"] for c in selected if c["is_neighbor"]]

    return {
        "qa_id": item["qa_id"],
        "gold_files": item["gold_files"],
        "num_candidates": len(candidates),
        "selected_count": len(selected),
        "selected_symbols": [c["name"] for c in selected],
        "selected_gold": selected_gold,
        "selected_neighbors": selected_neighbors,
        "union_visited_count": len(union_visited),
        "coverage": coverage,
    }


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, default=Path("datasets/benchmark_hard.json"))
    parser.add_argument("--oracle", type=Path, default=Path("results/gold_function_expansion_oracle_0_15.json"))
    parser.add_argument("--range", default="0,15")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument("--files-per-symbol", type=int, default=3)
    parser.add_argument("-w", "--workers", type=int, default=2)
    parser.add_argument("-o", "--output", type=Path, default=Path("results/oracle_neighbor_selection_0_15.json"))
    args = parser.parse_args()

    items = load_items(args.benchmark, args.range)
    with open(args.oracle, "r", encoding="utf-8") as f:
        oracle = json.load(f)
    oracle_by_qa = {r["qa_id"]: r for r in oracle["per_item"]}

    retriever = EmbeddingRetriever()
    llm = LLMClient()
    repo_path = str(get_repo_root())

    results = []
    completed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(run_item, item, oracle_by_qa[item["qa_id"]], retriever,
                            llm, repo_path, args.top_k, args.top_n, args.files_per_symbol): item
            for item in items
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            completed += 1
            print(f"  [{completed}/{len(items)}] {result['qa_id']}: "
                  f"selected={result.get('selected_count')} "
                  f"gold_selected={len(result.get('selected_gold', []))} "
                  f"cov={result.get('coverage', 0)*100:.0f}%")

    results.sort(key=lambda x: x["qa_id"])
    total = len([r for r in results if "error" not in r])
    avg_coverage = sum(r["coverage"] for r in results if "error" not in r) / total
    full = sum(1 for r in results if "error" not in r and r["coverage"] >= 1.0)
    gold_selected_count = sum(1 for r in results if "error" not in r and r.get("selected_gold"))

    print(f"\n{'='*60}")
    print("Oracle Neighbor Selection (Augmented Pool)")
    print(f"{'='*60}")
    print(f"总题数: {total}")
    print(f"平均 Coverage: {avg_coverage*100:.1f}%")
    print(f"完全覆盖: {full}/{total} ({full/total*100:.1f}%)")
    print(f"至少选中一个 gold symbol: {gold_selected_count}/{total}")

    output = {
        "summary": {
            "total": total,
            "avg_coverage": avg_coverage,
            "full_correct": full,
            "gold_selected_count": gold_selected_count,
        },
        "per_item": results,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
