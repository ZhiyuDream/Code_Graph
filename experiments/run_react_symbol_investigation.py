#!/usr/bin/env python3
"""
ReAct-style symbol-level investigation experiment.

Agent performs sequential investigation over a candidate pool:
  1. Choose one candidate to read
  2. Observe its code
  3. Record verdict (evidence anchor / not evidence anchor)
  4. Decide next action: investigate another candidate or finish

Tests whether sequential investigation with negative evidence can mitigate
selection degradation in large candidate pools.
"""
import json
import re
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.qa.candidate_pool import build_function_pool
from src.qa.investigation.base import LLMClient, load_prompt
from src.qa.retrievers.fast_embedding import FastEmbeddingRetriever


def extract_json(text: str) -> dict:
    try:
        start = text.find('{')
        end = text.rfind('}')
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
    except Exception:
        pass
    return {}


def run_investigation(item: dict, query_emb, retriever: FastEmbeddingRetriever,
                      llm: LLMClient, max_steps: int) -> dict:
    pool = build_function_pool(query_emb, retriever)
    gold_symbols = item["gold_symbols"]

    visited = {}
    trajectory = []
    final_guess = None

    for step in range(1, max_steps + 1):
        remaining = [c for c in pool if c["name"] not in visited]
        if not remaining:
            break

        candidate_summaries = []
        for i, c in enumerate(remaining[:30], 1):
            sig = c.get("signature", "")
            candidate_summaries.append(f"[{i}] {c['file_path']}::{c['name']}({sig})")

        visited_summaries = []
        for name, verdict in visited.items():
            visited_summaries.append(f"- {name}: {verdict['reasoning']}")

        prompt = load_prompt("react_select_next_symbol").format(
            question=item["question"],
            candidates="\n".join(candidate_summaries),
            visited="\n".join(visited_summaries) or "(none)",
            max_steps=max_steps,
            current_step=step,
        )
        decision = extract_json(llm.call(prompt).strip())

        target = decision.get("target", "")
        action = decision.get("action", "investigate")

        if action == "finish":
            for c in pool:
                if c["name"] == target or target in c["name"]:
                    final_guess = c["name"]
                    break
            if final_guess is None:
                final_guess = target
            trajectory.append({
                "step": step,
                "action": "finish",
                "target": target,
                "reasoning": decision.get("reasoning", ""),
            })
            break

        target_candidate = None
        for c in remaining:
            if c["name"] == target or target in c["name"]:
                target_candidate = c
                break
        if target_candidate is None:
            target_candidate = remaining[0]

        func_text = target_candidate["text"][:1500]
        judge_prompt = load_prompt("judge_evidence_anchor").format(
            question=item["question"],
            file_path=target_candidate["file_path"],
            name=target_candidate["name"],
            code=func_text,
        )
        verdict = extract_json(llm.call(judge_prompt).strip())

        is_evidence = verdict.get("is_evidence_anchor", False)
        verdict_reasoning = verdict.get("reasoning", decision.get("reasoning", "no reasoning"))

        visited[target_candidate["name"]] = {
            "is_evidence_anchor": is_evidence,
            "reasoning": verdict_reasoning,
        }

        trajectory.append({
            "step": step,
            "action": "investigate",
            "target": target_candidate["name"],
            "is_evidence_anchor": is_evidence,
            "reasoning": verdict_reasoning,
        })

        if is_evidence:
            final_guess = target_candidate["name"]
            break

        final_guess = target_candidate["name"]

    correct = final_guess in gold_symbols if final_guess else False
    return {
        "qa_id": item["qa_id"],
        "gold_symbols": sorted(gold_symbols),
        "final_guess": final_guess,
        "correct": correct,
        "trajectory": trajectory,
        "num_visited": len(visited),
    }


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, default=Path("datasets/benchmark_hard.json"))
    parser.add_argument("--range", default="0,50")
    parser.add_argument("--max-steps", type=int, default=5)
    parser.add_argument("-w", "--workers", type=int, default=1)
    parser.add_argument("-o", "--output", type=Path, default=Path("results/react_symbol_investigation_0_50.json"))
    args = parser.parse_args()

    with open(args.benchmark, "r", encoding="utf-8") as f:
        bench = json.load(f)
    all_items = bench["items"]
    if "," in args.range:
        start, end = map(int, args.range.split(","))
    else:
        start, end = 0, len(all_items)

    items = []
    for idx in range(start, end):
        item = all_items[idx]
        gold_symbols = set()
        for ev in item.get("gold_evidence", []):
            sym = ev.get("symbol", "")
            if sym:
                gold_symbols.add(sym)
        items.append({
            "qa_id": item.get("qa_id", f"q{idx}"),
            "question": item.get("question", ""),
            "gold_symbols": gold_symbols,
        })

    retriever = FastEmbeddingRetriever()
    query_embs = retriever.encode_queries([it["question"] for it in items])
    llm = LLMClient()

    results = []
    completed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(run_investigation, item, query_embs[i], retriever, llm, args.max_steps): item
            for i, item in enumerate(items)
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            completed += 1
            print(f"  [{completed}/{len(items)}] {result['qa_id']}: "
                  f"correct={result['correct']} steps={result['num_visited']} guess={result['final_guess']}")

    results.sort(key=lambda x: x["qa_id"])
    total = len(results)
    correct = sum(1 for r in results if r["correct"])

    print(f"\n{'='*60}")
    print(f"ReAct Symbol Investigation ({total} questions, max {args.max_steps} steps)")
    print(f"{'='*60}")
    print(f"Recall: {correct}/{total} = {correct/total*100:.1f}%")

    output = {
        "config": {"max_steps": args.max_steps, "range": args.range},
        "summary": {"total": total, "correct": correct, "recall": correct / total},
        "per_item": results,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
