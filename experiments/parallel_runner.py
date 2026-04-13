#!/usr/bin/env python3
"""
QAv2 数据集并行评测脚本。

用法：
  python experiments/parallel_runner.py --limit 0   # 跑全部 360 题
  python experiments/parallel_runner.py --limit 50  # 只跑 50 题测试
"""
import argparse
import json
import csv
import time
import sys
import os
import re
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from datetime import datetime

# ── path setup ──────────────────────────────────────────────────────
_FILE = Path(__file__).resolve()
_ROOT = _FILE.parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "tools"))
sys.path.insert(0, str(_ROOT))

SCRIPT_DIR = _ROOT / "tools"
DATA_DIR = _ROOT / "data"
RESULTS_DIR = _ROOT / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

PARALLEL_WORKERS = 20
LLM_JUDGE_WORKERS = 20


# ── 1. 转换 QAv2 JSON → CSV ──────────────────────────────────────────
def convert_qav2_to_csv(qav2_path: Path, output_csv: Path):
    with open(qav2_path) as f:
        d = json.load(f)

    qs = d["questions"]
    rows = []
    for i, q in enumerate(qs):
        entity = q.get("entity", {})
        entity_name = entity.get("name", "") if isinstance(entity, dict) else str(entity)

        evidence = q.get("evidence", {})
        if isinstance(evidence, dict):
            ev_lines = evidence.get("lines", [])
            ev_loc = evidence.get("location", "")
            evidence_str = f"{ev_loc}:{','.join(map(str, ev_lines))}" if ev_lines else ""
        else:
            evidence_str = str(evidence)

        ref_answer = str(q.get("full_answer", ""))[:500]

        rows.append({
            "index": i,
            "类别": q.get("level1", ""),
            "子类": q.get("level2", ""),
            "问题类型": q.get("question_type", ""),
            "意图": q.get("intention", ""),
            "实体名称": entity_name,
            "具体问题": q.get("question", ""),
            "答案": ref_answer,
            "Evidence": evidence_str,
        })

    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "index", "类别", "子类", "问题类型", "意图", "实体名称", "具体问题", "答案", "Evidence"
        ])
        writer.writeheader()
        writer.writerows(rows)

    print(f"转换完成: {output_csv} ({len(rows)} 题)")
    return str(output_csv)


# ── 2. LLM Judge ────────────────────────────────────────────────────
def llm_judge_one(args) -> dict:
    """单题 LLM Judge"""
    idx, question, reference, agent_answer, rag_answer, model = args
    from openai import OpenAI
    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"),
                    base_url=os.environ.get("OPENAI_BASE_URL") or None)

    prompt = f"""你是一位严格的代码问答质量评估员。请对下面这个问题的两种回答质量分别打分。

【问题】
{question}

【参考答案】
{reference[:500]}

【Graph-Agent 答案】
{agent_answer[:500]}

【Classic-RAG 答案】
{rag_answer[:500]}

评分标准（0-1分）：
- 1.0: 完全正确，涵盖关键信息点
- 0.75: 基本正确，但遗漏1-2个次要点
- 0.5: 部分正确，有遗漏但方向对
- 0.25: 少量正确，多数错误
- 0.0: 完全错误或无关

请先给出对两种答案的简要评价，然后给出各自的分数。
格式：
Graph-Agent评价：[评价]
Graph-Agent分数：[0到1之间的小数]
RAG评价：[评价]
RAG分数：[0到1之间的小数]
"""
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=400,
            timeout=60,
        )
        content = (resp.choices[0].message.content or "").strip()

        ag_score, rag_score = 0.5, 0.5
        ag_reason, rag_reason = content, content

        m = re.search(r'Graph-Agent[分数打分：:]\s*([0-9.]+)', content)
        if not m:
            m = re.search(r'Graph-Agent分数[：:\s]+([0-9.]+)', content)
        if m: ag_score = float(m.group(1))
        m = re.search(r'RAG[分数打分：:]\s*([0-9.]+)', content)
        if not m:
            m = re.search(r'RAG分数[：:\s]+([0-9.]+)', content)
        if m: rag_score = float(m.group(1))

        return {
            "index": idx,
            "question": question[:100],
            "Graph-Agent": {"answer": agent_answer[:200], "llm_judge_score": ag_score, "reason": ag_reason[:200]},
            "Classic-RAG": {"answer": rag_answer[:200], "llm_judge_score": rag_score, "reason": rag_reason[:200]},
            "delta": ag_score - rag_score,
        }
    except Exception as e:
        return {
            "index": idx,
            "Graph-Agent": {"answer": agent_answer[:200], "llm_judge_score": 0.0, "reason": str(e)},
            "Classic-RAG": {"answer": rag_answer[:200], "llm_judge_score": 0.0, "reason": str(e)},
            "delta": 0.0,
        }


def run_llm_judge(agent_results: list, rag_results: list, output_path: Path, model: str):
    """对两个系统的结果并行跑 LLM Judge"""
    from config import LLM_MODEL
    judge_model = model or LLM_MODEL

    # 建立 index → result 映射
    ag_by_idx = {r["index"]: r for r in agent_results}
    rg_by_idx = {r["index"]: r for r in rag_results}
    all_indices = sorted(set(ag_by_idx.keys()) & set(rg_by_idx.keys()))
    print(f"共 {len(all_indices)} 题需要 Judge")

    def clean_str(s):
        """Convert to string, handling NaN and None"""
        if s is None:
            return ""
        if isinstance(s, float):  # NaN
            return ""
        return str(s)[:500]  # Truncate to avoid too long

    args_list = []
    for idx in all_indices:
        ag = ag_by_idx[idx]
        rg = rg_by_idx[idx]
        args_list.append((
            idx,
            clean_str(ag.get("具体问题", "")),
            clean_str(ag.get("参考答案", "")),
            clean_str(ag.get("生成答案", "")),
            clean_str(rg.get("生成答案", "")),
            judge_model,
        ))

    counter = {"done": 0}
    lock = Lock()
    results = []

    def worker(args):
        result = llm_judge_one(args)
        with lock:
            counter["done"] += 1
            if counter["done"] % 20 == 0:
                print(f"  Judge 进度: {counter['done']}/{len(all_indices)}", flush=True)
        return result

    with ThreadPoolExecutor(max_workers=LLM_JUDGE_WORKERS) as executor:
        futures = [executor.submit(worker, a) for a in args_list]
        for f in as_completed(futures):
            results.append(f.result())

    results.sort(key=lambda x: x["index"])

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    n = len(results)
    ag_mean = sum(r["Graph-Agent"]["llm_judge_score"] for r in results) / n
    rg_mean = sum(r["Classic-RAG"]["llm_judge_score"] for r in results) / n
    print(f"\nJudge 完成: {output_path}")
    print(f"Graph-Agent LLM-Judge: {ag_mean:.4f}")
    print(f"Classic-RAG LLM-Judge: {rg_mean:.4f}")
    print(f"Delta: {ag_mean - rg_mean:+.4f}")
    return results


# ── 3. 运行 Classic-RAG ─────────────────────────────────────────────
def run_classic_rag(csv_path: Path, output_path: Path, limit: int = 0):
    """并行运行 Classic-RAG（直接调用模块内函数，不走子进程）"""
    import pandas as pd
    from classic_rag import retrieve, generate_answer
    from openai import OpenAI

    df = pd.read_csv(csv_path)
    if limit > 0:
        df = df.head(limit)
    print(f"Classic-RAG: {len(df)} 题，{PARALLEL_WORKERS} 并行")

    INDEX_PATH = DATA_DIR / "classic_rag_index.json"
    if not INDEX_PATH.exists():
        raise FileNotFoundError(f"RAG index not found: {INDEX_PATH}")
    with open(INDEX_PATH) as f:
        index = json.load(f)
    print(f"  索引: {len(index['chunks'])} chunks")

    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"),
                    base_url=os.environ.get("OPENAI_BASE_URL") or None)

    rows = []
    lock = Lock()
    counter = {"done": 0}

    def process_one(idx_row):
        i, row = idx_row
        idx = int(row["index"])
        question = row["具体问题"]
        evidence_raw = str(row["Evidence"])

        try:
            q_emb = client.embeddings.create(
                model=os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small"),
                input=[question]
            ).data[0].embedding
            top_chunks = retrieve(q_emb, index, top_k=6)
            answer, token_usage = generate_answer(client, question, top_chunks)
        except Exception as e:
            answer = ""
            top_chunks = []
            token_usage = {}

        with lock:
            counter["done"] += 1
            if counter["done"] % 20 == 0:
                print(f"  RAG 进度: {counter['done']}/{len(df)}", flush=True)

        return {
            "index": idx,
            "类别": row.get("类别", ""),
            "子类": row.get("子类", ""),
            "具体问题": question,
            "参考答案": row.get("答案", ""),
            "生成答案": answer,
            "Evidence": evidence_raw,
            "检索chunks": [{"id": c["id"], "score": c["score"]} for c in top_chunks],
            "延迟_s": 0,
            "token_usage": token_usage,
            "错误": None,
        }

    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as executor:
        futures = [executor.submit(process_one, (i, row)) for i, row in df.iterrows()]
        for f in as_completed(futures):
            rows.append(f.result())

    rows.sort(key=lambda x: x["index"])
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print(f"RAG 完成: {output_path}")
    return rows


# ── 4. 运行 Graph-Agent ──────────────────────────────────────────────
def run_graph_agent(csv_path: Path, output_path: Path, limit: int = 0):
    """并行运行 Graph-Agent"""
    import pandas as pd
    from agent_qa import run_agent
    from neo4j_writer import get_driver

    df = pd.read_csv(csv_path)
    if limit > 0:
        df = df.head(limit)
    print(f"Graph-Agent: {len(df)} 题，{PARALLEL_WORKERS} 并行")

    driver = get_driver()
    driver.verify_connectivity()

    rows = []
    lock = Lock()
    counter = {"done": 0}

    def process_one(idx_row):
        i, row = idx_row
        idx = int(row["index"])
        question = row["具体问题"]

        try:
            answer, trace, steps, token_usage = run_agent(driver, question)
        except Exception as e:
            answer = ""
            trace = []
            steps = 0
            token_usage = {}

        with lock:
            counter["done"] += 1
            if counter["done"] % 20 == 0:
                print(f"  Agent 进度: {counter['done']}/{len(df)}", flush=True)

        return {
            "index": idx,
            "类别": row.get("类别", ""),
            "子类": row.get("子类", ""),
            "具体问题": question,
            "参考答案": row.get("答案", ""),
            "生成答案": answer,
            "Evidence": row.get("Evidence", ""),
            "工具调用步数": steps,
            "工具轨迹": trace,
            "延迟_s": 0,
            "token_usage": token_usage,
            "错误": None,
        }

    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as executor:
        futures = [executor.submit(process_one, (i, row)) for i, row in df.iterrows()]
        for f in as_completed(futures):
            rows.append(f.result())

    driver.close()
    rows.sort(key=lambda x: x["index"])
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print(f"Agent 完成: {output_path}")
    return rows


# ── 5. Good/Bad Case ───────────────────────────────────────────────
def analyze_good_bad(results: list, output_dir: Path):
    improved = [r for r in results if r["delta"] > 0.15]
    degraded = [r for r in results if r["delta"] < -0.15]

    improved.sort(key=lambda x: -x["delta"])
    degraded.sort(key=lambda x: x["delta"])

    report = {
        "graph_agent_wins": improved[:15],
        "rag_wins": degraded[:15],
    }
    with open(output_dir / "good_bad_cases.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"Good/Bad Case: Graph-Agent 胜 {len(improved)}, RAG 胜 {len(degraded)}")


# ── 6. 主流程 ───────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="0=全部")
    parser.add_argument("--skip-agent", action="store_true")
    parser.add_argument("--skip-rag", action="store_true")
    parser.add_argument("--skip-judge", action="store_true")
    parser.add_argument("--workers", type=int, default=20)
    args = parser.parse_args()

    global PARALLEL_WORKERS, LLM_JUDGE_WORKERS
    PARALLEL_WORKERS = args.workers
    LLM_JUDGE_WORKERS = args.workers

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    qav2_path = DATA_DIR / "llama_cpp_QAv2.json"
    csv_path = RESULTS_DIR / f"qav2_{timestamp}.csv"
    agent_output = RESULTS_DIR / f"graph_agent_{timestamp}.json"
    rag_output = RESULTS_DIR / f"classic_rag_{timestamp}.json"
    judge_output = RESULTS_DIR / f"judge_{timestamp}.json"

    print("=" * 60)
    print(f"QAv2 并行评测 | workers={PARALLEL_WORKERS} | limit={args.limit}")
    print("=" * 60)

    print("\n[1/4] 转换 QAv2 → CSV...")
    convert_qav2_to_csv(qav2_path, csv_path)

    if not args.skip_rag:
        print("\n[2/4] 运行 Classic-RAG...")
        t0 = time.time()
        rag_results = run_classic_rag(csv_path, rag_output, args.limit)
        print(f"  耗时: {time.time() - t0:.1f}s")

    if not args.skip_agent:
        print("\n[3/4] 运行 Graph-Agent（等待 Neo4j 重建完成）...")
        # 检查 Neo4j 是否有数据
        from neo4j_writer import get_driver
        driver = get_driver()
        with driver.session() as s:
            result = s.run("MATCH (f:Function) RETURN count(f) as cnt")
            cnt = result.single()["cnt"]
        driver.close()
        if cnt == 0:
            print("  Neo4j 为空，跳过 Graph-Agent（需先运行 scripts/run_stage1_clangd.py）")
            print("  请运行: python scripts/run_stage1_clangd.py")
            print("  然后重新运行: python experiments/parallel_runner.py --skip-rag")
        else:
            t0 = time.time()
            agent_results = run_graph_agent(csv_path, agent_output, args.limit)
            print(f"  耗时: {time.time() - t0:.1f}s")

    if not args.skip_judge:
        if not (Path(agent_output).exists() and Path(rag_output).exists()):
            print("\n[Judge] 需要先完成 Agent 和 RAG 评测")
        else:
            print("\n[4/4] 运行 LLM Judge...")
            with open(agent_output) as f:
                agent_results = json.load(f)
            with open(rag_output) as f:
                rag_results = json.load(f)
            t0 = time.time()
            judge_results = run_llm_judge(agent_results, rag_results, judge_output, None)
            print(f"  耗时: {time.time() - t0:.1f}s")
            analyze_good_bad(judge_results, RESULTS_DIR)

    print(f"\n结果在: {RESULTS_DIR}/")
    print(f"  RAG:       {rag_output}")
    if Path(agent_output).exists():
        print(f"  Agent:     {agent_output}")
    if Path(judge_output).exists():
        print(f"  Judge:     {judge_output}")


if __name__ == "__main__":
    main()
