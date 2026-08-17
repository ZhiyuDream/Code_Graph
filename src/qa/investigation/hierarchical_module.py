"""
Hierarchical Module Navigator。

让 Agent 在高层 Module 抽象上推理，再下钻到函数级细节。

两层导航：
1. Module Level: Question → Top-K Modules (via embedding) → LLM selects relevant modules
2. Function Level: Selected Modules → Function Retrieval → Optional call graph expansion

这个模块复用：
- src.core.module_abstraction.ModuleAbstraction
- src.qa.retrievers.fast_embedding.FastEmbeddingRetriever
- src.search.call_chain
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))

from config import OPENAI_API_KEY, OPENAI_BASE_URL
from openai import OpenAI

from src.core.module_abstraction import Module, ModuleAbstraction
from src.qa.retrievers.fast_embedding import FastEmbeddingRetriever
from src.search.call_chain import get_callers, get_callees


class HierarchicalModuleNavigator:
    """
    分层模块导航器。

    用法：
        nav = HierarchicalModuleNavigator(module_abstraction=ma)
        result = nav.investigate(question, mode="llm_select", max_modules=3, max_functions=50)
    """

    def __init__(
        self,
        module_abstraction: ModuleAbstraction,
        model: str = "gpt-4.1-mini",
    ):
        self.ma = module_abstraction
        self.model = model
        self.client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)
        self.retriever = FastEmbeddingRetriever()
        self.chunk_index_by_id = {
            ch["id"]: idx for idx, ch in enumerate(self.retriever.chunks)
        }

    def investigate(
        self,
        question: str,
        mode: str = "module_topk",
        max_modules: int = 3,
        max_functions: int = 50,
        expand_hops: int = 0,
    ) -> Dict:
        """
        执行分层调查。

        mode:
            - "module_topk": 直接取 top-K modules，在其函数内检索
            - "llm_select": 让 LLM 根据 module summary 选择相关 modules
            - "hierarchical": LLM 选 modules + 在 module 内做 call graph expansion
        """
        q_emb = self.retriever.encode_queries([question])[0]

        # Step 1: Module-level retrieval
        module_candidates = self.ma.retrieve_modules(
            q_emb, self.retriever.doc_matrix, self.chunk_index_by_id, top_k=max_modules * 2
        )

        # Step 2: Module selection
        if mode == "module_topk":
            selected_modules = [m for m, _ in module_candidates[:max_modules]]
        elif mode in ("llm_select", "hierarchical"):
            selected_modules = self._llm_select_modules(question, module_candidates, max_modules)
        else:
            raise ValueError(f"Unknown mode: {mode}")

        # Step 3: Collect function candidates
        candidate_fids = set()
        for m in selected_modules:
            candidate_fids.update(m.function_ids)

        # Step 4: Optional call graph expansion
        if expand_hops > 0 and mode == "hierarchical":
            candidate_fids = self._expand_by_call_graph(candidate_fids, expand_hops)

        # Step 5: Function-level retrieval
        # 用 candidate functions 所在文件作为 filter，再在这些文件内做 embedding retrieval
        candidate_files = set()
        for fid in candidate_fids:
            # fid 格式：file_path:name:start_line
            parts = fid.rsplit(":", 2)
            if len(parts) >= 3:
                candidate_files.add(parts[0])

        function_results = self.retriever.retrieve(
            np.asarray([q_emb], dtype=np.float32),
            file_filter=candidate_files,
            top_k=max_functions,
        )

        return {
            "question": question,
            "mode": mode,
            "selected_modules": [
                {"id": m.id, "name": m.name, "summary": m.summary, "func_count": len(m.function_ids)}
                for m in selected_modules
            ],
            "candidate_count": len(candidate_fids),
            "function_results": function_results,
        }

    def _llm_select_modules(
        self,
        question: str,
        module_candidates: List[Tuple[Module, float]],
        max_select: int,
    ) -> List[Module]:
        """用 LLM 根据 module summary 选择最相关的 modules。"""
        if len(module_candidates) <= max_select:
            return [m for m, _ in module_candidates]

        prompt = f"""你是一位代码库审计专家。请根据问题，从以下候选模块中选择最相关的 {max_select} 个模块。

问题：{question}

候选模块（按相关性排序）：
"""
        for idx, (m, score) in enumerate(module_candidates, 1):
            prompt += f"\n{idx}. {m.name}\n   Summary: {m.summary}\n   Functions: {len(m.function_ids)}\n   Score: {score:.3f}"

        prompt += f"""

请输出 JSON：
{{
  "selected_indices": [1, 2, ...],
  "reasoning": "为什么选择这些模块"
}}

注意：只返回 {max_select} 个索引，从 1 开始计数。
"""
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=500,
            )
            text = resp.choices[0].message.content.strip()
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                text = text.split("```")[1].split("```")[0].strip()
            data = json.loads(text)
            indices = data.get("selected_indices", list(range(1, max_select + 1)))
            selected = []
            for idx in indices[:max_select]:
                if 1 <= idx <= len(module_candidates):
                    selected.append(module_candidates[idx - 1][0])
            if selected:
                return selected
        except Exception as e:
            print(f"[HierarchicalModuleNavigator] LLM select failed: {e}")

        return [m for m, _ in module_candidates[:max_select]]

    def _expand_by_call_graph(self, fids: set, hops: int = 1) -> set:
        """沿 call graph 扩展 function 候选集。"""
        expanded = set(fids)
        current = set(fids)
        for _ in range(hops):
            next_level = set()
            for fid in current:
                try:
                    callers = get_callers(fid)
                    callees = get_callees(fid)
                    for c in callers:
                        next_level.add(c["id"])
                    for c in callees:
                        next_level.add(c["id"])
                except Exception:
                    continue
            expanded.update(next_level)
            current = next_level
        return expanded


def build_default_navigator(cache_path: Optional[str] = None) -> HierarchicalModuleNavigator:
    """构建默认的 HierarchicalModuleNavigator。"""
    ma = ModuleAbstraction(cache_path=cache_path)
    if not ma.loaded:
        if not ma.load():
            ma.build_from_neo4j()
            ma.save()
    return HierarchicalModuleNavigator(module_abstraction=ma)


if __name__ == "__main__":
    nav = build_default_navigator()
    question = "How is chat template selected in llama.cpp?"
    result = nav.investigate(question, mode="llm_select", max_modules=3, max_functions=20)
    print(f"Question: {result['question']}")
    print(f"Selected modules:")
    for m in result["selected_modules"]:
        print(f"  - {m['name']}: {m['summary']}")
    print(f"Candidate functions: {result['candidate_count']}")
    print(f"Top functions:")
    for r in result["function_results"][:5]:
        print(f"  - {r['metadata']['name']} ({r['metadata']['file_path']})")
