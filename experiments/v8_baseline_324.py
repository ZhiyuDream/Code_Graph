import os, sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))
os.environ['LLM_MODEL'] = 'deepseek-v4-pro'

from src.core import get_neo4j_driver, close_neo4j_driver, generate_answer
from src.core.llm_client import reset_usage_stats, get_usage_stats
from experiments.module_expansion.run_qa_v8_react_ablation import react_search
from config import NEO4J_DATABASE, REPO_ROOT
from concurrent.futures import ThreadPoolExecutor, as_completed
import json, time

def process_single_baseline(driver, row, idx, retrievers, repo_root, model, provider):
    if idx % 20 == 0:
        print(f"[{idx}] {row.get('question', 'N/A')[:50]}...")
    reset_usage_stats()
    start_time = time.time()
    question = row.get('question', '')
    try:
        trace = {"index": idx, "question": question}
        collected, trace = react_search(driver, question, trace, retrievers, repo_root, model, provider)
        answer = generate_answer(
            question=question,
            collected=collected,
            max_tokens=8192,
            model=model,
            provider=provider,
        )
        usage = get_usage_stats()
        latency = time.time() - start_time
        return {
            "index": idx,
            "id": row.get('id', f'qa_{idx}'),
            "question": question,
            "reference": row.get('answer', ''),
            "generated": answer,
            "router": "V8_Baseline",
            "retrieval": {
                "function_count": len(collected.get("functions", [])),
                "issue_count": len(collected.get("issues", [])),
                "step_count": len(trace.get("react_steps", [])),
            },
            "trace": trace,
            "latency_s": latency,
            "usage": usage,
        }
    except Exception as e:
        import traceback
        return {
            "index": idx,
            "id": row.get('id', f'qa_{idx}'),
            "question": question,
            "reference": row.get('answer', ''),
            "generated": f"处理失败: {str(e)}\n{traceback.format_exc()}",
            "router": "V8_Baseline",
            "error": str(e),
            "latency_s": time.time() - start_time,
        }

def main():
    with open('datasets/llama_cpp_QA_cleaned.json', 'r', encoding='utf-8') as f:
        benchmark = json.load(f)
    questions = benchmark['questions']
    print(f"V8 Baseline 验证: {len(questions)} 题")
    print(f"模型: deepseek-v4-pro")
    print(f"并行: 50 workers")
    print()
    driver = get_neo4j_driver()
    retrievers = {"embedding", "issue", "grep", "graph"}
    repo_root = str(REPO_ROOT)
    model = 'deepseek-v4-pro'
    provider = 'deepseek'
    results = []
    completed = 0
    with ThreadPoolExecutor(max_workers=50) as executor:
        futures = {
            executor.submit(process_single_baseline, driver, q, i, retrievers, repo_root, model, provider): i
            for i, q in enumerate(questions)
        }
        for future in as_completed(futures):
            try:
                result = future.result()
                results.append(result)
                completed += 1
                if completed % 20 == 0 or completed == len(questions):
                    print(f"  已完成 {completed}/{len(questions)} 题...")
                    sorted_results = sorted(results, key=lambda x: x.get('index', 0))
                    with open('results/v8_baseline_324.json', 'w', encoding='utf-8') as f:
                        json.dump(sorted_results, f, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f"  处理题目时出错: {e}")
                completed += 1
    sorted_results = sorted(results, key=lambda x: x.get('index', 0))
    with open('results/v8_baseline_324.json', 'w', encoding='utf-8') as f:
        json.dump(sorted_results, f, ensure_ascii=False, indent=2)
    print(f"\n完成！共处理 {len(results)}/{len(questions)} 题")
    print(f"结果保存至: results/v8_baseline_324.json")
    close_neo4j_driver()

if __name__ == "__main__":
    main()
