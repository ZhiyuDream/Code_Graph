#!/usr/bin/env python3
"""
端到端 QA：Concept-Symbol 初始定位 + ReAct 调查（V19）。

流程：
Question
  ↓
Concept-Symbol retrieval（top-100 候选池，首批展示 20）
  ↓
ReAct 调查：
  - read_function 读完整实现，标记函数相关性
  - 对相关函数用 find_callers / find_callees / list_functions / list_files 扩展
  - 初始召回不相关时 expand_recall 或按目录下钻
  - 文件级不相关标记，避免重复调查
  ↓
Generate answer with LLM

输出格式兼容 evals/eval_v2.py，steps 字段记录每步 thought/action/observation。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
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
from src.qa.tools.react_tools import (
    tool_read_function,
    tool_read_lines,
    tool_list_functions,
    tool_list_files,
    tool_find_callers,
    tool_find_callees,
    tool_search_symbol,
    tool_expand_recall,
)
from src.qa.tools.grep_call_chain import grep_callers
from scripts.analysis.eval_region_compression import (
    fetch_functions_and_calls,
    load_benchmark,
    load_embedding_index,
    build_helpers,
)


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
    top_funcs: int = 100,
    extra_symbols: list | None = None,
) -> list[dict]:
    """Retrieve top functions with accurate positions."""
    symbols = extract_symbols(question)
    if extra_symbols:
        symbols = list(set(symbols) | {s.lower() for s in extra_symbols if isinstance(s, str) and len(s) >= 3})
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

REACT_PROMPT = """你是一位代码审计专家。检索系统已经为你找到了一批**准确的函数位置**，你需要从这些函数开始，逐步扩展构建证据链。

【当前问题】
{question}

【仓库目录结构】（帮助你判断哪些目录可能与问题相关）
{repo_structure}

【检索系统提供的候选函数】（按相关性排序，文件路径准确；已展示 {recall_shown}/{recall_total} 个，可用 expand_recall 看更多）
{candidate_functions}

【系统推测的相关符号】（可用 search_symbol 验证它们是否真的存在、在哪里）
{keyword_hints}

【目录探测结果】（关键词在函数名索引中的命中密度，指示问题最可能属于哪个子系统）
{dir_probes}

【关键词命中函数族】（同一关键词可能对应多层实现——不同前缀往往是不同层级/子系统，注意逐族区分，别只读一族）
{keyword_families}

【你已看过的函数及相关性】
{visited_functions}

【文件调查状态】
{file_status}

【监督者意见】（每 5 步由监督 agent 评估方向后给出，请重视）
{supervisor_guidance}

【你已执行过的动作】（最近几步）
{action_history}

---

你可以使用以下工具：

1. **read_function(function_name, file_path)** — 读取函数完整实现（不截断，必须看完整实现才能判断相关性）。支持批量：function_name 传数组（最多 8 个），file_paths 传对应路径数组。**建议开局就把前几个候选函数一批读完**，省步数；超大函数会只显示开头，可用 read_lines 按区间读剩余部分
2. **read_lines(file_path, start_line, end_line)** — 读取文件指定行号范围（用于宏、全局变量等非函数区域）
3. **list_functions(file_path)** — 列出文件中所有函数的名称和行号范围（了解同文件还有什么函数）
4. **list_files(directory)** — 列举目录下的代码文件和子目录（探索某个目录）
5. **find_callers(function_name)** — 查找谁调用了这个函数（全仓库搜索调用点，定位到具体调用方函数）
6. **find_callees(function_name)** — 列出这个函数体内调用了哪些函数（只需函数名，行号自动从你已读的函数中查找）
7. **search_symbol(symbol_name)** — 搜索符号：Neo4j 函数名索引（精确定义）+ grep 全文（所有提及）
8. **expand_recall()** — 初始召回的候选不相关时，查看召回池的下一批候选函数
9. **mark_file_irrelevant(file_path)** — 标记文件为不相关，避免重复调查（同一文件读 3 个以上函数全部不相关时系统也会自动标记）
10. **skip_candidates(function_names, reason)** — 分诊：仅凭名称/签名判断明显不相关的候选函数，批量跳过（不读实现，节省步数）。慎用：判断错了会漏证据
11. **finish(reason)** — 认为已有足够证据，结束调查并生成答案。注意：如果还有标记为"相关"的函数没做过 find_callers / find_callees 调查，或 find_callers 发现的调用点还没 read_function 读过实现，系统会拒绝 finish 并要求你先补全证据链

---

{skill}

**最多 {max_steps} 步，当前第 {current_step} 步。**

【输出格式】（必须是有效的 JSON）
{{
  "thought": "你的思考过程...",
  "action": "read_function|read_lines|list_functions|list_files|find_callers|find_callees|search_symbol|expand_recall|mark_file_irrelevant|skip_candidates|finish",
  "action_input": {{"参数名": "参数值"}},
  "reason": "为什么选择这个行动",
  "function_relevance": {{"function_name": "相关|不相关|未知"}}
}}

注意：function_relevance 用于标记你读过的函数是否与问题相关。标记为"相关"的函数所在文件会自动标记为相关文件；都不相关的文件请用 mark_file_irrelevant 显式标记。
"""

# 默认调查策略（skill）。可被外部 skill 文件覆盖：
# 优先级：QA_SKILL_PATH 环境变量 > data/qa_skill.md > 本默认值
DEFAULT_SKILL = """【调查策略】
1. **从候选函数开始**：优先用 read_function 批量读取候选函数的完整实现（开局建议一批读 3-8 个）
2. **分诊节省步数**：候选中明显不相关的（名称/签名/文件方向都对不上），用 skip_candidates 批量跳过，不要浪费 read_function；拿不准的就别跳，读了再说
3. **对相关函数重点扩展**：
   - find_callers 查它被谁调用（上游）
   - find_callees 查它调用了谁（下游）
   - list_functions 看同文件还有什么相关函数
   - list_files 看同目录还有什么相关文件
4. **初始召回全部不相关时的两条出路**：
   - expand_recall 往下看更多召回候选
   - 根据仓库目录结构，list_files 探索你判断可能相关的目录，再用 list_functions / read_function 调查其中的文件
5. **及时标记不相关文件**：文件内读过的函数都不相关 → mark_file_irrelevant，之后不要再调查该文件
6. **不要重复动作**：同一个动作+同样的参数执行过了就换方向，系统会拒绝重复执行
7. **文件路径必须准确**：read_function 的 file_path 必须来自候选函数或工具返回结果，不要猜测
8. **诚实原则（最重要）**：
   - 你只能引用你实际访问过的文件和函数
   - 如果工具返回空或找不到，如实说明"无法确认"
   - 绝对不要编造文件路径、函数名、代码内容或调用关系
   - 如果没有访问到任何相关文件，finish 时必须说"无法确认"
"""


def load_skill() -> str:
    """加载调查策略 skill（SkillOpt 试点的可训练文本状态）。"""
    import os
    path = os.environ.get("QA_SKILL_PATH")
    if path and Path(path).exists():
        return Path(path).read_text(encoding="utf-8")
    default_path = _ROOT / "data" / "qa_skill.md"
    if default_path.exists():
        return default_path.read_text(encoding="utf-8")
    return DEFAULT_SKILL


ANSWER_PROMPT = """基于你的调查过程和收集到的证据，回答问题。

【原始问题】
{question}

【调查决策过程】
{decision_log}

【你实际读过的函数清单】（含相关性标记）
{read_function_list}

【证据内容：你读过的函数完整实现】
{evidence_content}

---

请生成最终答案，必须严格遵守以下规则：

1. **相关函数必须引用，且每个相关文件都要有分析**：清单中每个标记为"相关"的函数，其所在文件都必须出现在你的引用中；对每个相关文件，至少写一段它证明了什么（引用格式 file:start-end），不允许只在末尾清单里列个路径而没有内容分析；如果认为某个相关函数确实与答案无关，必须在答案中明确说明理由
2. **以已读函数清单为唯一证据标准**：清单里的函数就是你实实在在读过实现的。调查过程中被拒绝的重复动作不影响这些证据的有效性——不要因为某个动作被拒过就说"无法确认"；只有当清单里确实没有相关函数时才说"无法确认"
3. **只能引用你实际访问过的文件**：参考文件清单只能包含上面证据内容中出现的文件
4. **只能引用你实际读到的函数**：不要提及你没有读过具体实现的函数
5. **不要编造**：绝对不要虚构文件路径、函数名、代码内容或调用关系
6. 引用格式：`file.cpp:start-end`
7. 答案末尾列出参考文件清单，清单中的文件必须是你实际访问过的

请用中文回答：
"""


SUPERVISOR_PROMPT = """你是调查监督者。一个代码审计 agent 正在仓库中调查以下问题，请评估它的调查方向并给出指导。

【调查问题】
{question}

【系统推测的相关符号】
{keyword_hints}

【agent 已读函数及相关性】
{visited_functions}

【文件调查状态】
{file_status}

【未读调用点线索】（find_callers 发现但还没读实现的）
{unread_leads}

【agent 最近 5 步动作】
{recent_actions}

---

请判断：
1. agent 当前探索的方向（文件/子系统）是否与问题真正相关？有没有钻入无关方向？
2. 有没有明明该读却没读的关键线索？
3. 是否应该换方向、换关键词，还是继续当前方向？

【输出格式】（必须是有效的 JSON）
{{
  "direction": "正确|可疑|错误",
  "assessment": "一句话判断依据",
  "guidance": "给调查 agent 的具体下一步指令（80字内，要具体到动作和对象）",
  "suggest_keywords": ["如果当前关键词不对，给出1-3个新搜索关键词，否则空数组"]
}}
"""


class ReactAgent:
    RECALL_BATCH = 20

    def __init__(self, repo_root: Path, max_steps: int = 25, recall_pool: list[dict] | None = None,
                 keyword_hints: list[str] | None = None,
                 retriever=None, chunks_by_id: dict | None = None,
                 dir_probes: list | None = None, keyword_families: dict | None = None):
        self.repo_root = repo_root
        self.max_steps = max_steps
        self.recall_pool = recall_pool or []
        self.keyword_hints = keyword_hints or []
        self.dir_probes = dir_probes or []
        self.keyword_families = keyword_families or {}
        self.retriever = retriever          # supervisor 触发重新召回用
        self.chunks_by_id = chunks_by_id    # 关键词名称查找用
        self.recall_shown = min(self.RECALL_BATCH, len(self.recall_pool))
        self.recall_expansions = 0

        self.visited_files = set()
        self.visited_functions = {}   # function_name -> True/False/None
        self.past_observations = {}   # (action, canonical_input) -> 首次执行的 observation，拒绝重复时回放
        self.read_functions = {}      # function_name -> {file, start_line, end_line, code}，证据注册表
        self.chain_checked = set()    # 已做过 find_callers/find_callees 的函数
        self.caller_leads = {}        # find_callers 发现的调用点线索: func -> [(caller_name, file)]
        self.finish_rejected_once = False  # finish 守卫只拒一次，防死循环
        self.usage = []               # LLM token 用量记录
        self.direction_warnings = 0   # 方向纠偏提示触发次数
        self.rejection_count = 0      # 重复动作被拒次数（>=2 触发升级干预）
        self.supervisor_notes = []    # 监督 agent 的历次评估
        self.latest_guidance = ""     # 最近一次监督意见（注入下一步 prompt）
        self.force_redirected = False  # 强制转向已用过（每次调查限一次）
        self.forced_action = None      # 待执行的强制动作 (action, input)
        self.backfilled = []           # 结束前兜底补读的调用点
        self.skipped_unread = {}       # 分诊跳过的候选: name@file -> reason
        self.triage_rejected_once = False  # 分诊误杀兜底只拒一次
        self.skill = load_skill()      # 调查策略（SkillOpt 可训练文本状态）
        self.func_file_map = {}       # function_name -> file_path（read_function 成功时记录）
        self.file_status = {}         # file_path -> "relevant" / "irrelevant"
        self.action_counts = Counter()  # (action, canonical_input) -> count，通用防循环
        self.file_read_counts = defaultdict(int)  # read_function 按文件计数
        self.repo_structure = self._get_repo_structure(repo_root, max_depth=2)

    def _get_repo_structure(self, repo_root: Path, max_depth: int = 2) -> str:
        """获取仓库目录结构（跳过隐藏目录和 build 目录）。"""
        lines = []
        skip = {"build", ".git", ".github", "node_modules", "vendor"}
        for dirpath, dirnames, filenames in os.walk(repo_root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in skip]
            rel = Path(dirpath).relative_to(repo_root)
            depth = len(rel.parts)
            if depth > max_depth:
                dirnames[:] = []
                continue
            if depth == 0:
                continue
            n_code = sum(1 for f in filenames if Path(f).suffix in (".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"))
            lines.append(f"{'  ' * (depth - 1)}{rel.name}/ ({n_code} code files)")
            if len(lines) >= 120:
                break
        return "\n".join(lines)

    # ── 工具分发 ─────────────────────────────────────────────────────

    def execute(self, action: str, action_input: dict) -> tuple[str, list]:
        if action == "read_function":
            # 支持批量：function_name 为数组，或另传 function_names / file_paths 数组
            names = action_input.get("function_name", "")
            if isinstance(names, str):
                names = [names] if names else []
            extra_names = action_input.get("function_names")
            if isinstance(extra_names, list):
                names.extend(extra_names)
            names = [n for n in names if isinstance(n, str) and n][:8]
            if not names:
                return "错误：read_function 缺少 function_name 参数", []
            file_path = action_input.get("file_path", "")
            file_paths = action_input.get("file_paths")
            if isinstance(file_path, list):  # LLM 把路径数组放在 file_path 里
                file_paths = file_path
                file_path = ""
            obs_parts, all_files = [], []
            OBS_BUDGET = 100000  # 本步 observation 的字符预算（≈25k tokens，上下文窗口保护）
            used = 0
            skipped = 0
            for i, fn in enumerate(names):
                fp = file_path
                if isinstance(file_paths, list) and i < len(file_paths) and file_paths[i]:
                    fp = file_paths[i]
                fp_clean = (fp or "").split(":")[0]
                # 结案文件禁读：已标记不相关的文件不再浪费步数
                if fp_clean and self.file_status.get(fp_clean) == "irrelevant":
                    obs_parts.append(f"【已结案】文件 {fp_clean} 已标记为不相关（调查后无相关内容），请勿回头。请调查其他方向。")
                    continue
                if used >= OBS_BUDGET:
                    skipped += 1
                    continue
                meta: dict = {}
                obs, nf = tool_read_function(fn, fp, self.repo_root, out_meta=meta)
                obs_parts.append(obs)
                used += len(obs)
                if nf:
                    self.func_file_map.setdefault(fn, set()).add(nf[0])
                    self.read_functions[f"{fn}@{nf[0]}"] = meta
                    all_files.append(nf[0])
            if skipped:
                obs_parts.append(f"【上下文预算】本步读取已达 {OBS_BUDGET} 字符上限，剩余 {skipped} 个函数请下一步再读。")
            return "\n\n".join(obs_parts), all_files

        if action == "read_lines":
            fp_clean = action_input.get("file_path", "").split(":")[0]
            if fp_clean and self.file_status.get(fp_clean) == "irrelevant":
                return f"【已结案】文件 {fp_clean} 已标记为不相关（调查后无相关内容），请勿回头。请调查其他方向。", []
            return tool_read_lines(
                action_input.get("file_path", ""),
                int(action_input.get("start_line", 1) or 1),
                int(action_input.get("end_line", 50) or 50),
                self.repo_root,
            )

        if action == "list_functions":
            return tool_list_functions(action_input.get("file_path", ""), self.repo_root)

        if action == "list_files":
            return tool_list_files(action_input.get("directory", ""), self.repo_root)

        if action == "find_callers":
            names = action_input.get("function_name", "")
            if isinstance(names, str):
                names = [names] if names else []
            names = [n.split("@")[0] for n in names if isinstance(n, str) and n][:5]
            if not names:
                return "错误：find_callers 缺少 function_name 参数", []
            self.chain_checked.update(names)
            obs_parts, all_files = [], []
            for n in names:
                found: list = []
                obs, nf = tool_find_callers(n, self.repo_root, out_meta=found)
                obs_parts.append(obs)
                all_files.extend(nf)
                # 记录调用点线索（供 finish 守卫检查是否已读）
                leads = [(c["name"], c["file"]) for c in found if c.get("name")]
                if leads:
                    self.caller_leads.setdefault(n, []).extend(leads)
                # 确定性自动扩展：find_callers 是 Agent 主动点名查的，结果精度高，
                # 无条件自动读 top-4 未读调用点（不依赖 LLM 是否已标相关——034/037 案例：
                # 查了 callers 但没标相关就没跟进，gold 调用点漏读；search_symbol 的探索性跟进才有条件）
                if True:
                    auto = 0
                    for caller_name, caller_file in leads:
                        if auto >= 4:
                            break
                        key = f"{caller_name}@{caller_file}"
                        if key in self.read_functions:
                            continue
                        meta: dict = {}
                        robs, rnf = tool_read_function(caller_name, caller_file, self.repo_root, out_meta=meta)
                        if rnf:
                            self.func_file_map.setdefault(caller_name, set()).add(rnf[0])
                            self.read_functions[key] = meta
                            all_files.append(rnf[0])
                            obs_parts.append(f"[系统自动读取调用点]\n{robs}")
                            auto += 1
                    if auto:
                        obs_parts.append(
                            f"（系统已自动读取 {auto} 个调用点的实现，可直接作为证据引用；"
                            "如判断与问题无关，请在 function_relevance 中标记不相关）"
                        )
            return "\n\n".join(obs_parts), sorted(set(all_files))

        if action == "find_callees":
            names = action_input.get("function_name", "")
            if isinstance(names, str):
                names = [names] if names else []
            names = [n.split("@")[0] for n in names if isinstance(n, str) and n][:5]
            if not names:
                return "错误：find_callees 缺少 function_name 参数", []
            self.chain_checked.update(names)
            obs_parts = []
            for func_name in names:
                file_path = action_input.get("file_path", "")
                start = int(action_input.get("start_line", 0) or 0)
                end = int(action_input.get("end_line", 0) or 0)
                if not file_path or not start or not end:
                    # 行号自动从已读注册表查找
                    matches = [m for k, m in self.read_functions.items() if k.split("@")[0] == func_name]
                    if matches:
                        m = matches[0]
                        file_path, start, end = m["file"], m["start_line"], m["end_line"]
                    elif not file_path:
                        obs_parts.append(
                            f"错误：find_callees 找不到 {func_name} 的实现位置。"
                            "请先 read_function 读取它，或手动提供 file_path/start_line/end_line"
                        )
                        continue
                obs, _ = tool_find_callees(func_name, file_path, start, end, self.repo_root)
                obs_parts.append(obs)
            return "\n\n".join(obs_parts), []

        if action == "search_symbol":
            symbol = action_input.get("symbol_name", "")
            obs, new_files = tool_search_symbol(symbol, self.repo_root)
            # 命中即跟进（仅在还没找到任何相关函数时启用）：此时 Agent 在找方向，
            # 名称匹配的函数是净收益；上了正轨后不再注入，避免干扰（V33 回退案例）
            if new_files and not any(v is True for v in self.visited_functions.values()):
                auto = 0
                seen_names = set()
                for key, meta in self.read_functions.items():
                    seen_names.add(key)
                candidates = []
                if self.chunks_by_id:
                    sym_l = symbol.strip().lower()
                    for fid, ch in self.chunks_by_id.items():
                        name = (ch.get("meta") or {}).get("name", "")
                        if sym_l and sym_l in name.lower() and fid not in seen_names:
                            candidates.append((name, ch["meta"].get("file_path", ""), fid))
                for name, fp, fid in candidates:
                    if auto >= 2:
                        break
                    meta: dict = {}
                    robs, rnf = tool_read_function(name, fp, self.repo_root, out_meta=meta)
                    if rnf:
                        self.func_file_map.setdefault(name, set()).add(rnf[0])
                        self.read_functions[f"{name}@{rnf[0]}"] = meta
                        new_files.append(rnf[0])
                        obs += f"\n\n[系统自动跟进读取]\n{robs}"
                        auto += 1
                if auto:
                    obs += f"\n（系统已自动跟进读取 {auto} 个名称匹配的函数实现；如无关请标记不相关）"
            return obs, sorted(set(new_files))

        if action == "expand_recall":
            obs, self.recall_shown = tool_expand_recall(self.recall_pool, self.recall_shown, self.RECALL_BATCH)
            self.recall_expansions += 1
            return obs, []

        if action == "mark_file_irrelevant":
            file_path = action_input.get("file_path", "").split(":")[0]
            if not file_path:
                return "错误：mark_file_irrelevant 缺少 file_path 参数", []
            if self.file_status.get(file_path) == "relevant":
                return f"文件 {file_path} 已含有相关函数，不能标记为不相关", []
            self.file_status[file_path] = "irrelevant"
            return f"已标记 {file_path} 为不相关文件，后续请不要再调查该文件", []

        if action == "skip_candidates":
            names = action_input.get("function_names") or action_input.get("function_name") or []
            if isinstance(names, str):
                names = [names]
            names = [n for n in names if isinstance(n, str) and n]
            if not names:
                return "错误：skip_candidates 缺少 function_names 参数", []
            reason = action_input.get("reason", "")
            skipped, not_found = [], []
            for n in names:
                # 匹配召回池中的候选（按裸名）
                matched = [f for f in self.recall_pool if f["name"] == n or f["name"].split("::")[-1] == n]
                if not matched:
                    not_found.append(n)
                    continue
                for f in matched:
                    key = f"{f['name']}@{f['file_path']}"
                    if key in self.read_functions:
                        continue  # 已读过的不能跳
                    self.visited_functions[key] = False
                    self.skipped_unread[key] = reason
                    skipped.append(n)
            obs = f"已跳过 {len(skipped)} 个候选（未读实现，仅依据名称/签名判断）：{', '.join(sorted(set(skipped)))}。"
            if not_found:
                obs += f"\n注意：{', '.join(not_found)} 不在候选池中，无需跳过。"
            return obs, []

        if action == "finish":
            return f"结束调查: {action_input.get('reason', '证据充分')}", []

        return f"未知工具: {action}", []

    # ── 主循环 ───────────────────────────────────────────────────────

    def _unread_leads(self) -> list[tuple[str, str]]:
        """相关函数的调用点线索中还没 read_function 读过的 [(name, file)]。"""
        unread, seen = [], set()
        for fn, rel in self.visited_functions.items():
            if rel is not True:
                continue
            for caller_name, caller_file in self.caller_leads.get(fn.split("@")[0], []):
                key = f"{caller_name}@{caller_file}"
                if key in seen or key in self.read_functions:
                    continue
                seen.add(key)
                unread.append((caller_name, caller_file))
        return unread

    def _call_supervisor(self, question: str, steps: list, model: str) -> None:
        """每 5 步由监督 agent 评估方向，意见注入下一步 prompt。"""
        recent = "\n".join(
            f"Step {s['step']}: {s['action']}({json.dumps(s['action_input'], ensure_ascii=False)[:80]})"
            + (" [被拒绝]" if s.get("rejected") else "")
            + f" → {s['observation'][:120]}"
            for s in steps[-5:]
        )
        visited_str = "\n".join(
            f"- {k} ({'相关' if v is True else '不相关' if v is False else '未知'})"
            for k, v in sorted(self.visited_functions.items())
        ) or "(无)"
        file_status_str = "\n".join(
            f"- {fp}: {st}" for fp, st in sorted(self.file_status.items())
        ) or "(无)"
        leads_str = "\n".join(f"- {n} @ {f}" for n, f in self._unread_leads()[:8]) or "(无)"
        prompt = SUPERVISOR_PROMPT.format(
            question=question,
            keyword_hints=", ".join(self.keyword_hints) or "(无)",
            visited_functions=visited_str,
            file_status=file_status_str,
            unread_leads=leads_str,
            recent_actions=recent,
        )
        try:
            note = call_llm_json(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=500,
                model=model,
                _usage_sink=self.usage,
            )
        except Exception as e:
            print(f"[SUPERVISOR_ERROR] {e}", file=sys.stderr, flush=True)
            return
        if not isinstance(note, dict):
            return
        note["step"] = steps[-1]["step"] if steps else 0
        self.supervisor_notes.append(note)
        kws = note.get("suggest_keywords") or []
        self.latest_guidance = (
            f"方向评估：{note.get('direction', '未知')}（{note.get('assessment', '')}）\n"
            f"监督指令：{note.get('guidance', '')}"
            + (f"\n建议搜索关键词：{', '.join(kws)}" if kws else "")
        )

        # 强制转向（每次调查限一次）：方向判"错误" →
        # 1) 最近在读但无相关产出的文件标记"不相关结案"，防止回头
        # 2) 用 supervisor 给的关键词**重新召回**（名称查找 + embedding 重查）注入候选池
        # 3) 下一步系统强制执行监督建议的搜索/扩展
        if note.get("direction") == "错误" and not self.force_redirected:
            self.force_redirected = True
            recent_files = set()
            for s in steps[-5:]:
                recent_files.update(s.get("files_accessed", []))
            closed = []
            for fp in sorted(recent_files):
                has_relevant = any(
                    k.split("@", 1)[1] == fp and v is True
                    for k, v in self.visited_functions.items() if "@" in k
                )
                if not has_relevant and self.file_status.get(fp) != "relevant":
                    self.file_status[fp] = "irrelevant"
                    closed.append(fp)

            # 重新召回：supervisor 关键词 → 名称索引 + embedding 重查，注入候选池前列
            new_candidates = []
            if kws:
                existing = {f["fid"] for f in self.recall_pool}
                if self.chunks_by_id:
                    new_candidates.extend(
                        h for h in lookup_functions_by_keywords(kws, self.chunks_by_id, limit=6)
                        if h["fid"] not in existing
                    )
                if self.retriever is not None:
                    try:
                        import numpy as _np
                        q2 = self.retriever.encode_queries([question + " " + " ".join(kws)])[0]
                        for r in self.retriever.retrieve(_np.asarray([q2], dtype=_np.float32), top_k=20):
                            md = r["metadata"]
                            fid = f"{md['file_path']}:{md['name']}:{md['start_line']}"
                            if fid in existing or any(c["fid"] == fid for c in new_candidates):
                                continue
                            new_candidates.append({
                                "fid": fid, "name": md.get("name", ""), "file_path": md.get("file_path", ""),
                                "start_line": md.get("start_line", 0), "end_line": md.get("end_line", 0),
                                "signature": md.get("signature", ""), "score": r.get("score", 0.0),
                            })
                    except Exception as e:
                        print(f"[SUPERVISOR_ERROR] 重新召回失败: {e}", file=sys.stderr, flush=True)
            if new_candidates:
                new_candidates = new_candidates[:20]
                self.recall_pool = new_candidates + self.recall_pool
                self.recall_shown = min(self.recall_shown + len(new_candidates), len(self.recall_pool))
                self.latest_guidance += f"\n【重新召回】已用新关键词补充 {len(new_candidates)} 个候选函数到候选池前列。"

            if kws:
                self.forced_action = ("search_symbol", {"symbol_name": kws[0]})
            else:
                self.forced_action = ("expand_recall", {})
            self.latest_guidance += (
                f"\n【强制转向】以下文件已结案（调查后无相关内容，勿再回头）：{', '.join(closed) or '无'}。"
                f"下一步系统将强制执行 {self.forced_action[0]}，请基于其结果继续。"
            )

    def investigate(self, question: str, model: str) -> dict:
        steps = []
        files_content = defaultdict(list)  # file_path -> [读到的内容块]

        for step_num in range(1, self.max_steps + 1):
            # 候选列表动态标注已读状态，避免 Agent 反复回到同一函数
            candidate_lines = []
            for f in self.recall_pool[:self.recall_shown]:
                key = f"{f['name']}@{f['file_path']}"
                rel = self.visited_functions.get(key)
                mark = ""
                if f.get("name_match"):
                    mark = " 【名称匹配】"
                if key in self.skipped_unread:
                    mark += " 【已跳过: 未读】"
                elif rel is True:
                    mark += " 【已读: 相关】"
                elif rel is False:
                    mark += " 【已读: 不相关】"
                elif rel is not None or key in self.visited_functions:
                    mark += " 【已读】"
                candidate_lines.append(
                    f"- {f['name']} @ {f['file_path']}:{f['start_line']}-{f['end_line']} (score: {f['score']:.3f}){mark}"
                )
            candidate_functions = "\n".join(candidate_lines) or "(无)"

            # 最近 10 步历史，被拒绝的动作明确标注
            action_history = "\n".join(
                f"Step {s['step']}: {s['action']}({json.dumps(s['action_input'], ensure_ascii=False)[:60]})"
                + (" [被拒绝: 重复动作]" if s.get("rejected") else "")
                for s in steps[-10:]
            ) or "(无)"

            visited_functions_str = "\n".join(
                f"- {name} ({'相关' if rel is True else '不相关' if rel is False else '未知'})"
                for name, rel in sorted(self.visited_functions.items())
            ) or "(无)"

            file_status_str = "\n".join(
                f"- {fp}: {'含有相关函数' if st == 'relevant' else '不相关，勿再调查'}"
                for fp, st in sorted(self.file_status.items())
            ) or "(无)"

            prompt = REACT_PROMPT.format(
                question=question,
                repo_structure=self.repo_structure,
                candidate_functions=candidate_functions,
                recall_shown=self.recall_shown,
                recall_total=len(self.recall_pool),
                keyword_hints=", ".join(self.keyword_hints) or "(无)",
                dir_probes="\n".join(
                    f"- {d}（{n} 个命中，如 {', '.join(ex[:3])}）" for d, n, ex in self.dir_probes
                ) or "(无)",
                keyword_families="\n".join(
                    f"- {kw}: " + "；".join(
                        f"族 {fam or '(无前缀)'}（如 {', '.join(f'{n}@{fp}' for n, fp in names[:2])}）"
                        for fam, names in fams[:3]
                    )
                    for kw, fams in list(self.keyword_families.items())[:6]
                ) or "(无)",
                visited_functions=visited_functions_str,
                file_status=file_status_str,
                supervisor_guidance=self.latest_guidance or "(暂无，第 5 步后会有首次评估)",
                action_history=action_history,
                skill=self.skill,
                max_steps=self.max_steps,
                current_step=step_num,
            )

            if self.forced_action:
                # 强制转向：supervisor 判"错误"后本步由系统执行，跳过 LLM 决策
                decision = None
            else:
                decision = call_llm_json(
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=None,
                    model=model,
                    _usage_sink=self.usage,
                )
                # 决策失败重试：单次 LLM 抽风不应葬送整题（002 案例：第一步解析失败直接投降）
                for _retry in range(2):
                    if decision is not None:
                        break
                    print(f"[WARN] step {step_num} 决策解析失败，重试 {_retry + 1}/2", flush=True)
                    decision = call_llm_json(
                        messages=[{"role": "user", "content": prompt}],
                        max_tokens=None,
                        model=model,
                        _usage_sink=self.usage,
                    )
            if decision is None and not self.forced_action:
                decision = {"thought": "决策失败", "action": "finish", "action_input": {"reason": "决策失败"}, "reason": "出错停止"}
            elif decision is None:
                decision = {"thought": "", "action": "finish", "action_input": {}, "reason": ""}

            action = decision.get("action", "finish")
            action_input = decision.get("action_input", {}) or {}
            # LLM 偶尔把 action_input 直接写成字符串而非 dict，归一化为该工具的主参数
            if not isinstance(action_input, dict):
                primary_param = {
                    "finish": "reason",
                    "search_symbol": "symbol_name",
                    "find_callers": "function_name",
                    "read_function": "function_name",
                    "list_functions": "file_path",
                    "list_files": "directory",
                    "mark_file_irrelevant": "file_path",
                }.get(action)
                action_input = {primary_param: action_input} if primary_param else {}
            thought = decision.get("thought", "")
            reason = decision.get("reason", "")

            # 强制转向：supervisor 判"错误"后下一步由系统执行，不再问 LLM
            if self.forced_action:
                action, action_input = self.forced_action
                self.forced_action = None
                thought = "监督者判定当前方向错误，系统强制执行转向动作"
                reason = "强制转向"

            # 记录 LLM 返回的函数相关性（匹配复合 key name@file）；相关函数所在文件自动标记为相关
            function_relevance = decision.get("function_relevance", {}) or {}
            # LLM 偶尔返回列表格式 [{"name": ..., "relevance": ...}]，归一化为 dict
            if isinstance(function_relevance, list):
                function_relevance = {
                    str(x.get("name", x.get("function_name", ""))): x.get("relevance", x.get("rel", "未知"))
                    for x in function_relevance if isinstance(x, dict)
                }
            for fn, rel in function_relevance.items():
                matched = [k for k in self.read_functions if k == fn or k.split("@")[0] == fn]
                targets = matched or [fn]
                for k in targets:
                    if rel == "相关":
                        self.visited_functions[k] = True
                        if "@" in k:
                            self.file_status[k.split("@", 1)[1]] = "relevant"
                    elif rel == "不相关":
                        self.visited_functions[k] = False
                    else:
                        self.visited_functions[k] = None

            # 自动标记不相关文件：同文件读 3 个以上函数全部不相关
            file_rels = defaultdict(list)
            for k, rel in self.visited_functions.items():
                if "@" in k:
                    file_rels[k.split("@", 1)[1]].append(rel)
            for fp, rels in file_rels.items():
                if len(rels) >= 3 and all(r is False for r in rels) and self.file_status.get(fp) != "relevant":
                    self.file_status[fp] = "irrelevant"

            # finish 守卫 A：分诊误杀兜底——零相关且跳过 ≥3 个未读候选 → 强制回读验证（只拒一次）
            if action == "finish" and not self.triage_rejected_once:
                n_relevant = sum(1 for v in self.visited_functions.values() if v is True)
                if n_relevant == 0 and len(self.skipped_unread) >= 3:
                    self.triage_rejected_once = True
                    rejected = True
                    recheck = list(self.skipped_unread.items())[:3]
                    observation = (
                        "【finish 被拒绝】你跳过了多个候选函数但一个相关函数都没找到，"
                        "分诊可能有误杀。请先用 read_function 实际读取以下被跳过的候选验证分诊是否正确：\n"
                        + "\n".join(f"- {k}（跳过理由：{r or '未给'}）" for k, r in recheck)
                        + "\n如果读完确认都不相关，再 finish 不迟。"
                    )
                    new_files = []
                    steps.append({
                        "step": step_num, "thought": thought, "action": action,
                        "action_input": action_input, "reason": reason,
                        "observation": observation, "files_accessed": [], "rejected": True,
                    })
                    continue

            # finish 守卫 B：还有相关函数没做调用链调查，或调用点线索未读 → 拒绝 finish（只拒一次；
            # 即使最后一步拒绝也无妨，循环结束后答案照常生成）
            if action == "finish" and not self.finish_rejected_once:
                pending_chain = [
                    fn for fn, rel in self.visited_functions.items()
                    if rel is True and fn.split("@")[0] not in self.chain_checked and fn in self.read_functions
                ]
                # 相关函数的调用点线索中，还没有 read_function 读过的
                unread_leads = self._unread_leads()
                if pending_chain or unread_leads:
                    self.finish_rejected_once = True
                    rejected = True
                    parts = []
                    if pending_chain:
                        shown = pending_chain[:5]
                        parts.append(
                            "以下相关函数还没有调查调用链：\n"
                            + "\n".join(f"- {fn}（{self.read_functions[fn]['start_line']}-{self.read_functions[fn]['end_line']} 行）" for fn in shown)
                            + "\n请对它们执行 find_callers / find_callees。"
                        )
                    if unread_leads:
                        shown = unread_leads[:5]
                        parts.append(
                            "以下调用点线索还没有 read_function 读过实现（只 grep 到的位置不能作为证据引用）：\n"
                            + "\n".join(f"- {n} @ {f}" for n, f in shown)
                            + "\n请 read_function 读取与问题相关的调用方函数。"
                        )
                    observation = (
                        "【finish 被拒绝】证据链还不完整：\n" + "\n\n".join(parts)
                        + "\n确认没有遗漏的调用方契约后再 finish。"
                        + "\n（如果认为某些确实不需要调查，请在下一步的 thought 中说明理由）"
                    )
                    new_files = []
                    steps.append({
                        "step": step_num,
                        "thought": thought,
                        "action": action,
                        "action_input": action_input,
                        "reason": reason,
                        "observation": observation,
                        "files_accessed": [],
                        "rejected": True,
                    })
                    continue

            # 通用防循环：同一动作+同样参数重复执行 → 拒绝，返回上次结果摘要和当前状态
            canonical = (action, json.dumps(action_input, sort_keys=True, ensure_ascii=False))
            if action not in ("expand_recall", "finish") and self.action_counts[canonical] >= 1:
                prev = self.past_observations.get(canonical, "")
                prev_summary = prev[:300] + ("..." if len(prev) > 300 else "") if prev else "(无记录)"
                observation = (
                    f"【重复动作被拒绝】你已经执行过 {action} 同样的参数，结果不会变化。\n"
                    f"上次执行的结果：\n{prev_summary}\n"
                    f"当前已读函数：{len(self.visited_functions)} 个，文件状态：{len(self.file_status)} 个已标记。\n"
                    "请换一个未探索的方向：\n"
                    "- 对相关函数用 find_callers / find_callees 扩展调用链\n"
                    "- 用 list_functions / list_files 探索同文件/同目录\n"
                    "- 用 expand_recall 看更多召回候选\n"
                    "- 用 search_symbol 换关键词搜索"
                )
                new_files = []
                rejected = True
                # 早期纠偏：第 2 次被拒起升级干预，给出具体的未完成任务清单
                self.rejection_count += 1
                if self.rejection_count >= 2:
                    rel_funcs = [k for k, v in self.visited_functions.items() if v is True]
                    leads = self._unread_leads()[:5]
                    escalation = (
                        f"\n\n【系统升级干预】这已是你第 {self.rejection_count} 次重复无效动作，说明当前探索方式陷入僵局。"
                        f"\n当前状态：{len(rel_funcs)} 个相关函数，{len(self._unread_leads())} 个未读调用点线索。"
                    )
                    if leads:
                        escalation += (
                            "\n请立即从以下未读调用点中选择一个 read_function（它们是你标记相关的函数的实际调用方）：\n"
                            + "\n".join(f"- {n} @ {f}" for n, f in leads)
                        )
                    elif not rel_funcs:
                        hint = f"试试 search_symbol 搜索：{', '.join(self.keyword_hints[:3])}。" if self.keyword_hints else ""
                        escalation += f"\n你还没有找到任何相关函数。{hint}或者 expand_recall 看更多候选。"
                    else:
                        escalation += "\n对相关函数执行 find_callers / find_callees 补全调用链，或者直接 finish 总结已有证据。"
                    observation += escalation
            else:
                self.action_counts[canonical] += 1
                rejected = False
                try:
                    observation, new_files = self.execute(action, action_input)
                    self.past_observations[canonical] = observation
                except Exception as e:
                    # 工具异常显式暴露：打 stderr + 完整留在 trace，不静默吞掉
                    print(f"[TOOL_ERROR] {action}({action_input}): {e}", file=sys.stderr, flush=True)
                    observation = f"[TOOL_ERROR] 工具 {action} 执行出错: {type(e).__name__}: {e}"
                    new_files = []

            # 同一文件反复 read_function：提示该文件调查状态，建议扩展
            if action == "read_function" and new_files:
                fp = new_files[0]
                self.file_read_counts[fp] += 1
                if self.file_read_counts[fp] > 3:
                    funcs_seen = [k.split("@")[0] for k in self.read_functions if k.split("@", 1)[1] == fp]
                    observation += (
                        f"\n\n注意：你已读取 {fp} 中 {len(funcs_seen)} 个函数（{', '.join(funcs_seen)}）。"
                        "如果该文件与问题相关，建议用 find_callers/find_callees 扩展调用链；"
                        "如果都不相关，请用 mark_file_irrelevant 标记后换方向。"
                    )

            # 方向纠偏：第 6/10 步仍没有任何相关函数标记 → 提示当前方向可能错误
            if step_num in (6, 10) and not any(v is True for v in self.visited_functions.values()):
                self.direction_warnings += 1
                hint = f"系统推测的相关符号：{', '.join(self.keyword_hints)}。" if self.keyword_hints else ""
                observation += (
                    f"\n\n【系统提示】已进行 {step_num} 步，你还没有标记任何相关函数，当前方向很可能是错的。{hint}"
                    "请认真考虑换方向：\n"
                    "- 用 search_symbol 验证系统推测的符号，定位真正相关的子系统\n"
                    "- 用 expand_recall 查看召回池的后续候选\n"
                    "- 用 list_files 探索你还没看过的其他目录\n"
                    "不要继续在已确认不相关的方向上消耗步数。"
                )

            # 记录访问证据
            for fp in new_files:
                self.visited_files.add(fp)
            if action == "read_lines" and new_files:
                files_content[new_files[0]].append(observation)

            steps.append({
                "step": step_num,
                "thought": thought,
                "action": action,
                "action_input": action_input,
                "reason": reason,
                "observation": observation,
                "files_accessed": new_files,
                "rejected": rejected,
            })

            # 每 5 步监督 agent 评估方向（finish 后不评估）
            if action != "finish" and step_num % 5 == 0 and step_num < self.max_steps:
                self._call_supervisor(question, steps, model)

            if action == "finish":
                break

        # ── 结束前兜底取证：相关函数还有未读调用点线索 → 自动补读 top-3 ──
        # （finish 守卫只对主动 finish 生效，撞步数上限的题会带着未读线索进答案阶段）
        backfilled = []
        for name, fp in self._unread_leads()[:3]:
            meta: dict = {}
            obs, nf = tool_read_function(name, fp, self.repo_root, out_meta=meta)
            if nf:
                self.func_file_map.setdefault(name, set()).add(nf[0])
                self.read_functions[f"{name}@{nf[0]}"] = meta
                self.visited_files.add(nf[0])
                backfilled.append(f"{name}@{nf[0]}")

        # ── 关键词兜底读：系统抽取的关键词，若没有任何已读函数名包含它，
        #    直接从名称索引读最匹配的 top-1（精确 > 前缀 > 包含）──
        #    注：族级兜底（每族强制补读）已证明是净负面（V42：噪声函数稀释证据焦点，
        #    003/012 回退）；族信息只展示给 Agent 选择，不强制读
        if self.chunks_by_id:
            read_names = " ".join(k.split("@")[0].lower() for k in self.read_functions)
            for kw in self.keyword_hints:
                if len(backfilled) >= 3:
                    break
                kwl = kw.lower()
                if len(kwl) < 4 or kwl in read_names:
                    continue
                matches = []
                for fid, ch in self.chunks_by_id.items():
                    meta2 = ch.get("meta") or {}
                    nl = (meta2.get("name") or "").lower()
                    if kwl in nl:
                        rank = 0 if nl == kwl else (1 if nl.startswith(kwl) else 2)
                        matches.append((rank, len(nl), meta2))
                if not matches:
                    continue
                matches.sort(key=lambda x: (x[0], x[1]))
                meta2 = matches[0][2]
                key = f"{meta2['name']}@{meta2['file_path']}"
                if key in self.read_functions:
                    continue
                meta: dict = {}
                obs, nf = tool_read_function(meta2["name"], meta2["file_path"], self.repo_root, out_meta=meta)
                if nf:
                    self.func_file_map.setdefault(meta2["name"], set()).add(nf[0])
                    self.read_functions[key] = meta
                    self.visited_files.add(nf[0])
                    backfilled.append(key)
        self.backfilled = backfilled

        # ── 生成答案：结构化证据传递，不做粗暴截断 ──

        # 决策日志：read 类动作只记决策（内容在证据区），其他动作保留完整 observation
        log_lines = []
        for s in steps:
            entry = f"Step {s['step']}: {s['action']}({json.dumps(s['action_input'], ensure_ascii=False)})"
            if s.get("rejected"):
                entry += " [被拒绝]"
            entry += f"\n  Thought: {s['thought']}"
            if s["action"] not in ("read_function", "read_lines"):
                entry += f"\n  结果: {s['observation']}"
            log_lines.append(entry)
        decision_log = "\n\n".join(log_lines)

        # 已读函数清单（含相关性标记）
        func_list_lines = []
        for key, meta in self.read_functions.items():
            rel = self.visited_functions.get(key)
            rel_str = "相关" if rel is True else "不相关" if rel is False else "未标记"
            func_list_lines.append(f"- {meta['name']} @ {meta['file']}:{meta['start_line']}-{meta['end_line']} ({rel_str})")
        read_function_list = "\n".join(func_list_lines) or "(没有读过任何函数)"

        # 证据内容：相关/未标记函数的完整实现；不相关函数只列名不给代码
        # 单个函数超过 30000 字符时保留头尾并明确标注省略行数（非任意截断）
        evidence_parts = []
        for key, meta in self.read_functions.items():
            if self.visited_functions.get(key) is False:
                continue
            code = meta["code"]
            if len(code) > 30000:
                head, tail = code[:15000], code[-10000:]
                elided_lines = code[15000:-10000].count("\n")
                code = head + f"\n... (函数过长，中间省略 {elided_lines} 行) ...\n" + tail
            evidence_parts.append(
                f"=== {meta['name']} @ {meta['file']}:{meta['start_line']}-{meta['end_line']} ===\n```cpp\n{code}\n```"
            )
        # read_lines 读到的非函数区域（宏、全局变量等）
        for fp, blocks in files_content.items():
            evidence_parts.append(f"=== {fp} (行片段) ===\n" + "\n\n".join(blocks))
        evidence_content = "\n\n".join(evidence_parts) or "(没有收集到任何证据)"

        prompt = ANSWER_PROMPT.format(
            question=question,
            decision_log=decision_log,
            read_function_list=read_function_list,
            evidence_content=evidence_content,
        )

        answer = call_llm(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=None,
            model=model,
            _usage_sink=self.usage,
        )

        # ── 引用自检回路：相关函数所在文件未出现在答案中 → 带缺失清单补一轮 ──
        answer_repair = None
        relevant_files = sorted({
            meta["file"] for key, meta in self.read_functions.items()
            if self.visited_functions.get(key) is True
        })
        missing_citations = [f for f in relevant_files if f not in answer]
        if missing_citations:
            missing_detail = []
            for f in missing_citations:
                funcs = [
                    f"{meta['name']}({meta['start_line']}-{meta['end_line']}行)"
                    for key, meta in self.read_functions.items()
                    if meta["file"] == f and self.visited_functions.get(key) is True
                ]
                missing_detail.append(f"- {f}：你读过并标记相关的函数 {', '.join(funcs)}")
            repair_msg = (
                "你的答案遗漏了以下你实际读过并标记为相关的文件，它们没有出现在引用中：\n"
                + "\n".join(missing_detail)
                + "\n\n请重写完整答案：纳入对上述文件中所读函数的分析和引用（引用格式 file:start-end）；"
                "如果认为某个文件确实不该引用，必须在答案中明确说明理由。其他规则不变。"
            )
            answer_v2 = call_llm(
                messages=[
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": answer},
                    {"role": "user", "content": repair_msg},
                ],
                max_tokens=None,
                model=model,
                _usage_sink=self.usage,
            )
            if answer_v2 and answer_v2.strip():
                answer_repair = {"missing_files": missing_citations, "original_answer": answer}
                answer = answer_v2

        # ── 引用净化：答案引用了从未读过的文件 → 要求删除或改为"无法确认" ──
        cited_paths = set(re.findall(r"[\w\-+.]+/[\w\-+./]+\.(?:cpp|cc|cxx|hpp|hh|hxx|c|h)\b", answer))
        read_files = {meta["file"] for meta in self.read_functions.values()} | set(files_content.keys())
        unvisited_citations = sorted(p for p in cited_paths if p not in read_files)
        if unvisited_citations:
            sanitize_msg = (
                "你的答案引用了以下你从未实际读取过的文件（只出现在检索结果或候选列表里，不能作为证据）：\n"
                + "\n".join(f"- {p}" for p in unvisited_citations)
                + "\n\n请重写完整答案：删除对这些文件的引用和相关论断；"
                "如果删掉后某些结论失去依据，如实改为\"无法确认\"。其他规则不变。"
            )
            answer_v3 = call_llm(
                messages=[
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": answer},
                    {"role": "user", "content": sanitize_msg},
                ],
                max_tokens=None,
                model=model,
                _usage_sink=self.usage,
            )
            if answer_v3 and answer_v3.strip():
                answer_repair = answer_repair or {}
                answer_repair["unvisited_citations"] = unvisited_citations
                answer_repair.setdefault("original_answer", answer)
                answer = answer_v3

        token_usage = {
            "prompt_tokens": sum(getattr(u, "prompt_tokens", 0) or 0 for u in self.usage),
            "completion_tokens": sum(getattr(u, "completion_tokens", 0) or 0 for u in self.usage),
            "llm_calls": len(self.usage),
        }

        return {
            "answer": answer,
            "steps": steps,
            "visited_files": sorted(self.visited_files),
            "visited_functions": self.visited_functions,
            "file_status": self.file_status,
            "recall_expansions": self.recall_expansions,
            "answer_repair": answer_repair,
            "direction_warnings": self.direction_warnings,
            "supervisor_notes": self.supervisor_notes,
            "backfilled": self.backfilled,
            "skipped_unread": self.skipped_unread,
            "token_usage": token_usage,
        }


# ── Main ────────────────────────────────────────────────────────────

def extract_keywords_llm(question: str, model: str) -> list[str]:
    """用 LLM 从中文问题推测 C++ 代码中可能出现的英文标识符（HyDE 轻量版）。"""
    prompt = (
        "以下是一个 C++ 仓库的代码审计中文问题。请推测仓库代码中可能出现的英文函数名/变量名/关键词标识符"
        "（ snake_case 或 CamelCase，如 parse_config、init_backend、handle_request 风格）。\n"
        "只输出 JSON，格式：{\"keywords\": [\"...\"]}，最多 8 个，不要解释。\n\n"
        f"问题：{question}"
    )
    try:
        r = call_llm_json(messages=[{"role": "user", "content": prompt}], max_tokens=300, model=model)
        if isinstance(r, dict):
            kws = r.get("keywords", [])
            return [k for k in kws if isinstance(k, str)][:8]
    except Exception as e:
        print(f"[WARN] 关键词抽取失败: {e}", flush=True)
    return []


def refine_keywords_llm(question: str, keywords: list[str], families: dict, model: str) -> list[str]:
    """符号假设验证回路：把上一轮猜测的验证结果反馈给 LLM，让它修正/补充。

    families: build_keyword_families 的输出（含命中族和示例函数）。
    """
    hit_lines, missed = [], []
    for kw in keywords:
        fams = families.get(kw)
        if fams:
            ex = "；".join(f"{fam or '(无前缀)'}（如 {names[0][0]}）" for fam, names in fams[:3])
            hit_lines.append(f"- 命中 '{kw}': {ex}")
        else:
            missed.append(kw)
    prompt = (
        "你在猜一个 C++ 仓库里实际存在的函数名。以下是上一轮猜测的验证结果：\n\n"
        + ("\n".join(hit_lines) or "（全部未命中）")
        + f"\n\n未命中: {', '.join(missed) or '（无）'}\n\n"
        f"原始问题：{question}\n\n"
        "请输出修正后的符号列表：保留命中的；对未命中的，参考命中族的命名风格推测更可能存在的变体"
        "（换动词/名词组合、加常见模块前缀、用更通用的术语）。\n"
        "只输出 JSON，格式：{\"keywords\": [\"...\"]}，最多 10 个。"
    )
    try:
        r = call_llm_json(messages=[{"role": "user", "content": prompt}], max_tokens=400, model=model)
        if isinstance(r, dict):
            return [k for k in r.get("keywords", []) if isinstance(k, str)][:10]
    except Exception as e:
        print(f"[WARN] 关键词修正失败: {e}", flush=True)
    return []


def lookup_functions_by_keywords(keywords: list[str], chunks_by_id: dict, limit: int = 10) -> list[dict]:
    """用 LLM 抽取的关键词直接查函数名索引，命中即作为高优先候选。"""
    hits = []
    seen = set()
    for kw in keywords:
        kwl = kw.lower()
        if len(kwl) < 4:
            continue
        for fid, ch in chunks_by_id.items():
            if fid in seen:
                continue
            name = (ch.get("meta") or {}).get("name", "").lower()
            if kwl in name:
                seen.add(fid)
                meta = ch["meta"]
                hits.append({
                    "fid": fid,
                    "name": meta.get("name", ""),
                    "file_path": meta.get("file_path", ""),
                    "start_line": meta.get("start_line", 0),
                    "end_line": meta.get("end_line", 0),
                    "signature": meta.get("signature", ""),
                    "score": 1.0,  # 名称精确匹配，排最前
                    "name_match": True,
                })
                if len(hits) >= limit:
                    return hits
    return hits


def build_keyword_families(keywords: list[str], chunks_by_id: dict, max_families: int = 4) -> dict:
    """关键词 → 函数族映射。

    同一关键词可能对应多层/多子系统实现（如 chat_template 命中
    llama_model_* / common_chat_* / llm_chat_* 三族），按"关键词前的名称前缀"分族。
    返回 {kw: [(family_prefix, [(name, file_path), ...]), ...]}，族按命中数降序。
    """
    from collections import defaultdict
    out = {}
    for kw in keywords:
        kwl = kw.lower()
        if len(kwl) < 4:
            continue
        fams = defaultdict(list)
        for ch in chunks_by_id.values():
            meta = ch.get("meta") or {}
            name = meta.get("name") or ""
            nl = name.lower()
            if kwl in nl:
                fam = nl.split(kwl)[0]
                fams[fam].append((name, meta.get("file_path", "")))
        if fams:
            ranked = sorted(fams.items(), key=lambda kv: -len(kv[1]))[:max_families]
            out[kw] = [(fam, names[:3]) for fam, names in ranked]
    return out


def probe_directories_by_keywords(keywords: list[str], chunks_by_id: dict, top_k: int = 5) -> list[tuple[str, int, list[str]]]:
    """目录级关键词探针：统计每个目录下名称命中关键词的函数数。

    返回 [(目录, 命中数, [命中函数名示例])]，按命中数降序。
    用于"问题不点名子系统"时给 Agent 数据支撑的方向提示（后端识别硬骨头）。
    """
    from collections import defaultdict
    dir_hits = defaultdict(list)  # dir -> [函数名]
    kws = [k.lower() for k in keywords if isinstance(k, str) and len(k) >= 4]
    if not kws:
        return []
    for ch in chunks_by_id.values():
        meta = ch.get("meta") or {}
        name = (meta.get("name") or "").lower()
        fp = meta.get("file_path") or ""
        if not name or not fp:
            continue
        for kw in kws:
            if kw in name:
                # 取前三层目录作为子系统粒度（如 ggml/src/ggml-sycl），不足则取现有层级
                parts = fp.split("/")
                subsys = "/".join(parts[:3]) if len(parts) > 3 else "/".join(parts[:-1]) or parts[0]
                dir_hits[subsys].append(meta["name"])
                break
    ranked = sorted(dir_hits.items(), key=lambda kv: -len(kv[1]))
    return [(d, len(names), names[:5]) for d, names in ranked[:top_k]]


def process_one(item, q_emb, ca, retriever, chunks_by_id, repo_root, model, max_steps):
    question = item["question"]
    qa_id = item["qa_id"]

    # 1. Concept-Symbol retrieval：一次性取 top-100 候选池，Agent 首批看到 20 个
    #    LLM 抽取的关键词补充 symbol 匹配（中文问题正则抽不到多少标识符）
    #    关键词多采样取并集，降低单次抽取的运气成分（001 曾因此全零）
    kw_a = extract_keywords_llm(question, model)
    kw_b = extract_keywords_llm(question, model)
    extra_symbols = list(dict.fromkeys(kw_a + kw_b))[:12]
    # 符号假设验证回路：命中/未命中结果反馈给 LLM 修正一轮（002 案例：修正轮直接猜中核心符号）
    fams_v1 = build_keyword_families(extra_symbols, chunks_by_id)
    refined = refine_keywords_llm(question, extra_symbols, fams_v1, model)
    if refined:
        extra_symbols = list(dict.fromkeys(extra_symbols + refined))[:16]
    recall_pool = concept_symbol_retrieve_functions(
        question, q_emb, ca, retriever, chunks_by_id, top_concepts=20, top_funcs=100,
        extra_symbols=extra_symbols,
    )

    # 目录级关键词探针：统计各子系统目录的名称命中密度，给 Agent 方向提示
    dir_probes = probe_directories_by_keywords(extra_symbols, chunks_by_id)
    # 关键词 → 函数族映射：同一关键词可能对应多层实现（002 案例：
    # chat_template 有 llama_model_/common_chat_/llm_chat_ 三族，只读一族会漏定义层）
    keyword_families = build_keyword_families(extra_symbols, chunks_by_id)

    # 2. 并入纯 embedding top-50 兜底：concept 层漏掉时召回池仍有正确方向
    #    RRF（Reciprocal Rank Fusion）融合，避免两路分数尺度不同导致兜底沉底（031 案例）
    emb_results = retriever.retrieve(np.asarray([q_emb], dtype=np.float32), top_k=50)
    rrf = {}
    entries = {}
    for rank, f in enumerate(recall_pool):
        rrf[f["fid"]] = rrf.get(f["fid"], 0.0) + 1.0 / (60 + rank + 1)
        entries[f["fid"]] = f
    for rank, r in enumerate(emb_results):
        md = r["metadata"]
        fid = f"{md['file_path']}:{md['name']}:{md['start_line']}"
        rrf[fid] = rrf.get(fid, 0.0) + 1.0 / (60 + rank + 1)
        entries.setdefault(fid, {
            "fid": fid,
            "name": md.get("name", ""),
            "file_path": md.get("file_path", ""),
            "start_line": md.get("start_line", 0),
            "end_line": md.get("end_line", 0),
            "signature": md.get("signature", ""),
            "score": r.get("score", 0.0),
        })
    recall_pool = sorted(entries.values(), key=lambda f: -rrf[f["fid"]])
    for f in recall_pool:
        f["score"] = rrf[f["fid"]]

    # MMR 多样性重排：同目录惩罚，避免单一后端/目录占满首屏（后端题的"起始地图错"根因）
    dir_of = lambda fp: str(Path(fp).parent)
    selected, remaining = [], list(recall_pool)
    while remaining:
        best_i, best_v = 0, -1.0
        for i, f in enumerate(remaining):
            same_dir = sum(1 for s in selected if dir_of(s["file_path"]) == dir_of(f["file_path"]))
            v = f["score"] / (1.0 + 0.5 * same_dir)
            if v > best_v:
                best_v, best_i = v, i
        selected.append(remaining.pop(best_i))
    recall_pool = selected

    # 3. 关键词直接召回（条件注入）：只在召回池与关键词完全对不上时才插到最前，
    #    避免在池子本来就好的题上劫持出发点（021 案例：注入 trim_leading_space 投毒）
    pool_top_names = " ".join(f["name"].lower() for f in recall_pool[:20])
    pool_aligned = any(
        kw.lower() in pool_top_names for kw in extra_symbols if isinstance(kw, str) and len(kw) >= 4
    )
    if not pool_aligned:
        keyword_hits = lookup_functions_by_keywords(extra_symbols, chunks_by_id)
        keyword_hits = [h for h in keyword_hits if h["fid"] not in rrf]
        recall_pool = keyword_hits + recall_pool

    # 3. ReAct 调查
    agent = ReactAgent(repo_root, max_steps=max_steps, recall_pool=recall_pool,
                       keyword_hints=extra_symbols, retriever=retriever, chunks_by_id=chunks_by_id,
                       dir_probes=dir_probes, keyword_families=keyword_families)
    result = agent.investigate(question, model)

    return {
        "qa_id": qa_id,
        "question": question,
        "answer": result["answer"],
        "initial_functions": recall_pool[:agent.recall_shown],
        "visited_files": result["visited_files"],
        "visited_functions": result["visited_functions"],
        "file_status": result["file_status"],
        "recall_expansions": result["recall_expansions"],
        "answer_repair": result["answer_repair"],
        "direction_warnings": result["direction_warnings"],
        "supervisor_notes": result["supervisor_notes"],
        "backfilled": result["backfilled"],
        "skipped_unread": result.get("skipped_unread", {}),
        "token_usage": result["token_usage"],
        "steps": result["steps"],
        "model": model,
    }


def main():
    parser = argparse.ArgumentParser(description="Concept-Symbol + ReAct QA")
    parser.add_argument("--model", default=None, help="LLM model")
    parser.add_argument("--max-steps", type=int, default=25)
    parser.add_argument("--workers", type=int, default=30)
    parser.add_argument("--output", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--qa-ids", default=None,
                        help="只跑指定题号（逗号分隔，如 posthoc_public_001,posthoc_public_013），用于失败子集冒烟")
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

    if args.qa_ids:
        wanted = {s.strip() for s in args.qa_ids.split(",") if s.strip()}
        pairs = [(it, qe) for it, qe in zip(items, q_embs) if it["qa_id"] in wanted]
        if not pairs:
            print(f"[ERROR] --qa-ids 没有匹配到任何题目: {args.qa_ids}")
            return
        items = [p[0] for p in pairs]
        q_embs = [p[1] for p in pairs]
        print(f"--qa-ids 筛选: {len(items)} 题")

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
                import traceback
                print(f"[ERROR] idx={idx}: {e}\n{traceback.format_exc()}", flush=True)

    qa_id_to_idx = {item["qa_id"]: i for i, item in enumerate(items)}
    results.sort(key=lambda r: qa_id_to_idx.get(r["qa_id"], 0))

    if args.output:
        output_path = Path(args.output)
    else:
        suffix = model.replace("/", "_")
        output_path = _ROOT / "results" / f"qa_react_concept_symbol_{suffix}.json"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\nSaved results to: {output_path}")


if __name__ == "__main__":
    main()
