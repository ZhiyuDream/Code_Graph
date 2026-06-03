#!/usr/bin/env python3
"""快速跑几个样本，输出精简结果到 /tmp/"""
import json, sys, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

os.environ.setdefault("LLM_MODEL", "deepseek-v4-pro")

from config import REPO_ROOT
from src.qa.pipeline import QAPipeline
from src.qa.expansion import CodeExpander
from src.qa.retrievers.grep import GrepRetriever
from src.qa.retrievers.embedding import EmbeddingRetriever
from src.qa.retrievers.graph import GraphRetriever
from src.core.neo4j_client import get_neo4j_driver
from src.core.llm_client import reset_usage_stats

retrievers = [GrepRetriever(REPO_ROOT, enabled=True)]
emb = EmbeddingRetriever(enabled=True)
if emb.is_available():
    retrievers.append(emb)
try:
    driver = get_neo4j_driver()
    g = GraphRetriever(driver, "neo4j")
    if g.is_available():
        retrievers.append(g)
except Exception:
    pass

pipeline = QAPipeline(
    retrievers=retrievers,
    expander=CodeExpander(),
    enable_react=True,
    max_react_steps=5,
    model="deepseek-v4-pro",
    repo_root=str(REPO_ROOT) if REPO_ROOT else "",
)

data = json.load(open("datasets/posthoc_audit_qa.json"))["items"]
indices = [int(x) for x in sys.argv[1:]] if len(sys.argv) > 1 else [0, 11, 22, 33, 44]

for idx in indices:
    item = data[idx]
    q = item.get("question", "")
    print(f"\n{'='*60}")
    print(f"[{idx}] {q[:70]}...")
    reset_usage_stats()
    result = pipeline.run(q)
    print(f"  error={result.error!r}")
    print(f"  answer_len={len(result.answer)}")
    print(f"  tokens={result.total_tokens}")
    print(f"  latency_ms={result.total_latency_ms:.0f}")
    for s in result.steps:
        print(f"    step {s.step}: {s.phase} action={s.action} gain={s.info_gain}")
    out = {
        "index": idx,
        "question": q,
        "answer": result.answer,
        "error": result.error,
        "steps": [s.to_dict() for s in result.steps],
        "total_tokens": result.total_tokens,
        "total_latency_ms": result.total_latency_ms,
    }
    with open(f"/tmp/ds_{idx}.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"  saved to /tmp/ds_{idx}.json")
