"""
Concept Abstraction Layer。

在 Module Abstraction 之下进一步构建 Semantic Concept：
- 对每个 Module 内部，用更高 resolution 的社区发现得到 sub-communities
- 每个 sub-community 是一个 semantic concept
- 用 LLM 给每个 concept 生成 name + summary

概念 vs 模块的区别：
- Module：开发者组织代码的物理/文件边界
- Concept：围绕某个语义职责（如 "Chat Template Loading"）的功能集合

Concept 可以跨文件、跨目录，但通常在一个 module 内。
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import networkx as nx
import numpy as np

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from config import OPENAI_API_KEY, OPENAI_BASE_URL
from openai import OpenAI

from src.core.module_abstraction import Module, ModuleAbstraction
from src.core.neo4j_client import run_cypher


@dataclass
class Concept:
    """语义概念节点。"""
    id: str
    parent_module_id: str
    name: str = ""
    summary: str = ""
    function_ids: List[str] = field(default_factory=list)
    file_paths: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "parent_module_id": self.parent_module_id,
            "name": self.name,
            "summary": self.summary,
            "function_ids": self.function_ids,
            "file_paths": self.file_paths,
        }

    @staticmethod
    def from_dict(d: dict) -> "Concept":
        return Concept(
            id=d["id"],
            parent_module_id=d["parent_module_id"],
            name=d.get("name", ""),
            summary=d.get("summary", ""),
            function_ids=d.get("function_ids", []),
            file_paths=d.get("file_paths", []),
        )


class ConceptAbstraction:
    """
    Repository Concept Abstraction 管理器。

    用法：
        ca = ConceptAbstraction(cache_path="data/concept_abstraction.json")
        if not ca.loaded:
            ca.build_from_modules(module_abstraction)
            ca.save()
        concepts = ca.retrieve_concepts(question_embedding, doc_matrix, chunk_index_by_id, top_k=10)
    """

    def __init__(
        self,
        cache_path: Optional[str] = None,
        model: str = "gpt-4.1-mini",
        sub_resolution: float = 2.0,
        min_concept_size: int = 3,
        same_file_weight: float = 0.3,
    ):
        self.cache_path = Path(cache_path) if cache_path else _ROOT / "data" / "concept_abstraction.json"
        self.model = model
        self.sub_resolution = sub_resolution
        self.min_concept_size = min_concept_size
        self.same_file_weight = same_file_weight
        self.concepts: Dict[str, Concept] = {}
        self.loaded = False
        self.client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)

    def build_from_modules(
        self,
        module_abstraction: ModuleAbstraction,
        functions: Optional[Dict[str, dict]] = None,
        calls: Optional[List[Tuple[str, str]]] = None,
    ) -> None:
        """在已有 Module Abstraction 基础上构建 Concept Abstraction。"""
        if functions is None or calls is None:
            print("[ConceptAbstraction] Fetching functions and calls from Neo4j...")
            func_records = run_cypher(
                "MATCH (f:Function) RETURN f.id, f.name, f.file_path, f.start_line, f.end_line"
            )
            functions = {r["f.id"]: r for r in func_records}
            call_records = run_cypher(
                "MATCH (a:Function)-[:CALLS]->(b:Function) RETURN a.id AS caller, b.id AS callee"
            )
            calls = [(r["caller"], r["callee"]) for r in call_records if r["caller"] != r["callee"]]

        print(f"[ConceptAbstraction] Building concepts from {len(module_abstraction.modules)} modules...")

        all_concepts = []
        for module in module_abstraction.modules.values():
            sub_concepts = self._cluster_module(module, functions, calls)
            for cid, fids in sub_concepts.items():
                concept_id = f"{module.id}:concept:{cid}"
                file_paths = sorted(set(functions[fid]["f.file_path"] for fid in fids))
                all_concepts.append(Concept(
                    id=concept_id,
                    parent_module_id=module.id,
                    function_ids=fids,
                    file_paths=file_paths,
                ))

        self.concepts = {c.id: c for c in all_concepts}
        print(f"[ConceptAbstraction] Built {len(self.concepts)} concepts")
        self._summarize_concepts(functions)

    def _cluster_module(
        self,
        module: Module,
        functions: Dict[str, dict],
        calls: List[Tuple[str, str]],
    ) -> Dict[int, List[str]]:
        """对一个 module 内部的函数做更细粒度的社区发现。"""
        import community as community_louvain

        module_fids = set(module.function_ids)
        if len(module_fids) < self.min_concept_size * 2:
            # 太小的 module 不再拆分
            return {0: list(module_fids)}

        G = nx.Graph()
        for fid in module_fids:
            G.add_node(fid, name=functions[fid]["f.name"], file_path=functions[fid]["f.file_path"])

        # 子图内的 CALLS 边
        seen_calls = set()
        for caller, callee in calls:
            if caller not in module_fids or callee not in module_fids or caller == callee:
                continue
            key = tuple(sorted([caller, callee]))
            if key in seen_calls:
                continue
            seen_calls.add(key)
            if G.has_edge(caller, callee):
                G[caller][callee]["weight"] += 1.0
            else:
                G.add_edge(caller, callee, weight=1.0)

        # 同文件边
        file_to_funcs = defaultdict(list)
        for fid in module_fids:
            file_to_funcs[functions[fid]["f.file_path"]].append(fid)
        for fp, fids in file_to_funcs.items():
            if len(fids) < 2:
                continue
            for i in range(len(fids)):
                for j in range(i + 1, len(fids)):
                    a, b = fids[i], fids[j]
                    if G.has_edge(a, b):
                        G[a][b]["weight"] += self.same_file_weight
                    else:
                        G.add_edge(a, b, weight=self.same_file_weight)

        if len(G.edges()) == 0:
            # 没有内部连接，按文件分组
            groups = defaultdict(list)
            for fid in module_fids:
                groups[functions[fid]["f.file_path"]].append(fid)
            return {idx: fids for idx, fids in enumerate(groups.values())}

        partition = community_louvain.best_partition(
            G, weight="weight", resolution=self.sub_resolution, random_state=42
        )

        communities = defaultdict(list)
        for fid, cid in partition.items():
            communities[cid].append(fid)

        # 合并小社区到最近的社区
        large = {cid: mem for cid, mem in communities.items() if len(mem) >= self.min_concept_size}
        small = {cid: mem for cid, mem in communities.items() if len(mem) < self.min_concept_size}

        if large:
            for sc, smem in small.items():
                best = None
                best_score = -1
                for lc, lmem in large.items():
                    score = sum(1 for m in smem for lm in lmem if G.has_edge(m, lm))
                    if score > best_score:
                        best_score = score
                        best = lc
                if best is None or best_score == 0:
                    best = max(large, key=lambda c: len(large[c]))
                large[best].extend(smem)
            communities = large
        else:
            # 所有社区都小，合并成一个大社区
            communities = {0: [fid for mem in communities.values() for fid in mem]}

        return communities

    def _summarize_concepts(self, functions: Dict[str, dict]) -> None:
        """用 LLM 给每个 concept 生成 name + summary。"""
        print(f"[ConceptAbstraction] Summarizing {len(self.concepts)} concepts with {self.model}...")
        for idx, concept in enumerate(self.concepts.values(), 1):
            sample_funcs = self._select_representative_functions(concept, functions)
            sample_files = concept.file_paths[:5]

            prompt = f"""你是一名资深代码库分析师。请根据以下函数簇中的函数名、文件路径，
用简洁的短语概括这个函数簇所代表的语义概念（Concept），并给出一句话总结。

这个概念簇包含 {len(concept.function_ids)} 个函数，来自 {len(concept.file_paths)} 个文件。

代表性函数：
{chr(10).join(f"- {name}" for name in sample_funcs)}

文件路径：
{chr(10).join(f"- {fp}" for fp in sample_files)}

请输出 JSON：
{{
  "name": "3-6个词的概念名，如 Chat Template Loading",
  "summary": "一句话描述该概念职责，不超过40字"
}}
"""
            try:
                resp = self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.0,
                    max_tokens=200,
                )
                text = resp.choices[0].message.content.strip()
                if "```json" in text:
                    text = text.split("```json")[1].split("```")[0].strip()
                elif "```" in text:
                    text = text.split("```")[1].split("```")[0].strip()
                data = json.loads(text)
                concept.name = data.get("name", concept.id)
                concept.summary = data.get("summary", "")
            except Exception as e:
                concept.name = concept.id
                concept.summary = f"Concept with {len(concept.function_ids)} functions"
                print(f"                  Warning: failed to summarize {concept.id}: {e}")

            if idx % 50 == 0:
                print(f"                  Progress: {idx}/{len(self.concepts)}")

    def _select_representative_functions(self, concept: Concept, functions: Dict[str, dict], top_k: int = 10) -> List[str]:
        scored = []
        for fid in concept.function_ids:
            name = functions[fid]["f.name"]
            file_path = functions[fid]["f.file_path"]
            score = 0
            if 5 <= len(name) <= 40:
                score += 1
            if not name.startswith("test_"):
                score += 1
            file_stem = Path(file_path).stem
            if file_stem.replace("-", "_").lower() in name.lower():
                score += 2
            scored.append((name, score))
        scored.sort(key=lambda x: -x[1])
        return [name for name, _ in scored[:top_k]]

    def save(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "sub_resolution": self.sub_resolution,
            "min_concept_size": self.min_concept_size,
            "model": self.model,
            "concepts": [c.to_dict() for c in self.concepts.values()],
        }
        with open(self.cache_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"[ConceptAbstraction] Saved to {self.cache_path}")

    def load(self) -> bool:
        if not self.cache_path.exists():
            return False
        with open(self.cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.sub_resolution = data.get("sub_resolution", self.sub_resolution)
        self.min_concept_size = data.get("min_concept_size", self.min_concept_size)
        self.model = data.get("model", self.model)
        self.concepts = {c["id"]: Concept.from_dict(c) for c in data.get("concepts", [])}
        self.loaded = True
        print(f"[ConceptAbstraction] Loaded {len(self.concepts)} concepts from {self.cache_path}")
        return True

    def retrieve_concepts(
        self,
        question_embedding: np.ndarray,
        doc_matrix: np.ndarray,
        chunk_index_by_id: Dict[str, int],
        top_k: int = 10,
        scoring: str = "max",
    ) -> List[Tuple[Concept, float]]:
        scores = []
        for concept in self.concepts.values():
            idxs = [chunk_index_by_id[fid] for fid in concept.function_ids if fid in chunk_index_by_id]
            if not idxs:
                continue
            sims = doc_matrix[idxs] @ question_embedding
            if scoring == "max":
                score = float(np.max(sims))
            elif scoring == "mean":
                score = float(np.mean(sims))
            elif scoring == "top5_mean":
                k = min(5, len(sims))
                score = float(np.mean(np.partition(sims, -k)[-k:]))
            else:
                raise ValueError(f"Unknown scoring: {scoring}")
            scores.append((concept, score))
        scores.sort(key=lambda x: -x[1])
        return scores[:top_k]

    def get_concept_function_ids(self, concept_ids: List[str]) -> List[str]:
        fids = set()
        for cid in concept_ids:
            c = self.concepts.get(cid)
            if c:
                fids.update(c.function_ids)
        return list(fids)


if __name__ == "__main__":
    ma = ModuleAbstraction()
    if not ma.load():
        ma.build_from_neo4j()
        ma.save()

    ca = ConceptAbstraction()
    if not ca.load():
        ca.build_from_modules(ma)
        ca.save()

    print(f"\nConcepts: {len(ca.concepts)}")
    for c in list(ca.concepts.values())[:5]:
        print(f"\n{c.id}: {c.name}")
        print(f"  {c.summary}")
        print(f"  parent: {c.parent_module_id}")
        print(f"  functions: {len(c.function_ids)}, files: {len(c.file_paths)}")
