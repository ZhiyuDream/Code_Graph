#!/usr/bin/env python3
"""Concept-Symbol + Embedding 初始探索 + ReAct 逐步扩展 QA。

流程：
1. Concept-Symbol retrieval 得到 top-K 准确函数位置（name, file_path, start_line, end_line）
2. ReAct Agent 从这些准确位置开始：
   - read_function 读取函数实现
   - grep_callers / grep_callees 沿调用链扩展
   - read_file / read_lines 深入阅读相关文件
   - finish 结束调查
3. 生成最终答案

核心改进：初始函数位置来自 Concept-Symbol 准确索引，不是 Agent 猜测，确保 read_function 成功率。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from config import LLM_MODEL, REPO_ROOT
from src.core.llm_client import call_llm, call_llm_json
from src.core.concept_abstraction import ConceptAbstraction
from src.core.module_abstraction import ModuleAbstraction
from src.qa.retrievers.fast_embedding import FastEmbeddingRetriever
from src.qa.tools.grep_call_chain import grep_files, grep_callers, grep_callees, read_function
from scripts.analysis.eval_region_compression import (
    fetch_functions_and_calls,
    load_benchmark,
    load_embedding_index,
    build_helpers,
)


# ── Concept-Symbol Retrieval ────────────────────────────────────────

def extract_symbols(question: str) -> list:
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", question)
    stopwords = {
        "how", "what", "where", "when", "why", "is", "are", "does", "do", "did",
        "the", "a", "an", "and", "or", "in", "on", "at", "to", "for", "of",
        "with", "by", "from", "as", "it", "its", "this", "that", "these", "those",
    }
    symbols = []
    for t in tokens:
        if len(t) < 3 or t.lower() in stopwords:
            continue
        symbols.append(t.lower())
        parts = re.findall(r"[A-Z][a-z]+|[a-z]+|[A-Z]+", t)
        if len(parts) > 1:
            for p in parts:
                if len(p) >= 3 and p.lower() not in stopwords:
                    symbols.append(p.lower())
    return list(set(symbols))


def symbol_score(fid: str, symbols: list, chunks_by_id: dict) -> float:
    if not symbols:
        return 0.0
    meta = chunks_by_id.get(fid, {}).get("meta", {})
    name = meta.get("name", "").lower()
    file_path = meta.get("file_path", "").lower()
    signature = meta.get("signature", "").lower()
    text = chunks_by_id.get(fid, {}).get("text", "").lower()
    text_all = f"{name} {file_path} {signature} {text}"
    matched = 0
    for sym in symbols:
        if sym in name:
            matched += 2.0
        elif sym in text_all:
            matched += 1.0
    return matched / (len(symbols) * 2.0)


def concept_symbol_retrieve_functions(
    question: str,
    q_emb: np.ndarray,
    ca: ConceptAbstraction,
    retriever: FastEmbeddingRetriever,
    chunks_by_id: dict,
    top_concepts: int = 20,
    top_funcs: int = 20,
) -> list[dict]:
    """Retrieve top functions with accurate positions."""
    symbols = extract_symbols(question)
    concepts = ca.retrieve_concepts(
        q_emb, retriever.doc_matrix, retriever.chunk_index_by_id, top_k=top_concepts, scoring="max"
    )

    candidate_fids = set()
    for c, _ in concepts:
        candidate_fids.update(c.function_ids)

    scored = []
    for fid in candidate_fids:
        idx = retriever.chunk_index_by_id.get(fid)
        if idx is None:
            continue
        sim = float(retriever.doc_matrix[idx] @ q_emb)
        sym = symbol_score(fid, symbols, chunks_by_id)
        score = sim + 0.5 * sym
        scored.append((fid, score))

    scored.sort(key=lambda x: -x[1])

    # 转换为带准确位置的函数信息
    functions = []
    for fid, score in scored[:top_funcs]:
        meta = chunks_by_id.get(fid, {}).get("meta", {})
        functions.append({
            "fid": fid,
            "name": meta.get("name", ""),
            "file_path": meta.get("file_path", ""),
            "start_line": meta.get("start_line", 0),
            "end_line": meta.get("end_line", 0),
            "signature": meta.get("signature", ""),
            "score": score,
        })
    return functions


# ── ReAct Agent ─────────────────────────────────────────────────────

def get_repo_structure(repo_root: Path, max_depth: int = 3) -> str:
    """获取仓库目录结构，帮助 Agent 像人类一样先浏览目录再决定探索方向。"""
    lines = []
    for depth in range(1, max_depth + 1):
        for path in sorted(repo_root.rglob("*")):
            if path.is_dir():
                rel = path.relative_to(repo_root)
                parts = rel.parts
                if len(parts) == depth:
                    indent = "  " * (depth - 1)
                    code_files = list(path.glob("*.cpp")) + list(path.glob("*.c")) + list(path.glob("*.h")) + list(path.glob("*.hpp"))
                    lines.append(f"{indent}{path.name}/ ({len(code_files)} code files)")
    return "\n".join(lines[:100])


REACT_PROMPT = """你是一位代码审计专家。检索系统已经为你找到了一批**准确的函数位置**，你需要从这些函数开始，逐步扩展构建证据链。

【当前问题】
{question}

【仓库目录结构】（帮助你像人类一样先浏览目录，决定探索方向）
{repo_structure}

【检索系统提供的候选函数】（按相关性排序，文件路径准确）
{candidate_functions}

【你已访问的文件】
{visited_files}

【你已访问的目录及次数】
{visited_dirs}

【你重复读取的文件及次数】（提醒你不要陷入循环）
{read_count}

【目录语义提示】
- `common/`：通用工具函数（聊天模板、参数解析、采样、字符串处理等）
- `src/`：核心模型代码（模型加载、推理、上下文管理等）
- `ggml/`：底层计算图和后端（各种硬件后端如 SYCL/CUDA/CANN/Vulkan 等）
- `tests/`：测试代码（通常包含函数的使用示例）
- `tools/`：工具代码（服务器、分析工具等）
- `examples/`：示例代码

【你已执行过的动作】
{action_history}

---

你可以使用以下工具：

1. **read_function(function_name, file_path)** — 直接读取某个函数的完整实现（文件路径必须来自候选函数或 search_symbol 结果）
2. **read_file(file_path)** — 读取完整文件内容
3. **read_lines(file_path, start_line, end_line)** — 读取文件的指定行号范围
4. **grep_callers(function_name)** — 用 grep 搜索调用该函数的所有位置（找谁调用了它）
5. **grep_callees(function_name, file_path, start_line, end_line)** — 从函数定义中提取被调用的函数（找它调用了谁）
6. **search_symbol(symbol_name)** — 搜索仓库中包含该符号的所有文件
7. **finish(reason)** — 认为已有足够证据，结束调查并生成答案

---

【决策规则】
1. **先看目录结构**：根据仓库目录结构和目录语义提示，判断哪个目录最可能包含相关实现
2. **从候选函数开始**：优先用 read_function 读取候选函数的实现，不要一开始就 search_symbol
3. **文件路径必须准确**：read_function 的 file_path 必须来自候选函数或 search_symbol 结果，不要猜测
4. **根据中文问题语义猜测函数名**：不要机械提取英文标识符，而是根据问题语义猜测可能的函数名/类名/变量名，然后用 search_symbol 验证
5. **打转就换**：如果你一直在同一个文件或目录打转，连续 2-3 步没有获得新信息，立即换其他文件或目录，不要硬撑
6. **找到相关文件必须 read**：如果 search_symbol 或 grep_callers 返回了相关文件，下一步必须 read_file 或 read_function 读取它，不要只是知道它存在就跳过
7. **优先使用调用链扩展**：当你发现一个关键函数时，优先用 grep_callers 找它的调用方，或用 grep_callees 找它调用的函数
8. **同文件扩展**：当你读一个函数时，注意观察同文件其他相关函数，可以顺便查看
9. **避免重复**：不要重复读同一个文件超过 2 次；如果已经读过，换其他文件或工具
10. **search_symbol 技巧**：从问题语义中猜测可能的函数名，不要搜空字符串或过于宽泛的词
11. **每次只选择一个工具**，给出明确的理由
12. **最多 {max_steps} 步**，当前第 {current_step} 步
13. **不需要读完所有候选文件**，只要证据充分就可以停止
14. **诚实原则（最重要）**：
    - 你只能引用你实际访问过的文件和函数
    - 如果工具返回空或找不到，如实说明"无法确认"
    - 绝对不要编造文件路径、函数名、代码内容或调用关系
    - 如果没有访问到任何相关文件，finish 时必须说"无法确认"
    - 参考文件清单只能包含你实际 read_file / read_lines / read_function 访问过的文件

【输出格式】（必须是有效的 JSON）
{{
  "thought": "你的思考过程...",
  "action": "read_function|read_file|read_lines|grep_callers|grep_callees|search_symbol|finish",
  "action_input": {{"参数名": "参数值"}},
  "reason": "为什么选择这个行动"
}}
"""

ANSWER_PROMPT = """基于你的调查过程和收集到的证据，回答问题。

【原始问题】
{question}

【调查过程】
{investigation_log}

【你实际访问过的文件及内容】
{files_content}

---

请生成最终答案，必须严格遵守以下规则：

1. **只能引用你实际访问过的文件**：参考文件清单只能包含上面"你实际访问过的文件及内容"中列出的文件
2. **只能引用你实际读到的函数**：不要提及你没有读过具体实现的函数
3. **如果没有访问到相关文件**：如实回答"无法确认"，并说明为什么没有找到证据
4. **不要编造**：绝对不要虚构文件路径、函数名、代码内容或调用关系
5. 引用格式：`file.cpp:start-end`
6. 答案末尾列出参考文件清单，清单中的文件必须是你实际访问过的

请用中文回答：
"""


class ReactAgent:
    def __init__(self, repo_root: Path, max_steps: int = 25):
        self.repo_root = repo_root
        self.max_steps = max_steps
        self.visited_files = set()
        self.file_cache = {}
        self.action_history = []
        self.read_count = defaultdict(int)
        self.visited_dirs = defaultdict(int)
        # 预先计算仓库目录结构，像人类一样先浏览目录
        self.repo_structure = get_repo_structure(repo_root, max_depth=3)

    def _read_file(self, file_path: str) -> str:
        if file_path not in self.file_cache:
            abs_path = self.repo_root / file_path
            try:
                with open(abs_path, encoding="utf-8", errors="replace") as f:
                    self.file_cache[file_path] = f.read()
            except Exception:
                self.file_cache[file_path] = ""
        return self.file_cache[file_path]

    def _get_file_functions(self, file_path: str) -> list[str]:
        content = self._read_file(file_path)
        if not content:
            return []
        pattern = re.compile(r"^\s*(?:[\w:<>]+\s+)*?([a-zA-Z_][a-zA-Z0-9_:]*)\s*\([^)]*\)\s*(?:const)?\s*\{")
        functions = []
        for line in content.split("\n"):
            m = pattern.match(line)
            if m:
                functions.append(m.group(1))
        return functions[:20]

    def execute(self, action: str, action_input: dict) -> tuple[str, list]:
        observation = ""
        new_files = []

        if action == "read_file":
            file_path = action_input.get("file_path", "")
            if not file_path:
                observation = "错误：read_file 缺少 file_path 参数"
                new_files = []
            else:
                content = self._read_file(file_path)
                self.visited_files.add(file_path)
                self.visited_dirs[str(Path(file_path).parent)] += 1
                observation = f"文件 {file_path} 内容:\n```cpp\n{content[:5000]}\n```"
                if len(content) > 5000:
                    observation += f"\n... (截断，共 {len(content)} 字符)"
                new_files = [file_path]

        elif action == "read_lines":
            file_path = action_input.get("file_path", "")
            start = action_input.get("start_line", 1)
            end = action_input.get("end_line", start + 50)
            if not file_path:
                observation = "错误：read_lines 缺少 file_path 参数"
                new_files = []
            else:
                content = self._read_file(file_path)
                lines = content.split("\n")
                s = max(0, start - 1)
                e = min(len(lines), end)
                snippet = "\n".join(lines[s:e])
                self.visited_files.add(file_path)
                self.visited_dirs[str(Path(file_path).parent)] += 1
                observation = f"文件 {file_path} 第 {start}-{end} 行:\n```cpp\n{snippet}\n```"
                new_files = [file_path]

        elif action == "read_function":
            func_name = action_input.get("function_name", "")
            file_path = action_input.get("file_path", "")
            # 清理文件路径：去掉行号部分（如 common/chat.cpp:2133-2240 → common/chat.cpp）
            if ":" in file_path:
                file_path = file_path.split(":")[0]
            if not func_name:
                observation = "错误：read_function 缺少 function_name 参数"
                new_files = []
            elif not file_path:
                observation = "错误：read_function 缺少 file_path 参数"
                new_files = []
            else:
                result = read_function(func_name, file_path, self.repo_root)
                if "error" in result:
                    observation = f"读取函数 {func_name} 失败: {result['error']}"
                    new_files = []
                else:
                    self.visited_files.add(file_path)
                    self.visited_dirs[str(Path(file_path).parent)] += 1
                    other_funcs = self._get_file_functions(file_path)
                    other_funcs = [f for f in other_funcs if f != func_name][:10]
                    observation = f"函数 {func_name} ({file_path}:{result['start_line']}-{result['end_line']}):\n```cpp\n{result['code']}\n```"
                    if other_funcs:
                        observation += f"\n\n同文件其他函数（可顺便查看）: {', '.join(other_funcs)}"
                    new_files = [file_path]

        elif action == "grep_callers":
            func_name = action_input.get("function_name", "")
            if not func_name:
                observation = "错误：grep_callers 缺少 function_name 参数"
                new_files = []
            else:
                callers = grep_callers(func_name, self.repo_root, limit=10)
                files = list(set(c["file"] for c in callers))
                observation = f"找到 {len(callers)} 个调用 '{func_name}' 的位置:\n" + "\n".join(
                    f"- {c['file']}:{c['line']}: {c['content'][:80]}" for c in callers[:10]
                )
                new_files = files

        elif action == "grep_callees":
            func_name = action_input.get("function_name", "")
            file_path = action_input.get("file_path", "")
            start = action_input.get("start_line", 1)
            end = action_input.get("end_line", start + 50)
            if not func_name:
                observation = "错误：grep_callees 缺少 function_name 参数"
                new_files = []
            elif not file_path:
                observation = "错误：grep_callees 缺少 file_path 参数"
                new_files = []
            else:
                callees = grep_callees(func_name, file_path, start, end, self.repo_root, limit=10)
                observation = f"函数 '{func_name}' 调用了:\n" + "\n".join(
                    f"- {c['name']} @ {c['file']}" for c in callees[:10]
                )
                new_files = [c["file"] for c in callees]

        elif action == "search_symbol":
            symbol = action_input.get("symbol_name", "").strip()
            if not symbol:
                observation = "搜索符号为空，跳过"
                new_files = []
            else:
                files = grep_files(rf"\b{re.escape(symbol)}\b", self.repo_root, limit=10)
                observation = f"找到 {len(files)} 个文件包含 '{symbol}':\n" + "\n".join(f"- {f}" for f in files)
                new_files = files

        elif action == "finish":
            reason = action_input.get("reason", "证据充分")
            observation = f"结束调查: {reason}"

        else:
            observation = f"未知工具: {action}"

        return observation, new_files

    def investigate(self, question: str, initial_functions: list, model: str) -> dict:
        steps = []
        files_content = {}

        # 构建候选函数字符串
        candidate_functions = "\n".join(
            f"- {f['name']} @ {f['file_path']}:{f['start_line']}-{f['end_line']} (score: {f['score']:.3f})"
            for f in initial_functions[:20]
        ) or "(无)"

        for step_num in range(1, self.max_steps + 1):
            action_history = "\n".join(
                f"Step {s['step']}: {s['action']}({json.dumps(s['action_input'], ensure_ascii=False)[:50]})"
                for s in steps[-5:]
            ) or "(无)"

            visited_dirs_str = "\n".join(
                f"- {d} (访问 {c} 次)" for d, c in sorted(self.visited_dirs.items(), key=lambda x: -x[1])
            ) or "(无)"

            read_count_str = "\n".join(
                f"- {f} (已读 {c} 次)" for f, c in sorted(self.read_count.items(), key=lambda x: -x[1]) if c > 1
            ) or "(无)"

            prompt = REACT_PROMPT.format(
                question=question,
                repo_structure=self.repo_structure,
                candidate_functions=candidate_functions,
                visited_files="\n".join(f"- {f}" for f in sorted(self.visited_files)) or "(无)",
                visited_dirs=visited_dirs_str,
                read_count=read_count_str,
                action_history=action_history,
                max_steps=self.max_steps,
                current_step=step_num,
            )

            decision = call_llm_json(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=None,
                model=model,
            )
            if decision is None:
                decision = {"thought": "决策失败", "action": "finish", "action_input": {"reason": "决策失败"}, "reason": "出错停止"}

            action = decision.get("action", "finish")
            action_input = decision.get("action_input", {})
            thought = decision.get("thought", "")
            reason = decision.get("reason", "")

            # 强制防循环：如果 read_file/read_lines/read_function 同一个文件超过 2 次，强制 finish
            if action in ("read_file", "read_lines", "read_function"):
                file_path = action_input.get("file_path", "")
                if ":" in file_path:
                    file_path = file_path.split(":")[0]
                self.read_count[file_path] += 1
                if self.read_count[file_path] > 2:
                    action = "finish"
                    action_input = {"reason": f"检测到重复读取 {file_path} 超过 2 次，强制结束调查防止循环"}
                    thought = f"检测到重复读取 {file_path}，强制结束调查"
                    reason = "强制防循环"

            observation, new_files = self.execute(action, action_input)

            # 记录实际访问的文件内容
            if action in ("read_file", "read_lines"):
                file_path = action_input.get("file_path", "")
                if file_path:
                    files_content[file_path] = self._read_file(file_path)[:5000]
            elif action == "read_function":
                file_path = action_input.get("file_path", "")
                if file_path:
                    self.visited_files.add(file_path)
                    files_content[file_path] = observation[:5000]

            steps.append({
                "step": step_num,
                "thought": thought,
                "action": action,
                "action_input": action_input,
                "observation": observation,
                "files_accessed": new_files,
            })

            if action == "finish":
                break

        # 生成答案
        investigation_log = "\n\n".join(
            f"Step {s['step']}: {s['action']}({json.dumps(s['action_input'], ensure_ascii=False)})\n"
            f"Thought: {s['thought']}\n"
            f"Observation: {s['observation'][:500]}"
            for s in steps
        )

        files_summary = "\n\n".join(
            f"=== {fp} ===\n{content[:3000]}"
            for fp, content in files_content.items()
        )

        prompt = ANSWER_PROMPT.format(
            question=question,
            investigation_log=investigation_log,
            files_content=files_summary,
        )

        answer = call_llm(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=None,
            model=model,
        )

        return {
            "answer": answer,
            "steps": steps,
            "visited_files": list(self.visited_files),
            "files_content": files_content,
        }


# ── Main ────────────────────────────────────────────────────────────

def process_one(item, q_emb, ca, retriever, chunks_by_id, repo_root, model, max_steps):
    question = item["question"]
    qa_id = item["qa_id"]

    # 1. Concept-Symbol retrieval 得到准确函数位置
    initial_functions = concept_symbol_retrieve_functions(
        question, q_emb, ca, retriever, chunks_by_id, top_concepts=20, top_funcs=20
    )

    # 2. ReAct 调查
    agent = ReactAgent(repo_root, max_steps=max_steps)
    result = agent.investigate(question, initial_functions, model)

    return {
        "qa_id": qa_id,
        "question": question,
        "answer": result["answer"],
        "initial_functions": initial_functions,
        "visited_files": result["visited_files"],
        "steps": result["steps"],
        "model": model,
    }


def main():
    parser = argparse.ArgumentParser(description="Concept-Symbol + ReAct QA")
    parser.add_argument("--model", default=None, help="LLM model")
    parser.add_argument("--max-steps", type=int, default=50)
    parser.add_argument("--workers", type=int, default=30)
    parser.add_argument("--output", default=None)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    model = args.model or LLM_MODEL or "deepseek-v4-flash"

    benchmark_path = _ROOT / "datasets" / "benchmark_hard.json"
    index_path = _ROOT / "data" / "qa_embedding_index.json"
    module_cache = _ROOT / "data" / "module_abstraction.json"
    concept_cache = _ROOT / "data" / "concept_abstraction.json"
    repo_root = Path(REPO_ROOT) if REPO_ROOT else _ROOT

    items = load_benchmark(benchmark_path)
    chunks, doc_matrix = load_embedding_index(index_path)
    line_lookup, chunks_by_id, chunk_index_by_id, func_to_file, file_to_funcs = build_helpers(chunks)

    retriever = FastEmbeddingRetriever(index_path=index_path)
    retriever.chunk_index_by_id = {ch["id"]: idx for idx, ch in enumerate(retriever.chunks)}
    print("Encoding questions...")
    q_embs = retriever.encode_queries([item["question"] for item in items])

    ma = ModuleAbstraction(cache_path=str(module_cache))
    if not ma.load():
        ma.build_from_neo4j()
        ma.save()

    ca = ConceptAbstraction(cache_path=str(concept_cache))
    if not ca.load():
        funcs, calls = fetch_functions_and_calls()
        ca.build_from_modules(ma, functions=funcs, calls=calls)
        ca.save()

    if args.limit:
        items = items[:args.limit]
        q_embs = q_embs[:args.limit]

    print(f"\nRunning Concept-Symbol + ReAct QA with {model} (max_steps={args.max_steps}, workers={args.workers})")
    print(f"Questions: {len(items)}")

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(process_one, item, q_emb, ca, retriever, chunks_by_id, repo_root, model, args.max_steps): idx
            for idx, (item, q_emb) in enumerate(zip(items, q_embs))
        }
        for future in as_completed(futures):
            idx = futures[future]
            try:
                result = future.result()
                results.append(result)
                print(f"[{len(results)}/{len(items)}] {result['qa_id']} done ({len(result['steps'])} steps)", flush=True)
            except Exception as e:
                print(f"[ERROR] idx={idx}: {e}", flush=True)

    qa_id_to_idx = {item["qa_id"]: i for i, item in enumerate(items)}
    results.sort(key=lambda r: qa_id_to_idx.get(r["qa_id"], 0))

    if args.output:
        output_path = Path(args.output)
    else:
        suffix = model.replace("/", "_")
        output_path = _ROOT / "results" / f"qa_react_concept_symbol_v7_{suffix}.json"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\nSaved results to: {output_path}")


if __name__ == "__main__":
    main()
