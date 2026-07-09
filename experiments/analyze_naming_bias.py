#!/usr/bin/env python3
"""
Analyze naming bias in selection failures.

Hypothesis: LLM systematically prefers verb/behavior-describing function names
over noun/API/anchor function names.

Categories:
  - behavior_verb: starts with a verb implying action (apply, verify, detect,
    get, set, init, register, parse, build, calculate, trim, prune, update,
    create, destroy, process, analyze, compare, handle, postprocess, download,
    list, segmentize)
  - module_anchor: starts with a module/entity prefix (llama_model_, llama_kv_,
    common_, ggml_backend_, ggml_sycl_, cpu_)
  - test_utility: starts with test_ or contains util/helper
  - other: none of the above

A function can be both behavior_verb and module_anchor (e.g., cpu_get_num_physical_cores).
In such cases we count it in both categories.
"""
import json
import re
import sys
from collections import Counter
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

BEHAVIOR_VERBS = {
    "apply", "verify", "detect", "get", "set", "init", "register", "parse",
    "build", "calculate", "trim", "prune", "update", "create", "destroy",
    "process", "analyze", "compare", "handle", "postprocess", "download",
    "list", "segmentize", "sample", "free", "load", "save", "copy",
    "count", "find", "search", "evaluate", "run", "execute", "perform",
}

MODULE_PREFIXES = [
    "llama_model_", "llama_kv_", "llama_sampler_", "llama_",
    "common_", "ggml_backend_", "ggml_sycl_", "ggml_", "cpu_",
]


def classify_name(name: str) -> dict:
    name_lower = name.lower()
    # strip namespace/scope like common_peg_parser_builder::build
    short = name_lower.split("::")[-1]
    tokens = re.split(r'[_\-]+', short)
    first_token = tokens[0] if tokens else ""

    is_behavior = first_token in BEHAVIOR_VERBS
    is_module = any(name_lower.startswith(p) for p in MODULE_PREFIXES)
    is_test = short.startswith("test_") or "util" in short or "helper" in short

    return {
        "behavior_verb": is_behavior,
        "module_anchor": is_module,
        "test_utility": is_test,
        "other": not (is_behavior or is_module or is_test),
    }


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--decomposition", type=Path, default=Path("results/retrieval_selection_decomposition_0_50.json"))
    parser.add_argument("-o", "--output", type=Path, default=Path("results/naming_bias_analysis_0_50.json"))
    args = parser.parse_args()

    with open(args.decomposition, "r", encoding="utf-8") as f:
        dec = json.load(f)

    failure_cases = []
    all_gold = []
    all_selected = []

    for item in dec["per_item"]:
        qa_id = item["qa_id"]
        gold_symbols = set(item.get("gold_symbols", []))
        selected = item.get("single_selected", "")

        for gold in gold_symbols:
            all_gold.append(gold)
        all_selected.append(selected)

        if selected not in gold_symbols:
            for gold in gold_symbols:
                failure_cases.append({
                    "qa_id": qa_id,
                    "gold": gold,
                    "selected": selected,
                    "gold_class": classify_name(gold),
                    "selected_class": classify_name(selected),
                })

    # Aggregate over all gold symbols
    gold_counter = Counter()
    for g in all_gold:
        for k, v in classify_name(g).items():
            if v:
                gold_counter[k] += 1

    selected_counter = Counter()
    for s in all_selected:
        for k, v in classify_name(s).items():
            if v:
                selected_counter[k] += 1

    # Failure-specific: selected behavior_verb vs gold module_anchor
    failure_counts = {
        "total_failures": len(failure_cases),
        "selected_behavior_and_gold_module": 0,
        "selected_behavior_and_gold_behavior": 0,
        "selected_module_and_gold_module": 0,
        "selected_module_and_gold_behavior": 0,
        "selected_other": 0,
    }
    for case in failure_cases:
        gc = case["gold_class"]
        sc = case["selected_class"]
        if sc["behavior_verb"] and gc["module_anchor"]:
            failure_counts["selected_behavior_and_gold_module"] += 1
        elif sc["behavior_verb"] and gc["behavior_verb"]:
            failure_counts["selected_behavior_and_gold_behavior"] += 1
        elif sc["module_anchor"] and gc["module_anchor"]:
            failure_counts["selected_module_and_gold_module"] += 1
        elif sc["module_anchor"] and gc["behavior_verb"]:
            failure_counts["selected_module_and_gold_behavior"] += 1
        elif sc["other"]:
            failure_counts["selected_other"] += 1

    print("\nNaming Bias Analysis")
    print(f"{'='*60}")
    print(f"\nAll gold symbols ({len(all_gold)}):")
    for cat, count in gold_counter.most_common():
        print(f"  {cat:<16s}: {count:3d} ({count/len(all_gold)*100:5.1f}%)")

    print(f"\nLLM selected symbols ({len(all_selected)}):")
    for cat, count in selected_counter.most_common():
        print(f"  {cat:<16s}: {count:3d} ({count/len(all_selected)*100:5.1f}%)")

    print(f"\nFailure cases ({len(failure_cases)}):")
    print(f"  Selected behavior_verb, gold module_anchor:   {failure_counts['selected_behavior_and_gold_module']:3d} "
          f"({failure_counts['selected_behavior_and_gold_module']/len(failure_cases)*100 if failure_counts['total_failures'] else 0:5.1f}%)")
    print(f"  Selected behavior_verb, gold behavior_verb:   {failure_counts['selected_behavior_and_gold_behavior']:3d} "
          f"({failure_counts['selected_behavior_and_gold_behavior']/len(failure_cases)*100 if failure_counts['total_failures'] else 0:5.1f}%)")
    print(f"  Selected module_anchor, gold module_anchor:   {failure_counts['selected_module_and_gold_module']:3d} "
          f"({failure_counts['selected_module_and_gold_module']/len(failure_cases)*100 if failure_counts['total_failures'] else 0:5.1f}%)")
    print(f"  Selected module_anchor, gold behavior_verb:   {failure_counts['selected_module_and_gold_behavior']:3d} "
          f"({failure_counts['selected_module_and_gold_behavior']/len(failure_cases)*100 if failure_counts['total_failures'] else 0:5.1f}%)")
    print(f"  Selected other:                               {failure_counts['selected_other']:3d} "
          f"({failure_counts['selected_other']/len(failure_cases)*100 if failure_counts['total_failures'] else 0:5.1f}%)")

    output = {
        "gold_distribution": dict(gold_counter),
        "selected_distribution": dict(selected_counter),
        "failure_breakdown": failure_counts,
        "failure_cases": failure_cases,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
