#!/usr/bin/env python3
"""
QA 流水线：读取 llama_cpp_QA.csv，按题目类型路由，从 Neo4j 检索证据，再调用 LLM 生成答案，输出 JSON（含参考答案与可选评价）。

用法：
  python run_qa.py [--csv PATH] [--limit N] [--output PATH] [--no-llm] [--eval] [--workers N]
  --workers 并行处理的题目数，默认 4；设为 1 则单线程顺序执行

依赖：pandas, neo4j, openai；.env 中配置 NEO4J_*、OPENAI_API_KEY、OPENAI_BASE_URL、LLM_MODEL。
详见 docs/llama_cpp_QA_题目分类与检索策略.md。
"""
import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# 项目根（Code_Graph 的上一级）
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_QA_CSV = ROOT / "llama_cpp_QA.csv"
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "qa_retrieval_results.json"


def _route(row: dict) -> str:
    """按意图/问题类型将题目路由到 A（图直接答）/ B（图+LLM）/ C（embedding+LLM）。"""
    intent = (row.get("意图") or "").strip()
    q_type = (row.get("问题类型") or "").strip().lower()
    question = (row.get("具体问题") or "").strip()

    # 类型 A：调用关系、依赖、功能位置、变量引用 → Cypher 可直接查
    if "Dependency" in intent or "调用" in question or "依赖" in question:
        return "A"
    if "Feature Location" in intent or "Data" in intent or "Control-flow" in intent:
        return "A"
    if "在哪里" in question and ("函数" in question or "代码" in question or "变量" in question):
        return "A"

    # 类型 B：架构、模块关系、流程 → 需 CALLS 展开或 Workflow + LLM 总结
    if "Architecture" in intent or "模块" in question or "流程" in question:
        return "B"
    if "Dependency tracing" in intent and ("关系图" in question or "传递" in question):
        return "B"

    # 类型 C：为什么、设计理由、影响 → 需语义检索 + LLM
    if "Design rationale" in intent or "Purpose" in intent or "Performance" in intent:
        return "C"
    if q_type == "why":
        return "C"

    # 默认：尝试用图做基础检索，再交给 LLM
    return "B"


def _parse_evidence(evidence_str: str) -> set[str]:
    """将 CSV 的 Evidence 列解析为规范化路径集合（逗号分隔，去掉行号等）。"""
    if not evidence_str or not isinstance(evidence_str, str):
        return set()
    out = set()
    for part in evidence_str.split(","):
        s = part.strip().strip('"').replace("\\", "/")
        if not s:
            continue
        if ":" in s and s.rsplit(":", 1)[-1].isdigit():
            s = s.rsplit(":", 1)[0].strip()
        out.add(s)
    return out


def _normalize_path_for_match(p: str) -> str:
    """规范化路径便于与 Evidence 匹配（统一用 /，去掉首尾空白）。"""
    return (p or "").strip().replace("\\", "/").rstrip("/")


def _path_matches(e: str, r: str) -> bool:
    """Evidence 路径 e 与检索路径 r 是否视为同一文件（精确或后缀匹配）。"""
    if not e or not r:
        return False
    if e == r:
        return True
    if r.endswith("/" + e) or r.endswith(e):
        return True
    if e.endswith("/" + r) or e.endswith(r):
        return True
    return False


def _evidence_match(evidence_set: set[str], retrieved_paths: set[str]) -> dict:
    """计算检索涉及文件与 Evidence 的匹配：命中数、召回率、精确率。"""
    evidence_norm = {_normalize_path_for_match(e) for e in evidence_set if e}
    retrieved_norm = {_normalize_path_for_match(r) for r in retrieved_paths if r}
    hit_evidence = set()
    hit_retrieved = set()
    for e in evidence_norm:
        for r in retrieved_norm:
            if _path_matches(e, r):
                hit_evidence.add(e)
                hit_retrieved.add(r)
                break
    n_ev = len(evidence_norm)
    n_ret = len(retrieved_norm)
    recall = len(hit_evidence) / n_ev if n_ev else None
    precision = len(hit_retrieved) / n_ret if n_ret else None
    return {
        "Evidence文件数": n_ev,
        "检索涉及文件数": n_ret,
        "命中Evidence文件数": len(hit_evidence),
        "证据召回率": round(recall, 4) if recall is not None else None,
        "证据精确率": round(precision, 4) if precision is not None else None,
        "命中的Evidence文件": sorted(hit_evidence)[:30],
    }


def _normalize_entity(entity: str) -> str:
    """将实体名称转为图中可用的 file_path 或 name 片段。"""
    if not entity:
        return ""
    e = entity.strip()
    # 若实体是文件名或路径片段，去掉可能的前缀，保留可匹配部分
    if e.endswith(".cpp") or e.endswith(".h") or "/" in e:
        return e.replace("\\", "/")
    return e


def _get_file_paths_for_names(driver, names: set[str], database: str) -> set[str]:
    """根据函数名集合查 Neo4j 得到涉及的 file_path 集合。"""
    if not names:
        return set()
    with driver.session(database=database) as session:
        r = session.run(
            """
            MATCH (f:Function)
            WHERE f.name IN $names
            RETURN DISTINCT f.file_path AS file_path
            """,
            names=list(names),
        )
        return {rec["file_path"] for rec in r if rec.get("file_path")}


def _query_type_a(driver, entity: str, intent: str, question: str, database: str) -> tuple[str, set[str]]:
    """类型 A：Cypher 检索——某路径/模块下的函数、或与实体相关的调用关系。返回 (检索文本, 涉及函数名集合)。"""
    entity_norm = _normalize_entity(entity)
    if not entity_norm:
        return "（未提供实体名称，跳过检索）", set()

    path_part = entity_norm if "/" in entity_norm or entity_norm.endswith(".cpp") or entity_norm.endswith(".h") else entity_norm
    lines = []
    names_involved: set[str] = set()
    with driver.session(database=database) as session:
        r1 = session.run(
            """
            MATCH (f:Function)
            WHERE f.file_path CONTAINS $path OR f.name CONTAINS $path
            RETURN f.name AS name, f.file_path AS file_path
            LIMIT 30
            """,
            path=path_part,
        )
        funcs = [(r["name"], r["file_path"]) for r in r1]
        for name, _ in funcs:
            names_involved.add(name)
        if funcs:
            lines.append("相关函数（file_path/name 含实体）:")
            for name, fp in funcs[:15]:
                lines.append(f"  - {name} @ {fp}")
            if len(funcs) > 15:
                lines.append(f"  ... 共 {len(funcs)} 个")

        if funcs:
            names = [f[0] for f in funcs[:20]]
            r2 = session.run(
                """
                MATCH (a:Function)-[r:CALLS]->(b:Function)
                WHERE a.name IN $names OR b.name IN $names
                RETURN a.name AS caller, b.name AS callee
                LIMIT 20
                """,
                names=names,
            )
            calls = [(r["caller"], r["callee"]) for r in r2]
            for c, e in calls:
                names_involved.add(c)
                names_involved.add(e)
            if calls:
                lines.append("调用关系（与上述函数相关）:")
                for c, e in calls[:10]:
                    lines.append(f"  {c} -> {e}")
                if len(calls) > 10:
                    lines.append(f"  ... 共 {len(calls)} 条")

    text = "\n".join(lines) if lines else "（图中未命中该实体相关节点）"
    return text, names_involved


# 类型 B：流程起点 BFS 的深度与节点上限
_WORKFLOW_DEPTH_LIMIT = 5
_WORKFLOW_NODE_LIMIT = 400


def _get_flow_start_candidates(driver, entity: str, database: str) -> list[tuple[str, str]]:
    """取与实体匹配的「流程起点」：CALLS 入度 0、出度≥1 的 Function，返回 (name, file_path)。"""
    entity_norm = _normalize_entity(entity)
    if not entity_norm:
        return []
    path_part = (
        entity_norm
        if "/" in entity_norm or entity_norm.endswith(".cpp") or entity_norm.endswith(".h")
        else entity_norm
    )
    with driver.session(database=database) as session:
        r = session.run(
            """
            MATCH (f:Function)
            WHERE (f.file_path CONTAINS $path OR f.name CONTAINS $path)
              AND NOT ()-[:CALLS]->(f)
              AND (f)-[:CALLS]->()
            RETURN f.name AS name, f.file_path AS file_path
            LIMIT 50
            """,
            path=path_part,
        )
        return [(rec["name"], rec["file_path"]) for rec in r]


def _agent_select_flow_starts(question: str, candidates: list[tuple[str, str]], max_pick: int = 3) -> list[str]:
    """用 LLM 从候选流程起点中选出与问题最相关的 1～max_pick 个函数名。"""
    from openai import OpenAI

    from config import LLM_MODEL, OPENAI_API_KEY, OPENAI_BASE_URL

    if not OPENAI_API_KEY or not candidates or max_pick <= 0:
        return [candidates[0][0]] if candidates else []
    if len(candidates) <= max_pick:
        return [c[0] for c in candidates]

    cand_text = "\n".join(f"  - {name} @ {fp}" for name, fp in candidates[:30])
    client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL if OPENAI_BASE_URL else None)
    prompt = f"""以下是与某实体相关的「流程起点」函数（无人调用它们，它们会调用别人）。请根据用户问题，选出最可能与该问题相关的 1～{max_pick} 个函数名。只输出函数名，每行一个，不要其他内容。

【用户问题】
{question[:400]}

【候选流程起点】
{cand_text}

【输出的函数名】（每行一个）
"""

    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=150,
        )
        if not resp.choices or not resp.choices[0].message.content:
            return [candidates[0][0]]
        text = resp.choices[0].message.content.strip()
        names_from_llm = [line.strip() for line in text.split("\n") if line.strip()][:max_pick]
        cand_names = {c[0] for c in candidates}
        selected = [n for n in names_from_llm if n in cand_names]
        return selected if selected else [candidates[0][0]]
    except Exception:
        return [candidates[0][0]]


def _expand_calls_bfs(driver, seed_names: list[str], database: str) -> tuple[list[tuple[str, str]], set[str]]:
    """从种子函数出发，沿 CALLS 有向 BFS 直到深度/节点数上限，返回 (边列表去重, 节点名集合)。"""
    edges_set: set[tuple[str, str]] = set()
    nodes: set[str] = set(seed_names)
    frontier = list(seed_names)
    depth = 0

    with driver.session(database=database) as session:
        while depth < _WORKFLOW_DEPTH_LIMIT and len(nodes) < _WORKFLOW_NODE_LIMIT and frontier:
            r = session.run(
                """
                MATCH (a:Function)-[:CALLS]->(b:Function)
                WHERE a.name IN $frontier
                RETURN a.name AS caller, b.name AS callee
                """,
                frontier=frontier,
            )
            next_frontier = set()
            for rec in r:
                caller, callee = rec["caller"], rec["callee"]
                if caller != callee:
                    edges_set.add((caller, callee))
                    nodes.add(callee)
                    next_frontier.add(callee)
            frontier = list(next_frontier)
            if not frontier:
                break
            depth += 1
            if len(nodes) >= _WORKFLOW_NODE_LIMIT:
                break

    return sorted(edges_set), nodes


def _query_type_b(driver, entity: str, question: str, database: str) -> tuple[str, set[str]]:
    """类型 B：由 agent 选出正确的流程起点，再沿 CALLS 一直往下 BFS 召回整条流程子图。返回 (检索文本, 涉及函数名集合)。"""
    entity_norm = _normalize_entity(entity)
    if not entity_norm:
        return "（未提供实体名称，跳过检索）", set()

    candidates = _get_flow_start_candidates(driver, entity, database)
    if not candidates:
        return "（图中未找到与实体匹配的流程起点：无 CALLS 入边且至少有一条 CALLS 出边的函数）", set()

    seeds = _agent_select_flow_starts(question, candidates, max_pick=3)
    edges, nodes = _expand_calls_bfs(driver, seeds, database)

    lines = [
        "agent 选中的流程起点:",
    ]
    for name in seeds:
        fp = next((fp for n, fp in candidates if n == name), "")
        lines.append(f"  - {name} @ {fp}")
    lines.append("")
    lines.append(f"沿 CALLS 向下展开（深度≤{_WORKFLOW_DEPTH_LIMIT}，节点数≤{_WORKFLOW_NODE_LIMIT}），共 {len(nodes)} 个节点、{len(edges)} 条调用边:")
    for i, (c, e) in enumerate(edges):
        if i >= 200:
            lines.append(f"  ... 共 {len(edges)} 条边")
            break
        lines.append(f"  {c} -> {e}")
    return "\n".join(lines), nodes


def _embed_texts(client, model: str, texts: list[str]) -> list[list[float]]:
    """调用 OpenAI 兼容的 embedding API，返回向量列表。单次最多 20 条避免超长。"""
    out = []
    for i in range(0, len(texts), 20):
        batch = texts[i : i + 20]
        resp = client.embeddings.create(model=model, input=batch)
        for e in sorted(resp.data, key=lambda x: x.index):
            out.append(e.embedding)
    return out


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """余弦相似度。"""
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _query_type_c(driver, entity: str, question: str, database: str) -> tuple[str, set[str]]:
    """类型 C：按实体取相关函数，用问题 embedding 做相似度排序取 top-k，再查其 CALLS 邻域。返回 (检索文本, 涉及函数名集合)。"""
    from openai import OpenAI

    from config import EMBEDDING_MODEL, OPENAI_API_KEY, OPENAI_BASE_URL

    entity_norm = _normalize_entity(entity)
    if not entity_norm:
        return "（未提供实体名称，跳过检索）", set()
    if not OPENAI_API_KEY:
        return "（未配置 OPENAI_API_KEY，无法做 embedding 检索）", set()

    path_part = (
        entity_norm
        if "/" in entity_norm or entity_norm.endswith(".cpp") or entity_norm.endswith(".h")
        else entity_norm
    )
    with driver.session(database=database) as session:
        r0 = session.run(
            """
            MATCH (f:Function)
            WHERE f.file_path CONTAINS $path OR f.name CONTAINS $path
            RETURN f.name AS name, f.file_path AS file_path
            LIMIT 50
            """,
            path=path_part,
        )
        funcs = [(r["name"], r["file_path"]) for r in r0]
    if not funcs:
        return "（图中未命中该实体相关节点）", set()

    client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL if OPENAI_BASE_URL else None)
    texts = [f"{name} | {fp}" for name, fp in funcs]
    try:
        q_emb = _embed_texts(client, EMBEDDING_MODEL, [question])[0]
        func_embs = _embed_texts(client, EMBEDDING_MODEL, texts)
    except Exception as e:
        return f"（embedding 调用失败: {e}）", set()

    scored = [(i, _cosine_similarity(q_emb, func_embs[i])) for i in range(len(funcs))]
    scored.sort(key=lambda x: -x[1])
    top_indices = [i for i, _ in scored[:10]]
    top_names = [funcs[i][0] for i in top_indices]
    names_involved: set[str] = set(top_names)

    lines = [
        "与问题语义最相关的函数（embedding 相似度 top-10）:",
    ]
    for idx, sim in scored[:10]:
        name, fp = funcs[idx]
        lines.append(f"  - {name} @ {fp} (相似度 {sim:.3f})")
    lines.append("")

    with driver.session(database=database) as session:
        r2 = session.run(
            """
            MATCH (a:Function)-[:CALLS]->(b:Function)
            WHERE a.name IN $names OR b.name IN $names
            RETURN a.name AS caller, b.name AS callee
            LIMIT 30
            """,
            names=top_names,
        )
        calls = [(r["caller"], r["callee"]) for r in r2]
    for c, e in calls:
        names_involved.add(c)
        names_involved.add(e)
    if calls:
        lines.append("上述函数的调用关系:")
        for c, e in calls[:20]:
            lines.append(f"  {c} -> {e}")
        if len(calls) > 20:
            lines.append(f"  ... 共 {len(calls)} 条")
    return "\n".join(lines), names_involved


# 发给 LLM 的检索内容最大长度，避免超长
_LLM_RETRIEVAL_MAX_CHARS = 4000


def _generate_answer_with_llm(question: str, retrieval: str) -> str:
    """用 OpenAI 兼容 API 根据「问题 + 检索结果」生成答案。"""
    from openai import OpenAI

    from config import LLM_MODEL, OPENAI_API_KEY, OPENAI_BASE_URL

    if not OPENAI_API_KEY:
        return "（未配置 OPENAI_API_KEY，跳过 LLM）"

    retrieval_for_prompt = (retrieval or "（无检索结果）").strip()
    if len(retrieval_for_prompt) > _LLM_RETRIEVAL_MAX_CHARS:
        retrieval_for_prompt = retrieval_for_prompt[:_LLM_RETRIEVAL_MAX_CHARS] + "\n...（已截断）"

    client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL if OPENAI_BASE_URL else None)
    prompt = f"""你是一个基于代码图回答仓库级问题的助手。下面给出「用户问题」和「从代码图中检索到的证据」（函数、文件路径、调用关系等）。请仅根据检索证据用中文简洁回答问题；若证据不足，请明确说明并基于已有信息尽量推断。不要编造图中不存在的函数或调用关系。

【用户问题】
{question}

【代码图检索结果】
{retrieval_for_prompt}

【你的回答】
"""

    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1500,
        )
        if resp.choices and resp.choices[0].message.content:
            return resp.choices[0].message.content.strip()
        return "（LLM 未返回内容）"
    except Exception as e:
        return f"（LLM 调用失败: {e}）"


def _evaluate_answer(question: str, reference: str, generated: str) -> tuple[float | None, str]:
    """用 LLM 对生成答案打分（0-1），并返回一句评价说明。返回 (score, explanation)。"""
    import re
    from openai import OpenAI

    from config import LLM_MODEL, OPENAI_API_KEY, OPENAI_BASE_URL

    if not OPENAI_API_KEY:
        return None, "（未配置 OPENAI_API_KEY，跳过评价）"

    ref = (reference or "").strip()[:2500]
    gen = (generated or "").strip()[:2500]

    client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL if OPENAI_BASE_URL else None)
    prompt = f"""请对比下面「参考答案」与「生成答案」，给出一个 0 到 1 之间的分数，以及一句简短理由。
- 1 表示生成答案与参考答案一致或高度互补、覆盖关键信息、无错误；
- 0 表示完全无关、严重错误或关键信息完全遗漏；
- 0.3~0.7 表示部分一致或部分覆盖。
你必须先单独一行输出：分数: 0.xx（例如 分数: 0.75），然后换行写一句简短理由。不要输出其他格式。

【问题】
{question[:300]}

【参考答案】（截取部分）
{ref}

【生成答案】
{gen}

【你的输出】（第一行必须是「分数: 0.xx」，第二行起为理由）
"""

    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
        )
        if not resp.choices or not resp.choices[0].message.content:
            return None, "（LLM 未返回评价）"
        text = resp.choices[0].message.content.strip()
        # 解析 "分数: 0.xx" 或 "分数：0.xx"
        match = re.search(r"分数\s*[:：]\s*(0?\.\d+|1\.0?|1)\b", text)
        score = None
        if match:
            try:
                score = float(match.group(1))
                score = max(0.0, min(1.0, score))
            except ValueError:
                pass
        # 理由：去掉第一行「分数: 0.xx」后的内容
        lines = text.split("\n")
        explanation = text
        for i, line in enumerate(lines):
            if re.search(r"分数\s*[:：]\s*", line):
                rest = "\n".join(lines[i + 1 :]).strip()
                if rest:
                    explanation = rest
                break
        return score, explanation
    except Exception as e:
        return None, f"（评价失败: {e}）"


def _process_one_row(
    idx: int,
    row_dict: dict,
    driver,
    database: str,
    no_llm: bool,
    do_eval: bool,
) -> dict:
    """处理单行题目：路由、检索、证据匹配、生成答案、可选评价。供并行调用。"""
    question = row_dict.get("具体问题", "")
    intent = row_dict.get("意图", "")
    entity = row_dict.get("实体名称", "")
    reference_answer = row_dict.get("答案", "")
    evidence_raw = row_dict.get("Evidence", "")
    route = _route(row_dict)

    if route == "A":
        retrieval, names_involved = _query_type_a(driver, entity, intent, question, database)
    elif route == "B":
        retrieval, names_involved = _query_type_b(driver, entity, question, database)
    else:
        retrieval, names_involved = _query_type_c(driver, entity, question, database)

    retrieved_paths = _get_file_paths_for_names(driver, names_involved, database)
    evidence_set = _parse_evidence(evidence_raw)
    evidence_match = _evidence_match(evidence_set, retrieved_paths)

    if no_llm:
        generated_answer = (retrieval[:500] if retrieval else "")
    else:
        generated_answer = _generate_answer_with_llm(question, retrieval)

    item = {
        "index": int(idx),
        "具体问题": question,
        "意图": intent,
        "实体名称": entity,
        "路由类型": route,
        "检索结果": retrieval,
        "参考答案": reference_answer,
        "生成答案": generated_answer,
        "Evidence": evidence_raw,
        "证据匹配": evidence_match,
    }
    if do_eval:
        eval_score, eval_explanation = _evaluate_answer(
            question, reference_answer, generated_answer
        )
        item["评价分数"] = eval_score
        item["评价说明"] = eval_explanation
    return item


def main():
    parser = argparse.ArgumentParser(description="QA 流水线：读 CSV，路由，查 Neo4j，输出检索结果")
    parser.add_argument("--csv", type=Path, default=DEFAULT_QA_CSV, help="QA CSV 路径")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 条，0 表示全部")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="输出路径，默认 JSON")
    parser.add_argument("--no-llm", action="store_true", help="仅检索，不调用 LLM")
    parser.add_argument("--eval", action="store_true", help="对每题用 LLM 评价生成答案与参考答案，写入 评价 字段")
    parser.add_argument("--workers", type=int, default=4, help="并行题目数，默认 4；设为 1 则顺序执行")
    args = parser.parse_args()

    if not args.csv.exists():
        print(f"错误：未找到 {args.csv}", file=sys.stderr)
        sys.exit(1)

    try:
        import pandas as pd
    except ImportError:
        print("请安装 pandas: pip install pandas", file=sys.stderr)
        sys.exit(1)

    # 加载配置与 Neo4j（与 run_stage1 一致）
    from config import NEO4J_DATABASE
    from neo4j_writer import get_driver

    df = pd.read_csv(args.csv, encoding="utf-8")
    if args.limit > 0:
        df = df.head(args.limit)

    driver = get_driver()
    try:
        driver.verify_connectivity()
    except Exception as e:
        print(f"Neo4j 连接失败: {e}", file=sys.stderr)
        sys.exit(1)

    rows = [(int(idx), row.to_dict()) for idx, row in df.iterrows()]
    workers = max(1, int(args.workers))

    if workers <= 1:
        results = [
            _process_one_row(idx, row_dict, driver, NEO4J_DATABASE, args.no_llm, args.eval)
            for idx, row_dict in rows
        ]
    else:
        by_index = {}
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    _process_one_row,
                    idx,
                    row_dict,
                    driver,
                    NEO4J_DATABASE,
                    args.no_llm,
                    args.eval,
                ): idx
                for idx, row_dict in rows
            }
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    item = future.result()
                    by_index[idx] = item
                except Exception as e:
                    print(f"题目 {idx} 处理异常: {e}", file=sys.stderr)
                    row_dict = next((r[1] for r in rows if r[0] == idx), {})
                    by_index[idx] = {
                        "index": idx,
                        "具体问题": row_dict.get("具体问题", ""),
                        "错误": str(e),
                    }
        results = [by_index[idx] for idx, _ in rows]

    driver.close()

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"已写入 {len(results)} 条到 {args.output}")


if __name__ == "__main__":
    main()
