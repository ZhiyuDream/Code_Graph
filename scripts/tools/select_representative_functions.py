#!/usr/bin/env python3
from __future__ import annotations

"""
Representative Function Selector — 三维度筛选代表性函数

维度1：影响力（PageRank/度数/入口/控制流）
维度2：空间覆盖（每目录/每模块至少覆盖）
维度3：功能覆盖（按语义领域去重）

输出：results/representative_functions.json
"""

import json
import re
from pathlib import Path
from collections import defaultdict

# 确保能导入 src
import sys
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

from config import NEO4J_DATABASE
from neo4j_writer import get_driver

# ---------------------------------------------------------------------------
# 控制流关键词（用于识别关键节点）
# ---------------------------------------------------------------------------
_CONTROL_FLOW_KEYWORDS = [
    r"schedul", r"dispatch", r"init", r"handle", r"register",
    r"forward", r"execute", r"run", r"start", r"stop",
    r"alloc", r"free", r"create", r"destroy", r"build",
    r"compute", r"process", r"prepare", r"setup", r"cleanup",
    r"load", r"save", r"parse", r"encode", r"decode",
    r"main", r"entry", r"bootstrap",
]

_CONTROL_FLOW_RE = re.compile(
    "|".join(kw for kw in _CONTROL_FLOW_KEYWORDS),
    re.IGNORECASE
)

# ---------------------------------------------------------------------------
# 功能领域关键词（用于确保冷门功能被覆盖）
# ---------------------------------------------------------------------------
_FUNCTIONAL_DOMAINS = {
    "riscv": ["riscv", "riscv64"],
    "s390x": ["s390x"],
    "metal": ["metal", "mtl"],
    "vulkan": ["vulkan", "vk"],
    "cuda": ["cuda", "cu_"],
    "cpu": ["cpu", "x86", "arm", "neon", "avx", "sse"],
    "sycl": ["sycl", "zendnn"],
    "opencl": ["opencl", "cl_"],
    "wasi": ["wasi", "wasm"],
    "kompute": ["kompute"],
    "blas": ["blas", "cublas", "clblast"],
    "rocm": ["rocm", "hip"],
    "musa": ["musa"],
    "canary": ["canary"],
    "gguf": ["gguf"],
    "quantize": ["quant", "q4", "q5", "q6", "q8", "iq2", "iq3", "iq4"],
    "tokenize": ["token", "vocab", "bpe"],
    "kv_cache": ["kv_cache", "kv_self", "kv_cross"],
    "rope": ["rope", "rotary"],
    "flash_attn": ["flash_attn", "flash_attention", "fa_"],
    "speculative": ["speculative", "draft", "lookahead"],
    "lora": ["lora", "adapter"],
    "server": ["server", "http", "endpoint", "handler"],
    "grammar": ["grammar", "constraint"],
    "sampling": ["sample", "temperature", "top_p", "top_k"],
    "log": ["log", "verbosity", "debug"],
    "backend": ["backend", "device", "buffer"],
    "tensor": ["tensor", "ggml_tensor", "ggml_view"],
    "graph": ["graph", "cgraph", "comput"],
    "model_loader": ["loader", "model_load", "checkpoint"],
}


def _matches_domain(name: str, keywords: list[str]) -> bool:
    """检查函数名是否匹配某个功能领域"""
    name_lower = name.lower()
    for kw in keywords:
        if kw.lower() in name_lower:
            return True
    return False


# ---------------------------------------------------------------------------
# Neo4j 查询
# ---------------------------------------------------------------------------

def fetch_all_functions(driver, database: str) -> list[dict]:
    """
    获取所有函数的基本信息 + 度数 + 所属模块
    返回 list of {
        id, name, file_path, signature,
        in_degree, out_degree,
        module_id, directory
    }
    """
    cypher = """
    MATCH (f:Function)
    OPTIONAL MATCH (f)<-[:CALLS]-(caller:Function)
    OPTIONAL MATCH (f)-[:CALLS]->(callee:Function)
    OPTIONAL MATCH (f)-[:BELONGS_TO]->(m:Module)
    WITH f,
         count(DISTINCT caller) AS in_degree,
         count(DISTINCT callee) AS out_degree,
         m.id AS module_id
    RETURN f.id AS id, f.name AS name, f.file_path AS file_path, f.signature AS signature,
           in_degree, out_degree, module_id
    """
    results = []
    with driver.session(database=database) as s:
        for rec in s.run(cypher):
            fp = rec["file_path"] or ""
            directory = str(Path(fp).parent) if fp else ""
            results.append({
                "id": rec["id"],
                "name": rec["name"],
                "file_path": fp,
                "directory": directory,
                "signature": rec["signature"],
                "in_degree": rec["in_degree"],
                "out_degree": rec["out_degree"],
                "degree": rec["in_degree"] + rec["out_degree"],
                "module_id": rec["module_id"] or "",
            })
    return results


# ---------------------------------------------------------------------------
# 维度1：影响力筛选
# ---------------------------------------------------------------------------

def select_by_influence(funcs: list[dict]) -> set[str]:
    """
    按影响力筛选：
    - Top 200 总度数（in + out）
    - 入口函数（in_degree <= 2，但 out_degree >= 5）
    - 控制流关键函数（名字匹配关键词）
    """
    selected = set()

    # 1. Top 200 总度数
    sorted_by_degree = sorted(funcs, key=lambda x: -x["degree"])
    for f in sorted_by_degree[:200]:
        selected.add(f["id"])

    # 2. 入口函数（被调用很少，但调用别人很多 = 流程起点）
    entry_candidates = [
        f for f in funcs
        if f["in_degree"] <= 2 and f["out_degree"] >= 5
    ]
    # 按 out_degree 排序，取前 100
    entry_candidates.sort(key=lambda x: -x["out_degree"])
    for f in entry_candidates[:100]:
        selected.add(f["id"])

    # 3. 控制流关键函数
    control_flow = [
        f for f in funcs
        if _CONTROL_FLOW_RE.search(f["name"])
    ]
    # 按 degree 排序，取前 150
    control_flow.sort(key=lambda x: -x["degree"])
    for f in control_flow[:150]:
        selected.add(f["id"])

    return selected


# ---------------------------------------------------------------------------
# 维度2：空间覆盖（每目录/每模块）
# ---------------------------------------------------------------------------

def select_by_spatial_coverage(funcs: list[dict]) -> set[str]:
    """
    按空间覆盖筛选：
    - 每个 directory 取度数最高的 2 个
    - 每个 module 取度数最高的 3 个
    """
    selected = set()

    # 按 directory 分组
    by_directory = defaultdict(list)
    for f in funcs:
        by_directory[f["directory"]].append(f)

    for directory, d_funcs in by_directory.items():
        d_funcs.sort(key=lambda x: -x["degree"])
        for f in d_funcs[:2]:
            selected.add(f["id"])

    # 按 module 分组
    by_module = defaultdict(list)
    for f in funcs:
        if f["module_id"]:
            by_module[f["module_id"]].append(f)

    for module_id, m_funcs in by_module.items():
        m_funcs.sort(key=lambda x: -x["degree"])
        for f in m_funcs[:3]:
            selected.add(f["id"])

    return selected


# ---------------------------------------------------------------------------
# 维度3：功能覆盖（按语义领域）
# ---------------------------------------------------------------------------

def select_by_functional_coverage(funcs: list[dict]) -> set[str]:
    """
    按功能领域覆盖筛选：
    - 每个预定义功能领域，取度数最高的 1-2 个函数
    """
    selected = set()

    for domain, keywords in _FUNCTIONAL_DOMAINS.items():
        matched = [f for f in funcs if _matches_domain(f["name"], keywords)]
        if matched:
            matched.sort(key=lambda x: -x["degree"])
            for f in matched[:2]:
                selected.add(f["id"])

    return selected


# ---------------------------------------------------------------------------
# 覆盖率验证
# ---------------------------------------------------------------------------

def validate_coverage(selected_ids: set[str], funcs: list[dict], benchmark_path: Path) -> dict:
    """
    验证 representative functions 对 benchmark 的覆盖率
    """
    id_to_name = {f["id"]: f["name"] for f in funcs}
    selected_names = {id_to_name.get(fid, fid) for fid in selected_ids}

    # 加载 benchmark
    with open(benchmark_path, "r", encoding="utf-8") as f:
        bench = json.load(f)

    questions = bench.get("questions", [])

    # 简单实体提取：从问题中提取函数名/关键词
    import re
    uncovered = []
    for q in questions:
        text = q.get("question", "")
        # 找 camelCase / snake_case 标识符
        entities = re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]{2,}\b", text)
        # 检查是否有实体被 representative 覆盖
        covered = False
        for ent in entities:
            if ent in selected_names:
                covered = True
                break
        if not covered and entities:
            uncovered.append({"question": text[:80], "entities": entities[:5]})

    coverage = (len(questions) - len(uncovered)) / len(questions) * 100 if questions else 0

    return {
        "total_questions": len(questions),
        "uncovered_questions": len(uncovered),
        "coverage_rate": f"{coverage:.1f}%",
        "sample_uncovered": uncovered[:10],
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    driver = get_driver()
    try:
        driver.verify_connectivity()
    except Exception as e:
        print(f"Neo4j connection failed: {e}")
        return 1

    print("Fetching all functions from Neo4j...")
    funcs = fetch_all_functions(driver, NEO4J_DATABASE)
    print(f"Total functions: {len(funcs)}")

    # 三个维度筛选
    print("\n[Dimension 1] Selecting by influence...")
    influence_ids = select_by_influence(funcs)
    print(f"  Selected: {len(influence_ids)}")

    print("\n[Dimension 2] Selecting by spatial coverage...")
    spatial_ids = select_by_spatial_coverage(funcs)
    print(f"  Selected: {len(spatial_ids)}")

    print("\n[Dimension 3] Selecting by functional coverage...")
    functional_ids = select_by_functional_coverage(funcs)
    print(f"  Selected: {len(functional_ids)}")

    # 合并去重
    all_selected = influence_ids | spatial_ids | functional_ids
    print(f"\n[Merge] Total unique representative functions: {len(all_selected)}")

    # 统计各维度贡献
    only_influence = influence_ids - spatial_ids - functional_ids
    only_spatial = spatial_ids - influence_ids - functional_ids
    only_functional = functional_ids - influence_ids - spatial_ids
    print(f"  Only influence: {len(only_influence)}")
    print(f"  Only spatial: {len(only_spatial)}")
    print(f"  Only functional: {len(only_functional)}")
    print(f"  Overlap (influence + spatial): {len(influence_ids & spatial_ids)}")
    print(f"  Overlap (influence + functional): {len(influence_ids & functional_ids)}")
    print(f"  Overlap (spatial + functional): {len(spatial_ids & functional_ids)}")

    # 生成 representative 列表
    id_to_func = {f["id"]: f for f in funcs}
    representatives = []
    for fid in sorted(all_selected):
        f = id_to_func[fid]
        # 标记来源维度
        sources = []
        if fid in influence_ids:
            sources.append("influence")
        if fid in spatial_ids:
            sources.append("spatial")
        if fid in functional_ids:
            sources.append("functional")
        f["selection_source"] = sources
        representatives.append(f)

    # 保存
    output_path = _ROOT / "results" / "representative_functions.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(representatives, f, ensure_ascii=False, indent=2)
    print(f"\nSaved to: {output_path}")

    # 覆盖率验证
    bench_path = _ROOT / "datasets" / "llama_cpp_QA_cleaned.json"
    if bench_path.exists():
        print("\n[Validation] Checking benchmark coverage...")
        coverage = validate_coverage(all_selected, funcs, bench_path)
        print(f"  Total questions: {coverage['total_questions']}")
        print(f"  Uncovered: {coverage['uncovered_questions']}")
        print(f"  Coverage rate: {coverage['coverage_rate']}")
        if coverage['sample_uncovered']:
            print(f"  Sample uncovered questions:")
            for u in coverage['sample_uncovered'][:5]:
                print(f"    - {u['question']}... (entities: {u['entities']})")

    # 存入 Neo4j（标记 is_representative）
    print("\n[Neo4j] Marking representative functions...")
    with driver.session(database=NEO4J_DATABASE) as s:
        # 先清除旧标记
        s.run("MATCH (f:Function) REMOVE f.is_representative, f.selection_source")
        # 批量标记
        for fid in all_selected:
            s.run(
                "MATCH (f:Function {id: $id}) SET f.is_representative = true",
                id=fid
            )
    print("  Done.")

    driver.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
