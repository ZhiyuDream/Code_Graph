"""Symbol-graph expansion worklist.

After the deterministic multi-hop directory expansion, additionally expand via
symbol-level call/callee links: extract prominent symbols from visited files,
find their callees, and read those new files. This tests whether graph
navigation can recover files that pure embedding retrieval misses.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import get_repo_root
from src.qa.retrievers.fast_embedding import FastEmbeddingRetriever
from src.qa.candidate_pool import build_function_pool
from src.search.code_reader import read_file_lines, read_full_file
from src.search.call_chain import get_callees, get_callers

BENCH_PATH = ROOT / "datasets" / "benchmark_hard.json"
RESULT_DIR = ROOT / "results"
REPO_ROOT = Path(get_repo_root())


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


def extract_symbols(content: str, max_symbols: int = 20) -> list[str]:
    """Extract likely function names from code content."""
    symbols = []
    # Function definitions
    pattern = re.compile(
        r"(?:^|\n)\s*(?:static\s+|inline\s+|virtual\s+|constexpr\s+)?"
        r"(?:[\w:\*\&<>\[\]]+\s+)+"
        r"(\w+)\s*\([^)]*\)\s*(?:const\s*)?(?:noexcept\s*)?(?:override\s*)?\s*(?:try\s*)?\{",
        re.MULTILINE,
    )
    for m in pattern.finditer(content):
        symbols.append(m.group(1))
    # Frequency-based top symbols
    from collections import Counter
    counts = Counter(symbols)
    stopwords = {
        "if", "while", "for", "switch", "return", "else", "catch", "try",
        "class", "struct", "namespace", "using", "template", "public",
        "private", "protected", "default", "delete", "override", "final",
        "const", "static", "inline", "virtual", "explicit", "operator",
        "true", "false", "nullptr", "NULL", "void", "int", "bool", "size_t",
        "char", "float", "double", "long", "short", "unsigned", "signed",
        "auto", "decltype", "typename", "noexcept", "constexpr", "mutable",
        "volatile", "extern", "friend", "typedef", "union", "enum", "goto",
        "case", "break", "continue", "throw", "new", "delete",
    }
    filtered = [(s, c) for s, c in counts.items() if s not in stopwords and len(s) >= 2]
    filtered.sort(key=lambda x: -x[1])
    return [s for s, _ in filtered[:max_symbols]]


def main(
    lines_per_file: int = 100,
    top_k_files: int = 20,
    top_m_functions: int = 5,
    expand_per_dir: int = 5,
    max_expand_files_per_hop: int = 15,
    max_hops: int = 2,
    symbol_expand_top: int = 5,
    max_symbol_files: int = 20,
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
        pool = build_function_pool(
            query_emb,
            retriever,
            top_k_files=top_k_files,
            top_m_functions=top_m_functions,
        )

        seen = set()
        unique_files = []
        for c in pool:
            fp = normalize_path(c["file_path"])
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
                except Exception as e:
                    print(f"[{qa_id}] failed to read expanded {fp}: {e}")

        # Symbol-graph expansion
        symbol_files_added = []
        symbols_to_expand = []
        for fp in visited_files:
            try:
                content = read_full_file(fp)
                symbols_to_expand.extend(extract_symbols(content, max_symbols=symbol_expand_top))
            except Exception:
                continue

        # Deduplicate and keep top symbols by frequency
        from collections import Counter
        sym_counts = Counter(symbols_to_expand)
        top_symbols = [s for s, _ in sym_counts.most_common(symbol_expand_top)]

        new_files = set()
        for sym in top_symbols:
            try:
                callees = get_callees(sym, limit=10)
                callers = get_callers(sym, limit=10)
                for c in callees + callers:
                    f = c.get("file", "")
                    if f:
                        f = normalize_path(f)
                        if f and f not in seen:
                            new_files.add(f)
            except Exception:
                continue
            if len(new_files) >= max_symbol_files:
                break

        for fp in new_files:
            try:
                read_file_lines(fp, 1, lines_per_file)
                visited_files.append(fp)
                symbol_files_added.append(fp)
                seen.add(fp)
            except Exception as e:
                print(f"[{qa_id}] failed to read symbol-expanded {fp}: {e}")

        gold_files = [
            ev["file"]
            for ev in item["gold_evidence"]
            if not ev["file"].endswith((".h", ".hpp"))
        ]
        cov = file_coverage(gold_files, visited_files)

        per_item.append({
            "qa_id": qa_id,
            "question": question,
            "pool_size": len(pool),
            "unique_files": unique_files,
            "expanded_files": [],
            "symbol_files_added": symbol_files_added,
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
        "symbol_expand_top": symbol_expand_top,
        "max_symbol_files": max_symbol_files,
    }

    out = {"summary": summary, "per_item": per_item}
    out_path = RESULT_DIR / (
        f"symgraph_worklist_hard_L{lines_per_file}_K{top_k_files}_"
        f"E{expand_per_dir}_M{max_expand_files_per_hop}_H{max_hops}_S{symbol_expand_top}.json"
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
    parser.add_argument("--symbol-expand-top", type=int, default=5)
    parser.add_argument("--max-symbol-files", type=int, default=20)
    args = parser.parse_args()
    main(
        args.lines,
        args.top_k_files,
        args.top_m_functions,
        args.expand_per_dir,
        args.max_expand_files,
        args.max_hops,
        args.symbol_expand_top,
        args.max_symbol_files,
    )
