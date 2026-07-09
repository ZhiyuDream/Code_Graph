"""
Module Abstraction Layer。

在 clangd Syntax Graph 之上构建 Agent 可利用的语义模块层：
- 用社区发现从 call graph 自动划分 Module
- 用 LLM 给每个 Module 生成 name + summary
- 提供 Question → Module 的匹配能力
- 提供 Module → Functions 的展开能力

核心用途：让 Agent 先在高层 Module 上推理导航，再下钻到函数级细节。
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

from src.core.neo4j_client import run_cypher


@dataclass
class Module:
    """高层语义模块。"""
    id: str
    name: str = ""
    summary: str = ""
    function_ids: List[str] = field(default_factory=list)
    file_paths: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "summary": self.summary,
            "function_ids": self.function_ids,
            "file_paths": self.file_paths,
        }

    @staticmethod
    def from_dict(d: dict) -> "Module":
        return Module(
            id=d["id"],
            name=d.get("name", ""),
            summary=d.get("summary", ""),
            function_ids=d.get("function_ids", []),
            file_paths=d.get("file_paths", []),
        )


class ModuleAbstraction:
    """
    Repository Module Abstraction 管理器。

    用法：
        ma = ModuleAbstraction(cache_path="data/module_abstraction.json")
        if not ma.loaded:
            ma.build_from_neo4j()
            ma.save()
        modules = ma.retrieve_modules(question_embedding, top_k=5)
    """

    def __init__(
        self,
        cache_path: Optional[str] = None,
        model: str = "gpt-4.1-mini",
        resolution: float = 0.5,
        min_module_size: int = 10,
        same_file_weight: float = 0.5,
    ):
        self.cache_path = Path(cache_path) if cache_path else _ROOT / "data" / "module_abstraction.json"
        self.model = model
        self.resolution = resolution
        self.min_module_size = min_module_size
        self.same_file_weight = same_file_weight
        self.modules: Dict[str, Module] = {}
        self.loaded = False

        self.client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)

    # ── Build ─────────────────────────────────────────────────────────

    def build_from_neo4j(self) -> None:
        """从 Neo4j 读取函数和调用关系，构建 Module Abstraction。"""
        print("[ModuleAbstraction] Fetching functions from Neo4j...")
        func_records = run_cypher(
            "MATCH (f:Function) RETURN f.id, f.name, f.file_path, f.start_line, f.end_line"
        )
        functions = {r["f.id"]: r for r in func_records}
        print(f"                  Functions: {len(functions)}")

        print("[ModuleAbstraction] Fetching CALLS edges from Neo4j...")
        call_records = run_cypher(
            "MATCH (a:Function)-[:CALLS]->(b:Function) RETURN a.id AS caller, b.id AS callee"
        )
        calls = [(r["caller"], r["callee"]) for r in call_records if r["caller"] != r["callee"]]
        print(f"                  Calls: {len(calls)}")

        self._build_modules(functions, calls)
        self._summarize_modules(functions)

    def _build_modules(self, functions: Dict[str, dict], calls: List[Tuple[str, str]]) -> None:
        """Louvain + 同文件加权社区发现。"""
        import community as community_louvain

        G = nx.Graph()
        for fid, f in functions.items():
            G.add_node(fid, name=f["f.name"], file_path=f["f.file_path"])

        # CALLS 边
        seen_calls = set()
        for caller, callee in calls:
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
        for fid, f in functions.items():
            file_to_funcs[f["f.file_path"]].append(fid)

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

        partition = community_louvain.best_partition(
            G, weight="weight", resolution=self.resolution, random_state=42
        )

        communities = defaultdict(list)
        for fid, cid in partition.items():
            communities[cid].append(fid)

        # 合并小社区
        large = {cid: mem for cid, mem in communities.items() if len(mem) >= self.min_module_size}
        small = {cid: mem for cid, mem in communities.items() if len(mem) < self.min_module_size}

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

        self.modules = {}
        for cid, fids in communities.items():
            module_id = f"module:{cid}"
            file_paths = sorted(set(functions[fid]["f.file_path"] for fid in fids))
            self.modules[module_id] = Module(
                id=module_id,
                function_ids=fids,
                file_paths=file_paths,
            )

        print(f"[ModuleAbstraction] Built {len(self.modules)} modules")

    def _summarize_modules(self, functions: Dict[str, dict]) -> None:
        """用 LLM 给每个 Module 生成 name 和 summary。"""
        print(f"[ModuleAbstraction] Summarizing {len(self.modules)} modules with {self.model}...")

        for idx, module in enumerate(self.modules.values(), 1):
            sample_funcs = self._select_representative_functions(module, functions)
            sample_files = module.file_paths[:5]

            prompt = f"""你是一名资深代码库分析师。请根据以下代码模块中的函数名、文件路径，
用简洁的短语概括这个模块的职责，并给出一句话总结。

该模块包含 {len(module.function_ids)} 个函数，来自 {len(module.file_paths)} 个文件。

代表性函数：
{chr(10).join(f"- {name}" for name in sample_funcs)}

文件路径：
{chr(10).join(f"- {fp}" for fp in sample_files)}

请输出 JSON：
{{
  "name": "3-5个词的模块名，如 Chat Template Management",
  "summary": "一句话描述该模块职责，不超过30字"
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
                module.name = data.get("name", module.id)
                module.summary = data.get("summary", "")
            except Exception as e:
                module.name = module.id
                module.summary = f"Module with {len(module.function_ids)} functions"
                print(f"                  Warning: failed to summarize {module.id}: {e}")

            if idx % 10 == 0:
                print(f"                  Progress: {idx}/{len(self.modules)}")

    def _select_representative_functions(self, module: Module, functions: Dict[str, dict], top_k: int = 15) -> List[str]:
        """选择代表性函数：优先选度数高的，或名字有信息量的。"""
        # 简单策略：按函数名长度和是否包含模块目录名打分
        scored = []
        for fid in module.function_ids:
            name = functions[fid]["f.name"]
            file_path = functions[fid]["f.file_path"]
            score = 0
            # 名字不要太短也不要太长
            if 5 <= len(name) <= 40:
                score += 1
            # 避免纯测试函数
            if not name.startswith("test_"):
                score += 1
            # 与文件路径相关
            file_stem = Path(file_path).stem
            if file_stem.replace("-", "_").lower() in name.lower():
                score += 2
            scored.append((name, score))

        scored.sort(key=lambda x: -x[1])
        return [name for name, _ in scored[:top_k]]

    # ── Persistence ───────────────────────────────────────────────────

    def save(self) -> None:
        """保存到 JSON。"""
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "resolution": self.resolution,
            "min_module_size": self.min_module_size,
            "model": self.model,
            "modules": [m.to_dict() for m in self.modules.values()],
        }
        with open(self.cache_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"[ModuleAbstraction] Saved to {self.cache_path}")

    def load(self) -> bool:
        """从 JSON 加载。返回是否成功。"""
        if not self.cache_path.exists():
            return False
        with open(self.cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.resolution = data.get("resolution", self.resolution)
        self.min_module_size = data.get("min_module_size", self.min_module_size)
        self.model = data.get("model", self.model)
        self.modules = {m["id"]: Module.from_dict(m) for m in data.get("modules", [])}
        self.loaded = True
        print(f"[ModuleAbstraction] Loaded {len(self.modules)} modules from {self.cache_path}")
        return True

    # ── Retrieval ─────────────────────────────────────────────────────

    def retrieve_modules(
        self,
        question_embedding: np.ndarray,
        doc_matrix: np.ndarray,
        chunk_index_by_id: Dict[str, int],
        top_k: int = 5,
        scoring: str = "max",
    ) -> List[Tuple[Module, float]]:
        """
        用 question embedding 检索最相关的 modules。

        scoring:
            - "max": module score = max(function similarity)
            - "mean": module score = mean(function similarity)
            - "top5_mean": module score = mean of top-5 function similarities
        """
        scores = []
        for module in self.modules.values():
            idxs = [chunk_index_by_id[fid] for fid in module.function_ids if fid in chunk_index_by_id]
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
            scores.append((module, score))

        scores.sort(key=lambda x: -x[1])
        return scores[:top_k]

    def get_module_function_ids(self, module_ids: List[str]) -> List[str]:
        """获取多个 module 内所有 function ids。"""
        fids = set()
        for mid in module_ids:
            m = self.modules.get(mid)
            if m:
                fids.update(m.function_ids)
        return list(fids)

    def get_modules_for_question(
        self,
        question: str,
        embed_fn,
        doc_matrix: np.ndarray,
        chunk_index_by_id: Dict[str, int],
        top_k: int = 5,
    ) -> List[Tuple[Module, float]]:
        """端到端：Question → Top-K Modules。"""
        q_emb = embed_fn([question])[0]
        return self.retrieve_modules(q_emb, doc_matrix, chunk_index_by_id, top_k=top_k)


if __name__ == "__main__":
    ma = ModuleAbstraction()
    if not ma.load():
        ma.build_from_neo4j()
        ma.save()
    print(f"\nModules: {len(ma.modules)}")
    for m in list(ma.modules.values())[:3]:
        print(f"  {m.id}: {m.name}")
        print(f"    {m.summary}")
        print(f"    functions: {len(m.function_ids)}, files: {len(m.file_paths)}")
