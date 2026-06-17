#!/usr/bin/env python3
"""
实验 1：检查 Agent 每步识别的 symbol 与 gold files 的匹配程度。

对 merged trajectory 中每一步：
1. 提取 next_search_target / decision_impact / new_evidence 中的符号
2. 检查这些符号是否出现在 gold files 的内容中
3. 统计 symbol 命中 gold 的比例

用法:
    python scripts/analysis/symbol_quality_check.py results/trajectory_merged_0_15.json
"""
import json
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))


def extract_symbols(text: str) -> set[str]:
    """从文本中提取候选符号。"""
    pattern = re.compile(r'[A-Za-z_][A-Za-z0-9_:]*')
    tokens = pattern.findall(text)
    stopwords = {"the", "this", "that", "file", "path", "code", "function", "class",
                 "struct", "common", "src", "ggml", "llama", "test", "example"}
    return {t for t in tokens if len(t) >= 3 and t.lower() not in stopwords}


def load_file_content(file_path: str) -> str:
    """读取文件内容用于匹配符号。"""
    from src.search.code_reader import read_full_file
    try:
        return read_full_file(file_path)
    except Exception:
        return ""


def analyze(result_path: Path):
    with open(result_path, "r", encoding="utf-8") as f:
        merged = json.load(f)

    total_symbols = 0
    symbols_in_gold = 0
    symbols_in_any_visited = 0
    step_level_stats = []

    for r in merged:
        qa_id = r["qa_id"]
        gold_files = r["gold_files"]
        gold_content = "\n".join(load_file_content(f) for f in gold_files)

        for step in r.get("entry_trajectory", []):
            text = " ".join([
                step.get("new_evidence", ""),
                step.get("decision_impact", ""),
                step.get("next_search_target", ""),
                step.get("next_action", ""),
            ])
            symbols = extract_symbols(text)
            if not symbols:
                continue

            total_symbols += len(symbols)
            in_gold = sum(1 for s in symbols if s in gold_content)
            symbols_in_gold += in_gold

            step_level_stats.append({
                "qa_id": qa_id,
                "step": step.get("step"),
                "symbols": list(symbols),
                "in_gold_count": in_gold,
                "total_count": len(symbols),
                "hit_rate": in_gold / len(symbols) if symbols else 0,
            })

    print(f"共分析 {len(merged)} 题，{len(step_level_stats)} 步包含符号")
    print(f"总 symbol 提及次数: {total_symbols}")
    print(f"出现在 gold files 中的次数: {symbols_in_gold}")
    print(f"Symbol-Gold 命中率: {symbols_in_gold/total_symbols*100:.1f}%\n")

    print("逐题 Symbol-Gold 命中率:")
    for r in merged:
        qa_id = r["qa_id"]
        gold_files = r["gold_files"]
        gold_content = "\n".join(load_file_content(f) for f in gold_files)
        steps = r.get("entry_trajectory", [])

        total = 0
        hits = 0
        for step in steps:
            text = " ".join([
                step.get("new_evidence", ""),
                step.get("decision_impact", ""),
                step.get("next_search_target", ""),
                step.get("next_action", ""),
            ])
            symbols = extract_symbols(text)
            total += len(symbols)
            hits += sum(1 for s in symbols if s in gold_content)

        rate = hits / total * 100 if total else 0
        print(f"{qa_id}: {rate:.1f}% ({hits}/{total}) coverage={r['entry_coverage']*100:.0f}%")


if __name__ == "__main__":
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("results/trajectory_merged_0_15.json")
    analyze(path)
