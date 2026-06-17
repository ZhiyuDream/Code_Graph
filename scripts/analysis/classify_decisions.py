#!/usr/bin/env python3
"""
对 trajectory 中每一步决策进行分类，统计 Symbol Follow / Topic Follow / Guess Follow 的占比与成功率。

用法:
    python scripts/analysis/classify_decisions.py results/trajectory_merged_0_15.json
"""
import json
import re
import sys
from pathlib import Path


GUESS_KEYWORDS = ["可能", "应该", "猜测", "估计", "试试", "尝试", "或许", "大概", "也许",
                  "might", "maybe", "probably", "guess", "try", "possibly", "should", "could",
                  "假设", "假定", "不妨"]
TOPIC_KEYWORDS = ["模块", "目录", "主题", "backend", "frontend", "注册", "采样", "模板", "裁剪",
                  "module", "directory", "topic", "registration", "sampling", "template", "trim",
                  "调用方", "实现", "声明"]


def extract_symbols(text: str) -> list[str]:
    """从文本中提取候选符号（函数名/类名/变量名）。"""
    # Match identifiers with optional namespace, including template parameters
    pattern = re.compile(r'[A-Za-z_][A-Za-z0-9_:<>,\[\]]*')
    tokens = pattern.findall(text)
    # Filter out common English words and short tokens
    stopwords = {"the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
                 "have", "has", "had", "do", "does", "did", "will", "would", "could",
                 "should", "may", "might", "must", "shall", "can", "need", "dare",
                 "ought", "used", "to", "of", "in", "for", "on", "with", "at", "by",
                 "from", "as", "into", "through", "during", "before", "after", "above",
                 "below", "between", "under", "and", "but", "or", "yet", "so", "if",
                 "because", "although", "though", "while", "where", "when", "that",
                 "which", "who", "whom", "whose", "what", "this", "these", "those",
                 "such", "file", "path", "code", "function", "class", "struct"}
    results = []
    for t in tokens:
        t = t.strip("<>,[]")
        if len(t) >= 3 and t.lower() not in stopwords:
            results.append(t)
    return results


def classify_step(step: dict) -> str:
    """对单步决策分类。"""
    impact = step.get("decision_impact", "")
    target = step.get("next_search_target", "")
    action = step.get("next_action", "")
    evidence = step.get("new_evidence", "")

    combined = impact + " " + target + " " + evidence

    # Extract symbols
    symbols = extract_symbols(combined)

    # Symbol Follow: decision explicitly follows a concrete symbol
    # Heuristic: if impact mentions "转向/查看/追/定位 + symbol" or next_search_target is symbol-like
    symbol_indicators = ["转向", "转到", "追", "定位", "查看", "检查", "调用", "实现",
                         "turn to", "move to", "look at", "check", "locate", "follow"]
    has_symbol_indicator = any(kw in impact for kw in symbol_indicators)

    if symbols and has_symbol_indicator:
        # Check if the action file relates to the symbol
        return "symbol"

    if looks_like_symbol(target):
        return "symbol"

    # Topic Follow: based on module/theme/abstract concept
    if any(kw in combined for kw in TOPIC_KEYWORDS):
        return "topic"

    # Guess Follow: uncertainty or no clear target
    if any(kw in impact for kw in GUESS_KEYWORDS):
        return "guess"

    # Default: if no concrete symbol and no topic, it's a guess
    return "guess"


def looks_like_symbol(text: str) -> bool:
    if not text:
        return False
    text = text.strip()
    return bool(re.match(r'^[A-Za-z_][A-Za-z0-9_:]*$', text)) and len(text) >= 3


def analyze(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        merged = json.load(f)

    type_counts = {"symbol": 0, "topic": 0, "guess": 0}
    type_success = {"symbol": 0, "topic": 0, "guess": 0}

    for r in merged:
        gold_files = set(r["gold_files"])
        visited_before = set()

        for step in r.get("entry_trajectory", []):
            step_type = classify_step(step)
            type_counts[step_type] += 1

            action = step.get("next_action", "")
            if action.startswith("read_file:"):
                target_file = action[len("read_file:"):]
                if target_file in gold_files and target_file not in visited_before:
                    type_success[step_type] += 1
                visited_before.add(target_file)

    total = sum(type_counts.values())
    print(f"共分析 {len(merged)} 题，{total} 步决策\n")
    print(f"{'类型':<15} {'数量':>8} {'占比':>8} {'命中 gold':>10} {'成功率':>8}")
    print("-" * 55)
    for t in ["symbol", "topic", "guess"]:
        count = type_counts[t]
        ratio = count / total * 100 if total else 0
        success = type_success[t]
        rate = success / count * 100 if count else 0
        print(f"{t:<15} {count:>8} {ratio:>7.1f}% {success:>10} {rate:>7.1f}%")

    print("\n逐题决策分类:")
    for r in merged:
        print(f"\n{r['qa_id']} (coverage={r['entry_coverage']*100:.0f}%):")
        for step in r.get("entry_trajectory", []):
            t = classify_step(step)
            target = step.get("next_search_target", "")[:50]
            action = step.get("next_action", "")
            print(f"  Step {step['step']}: [{t}] {target} -> {action}")


if __name__ == "__main__":
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("results/trajectory_merged_0_15.json")
    analyze(path)
