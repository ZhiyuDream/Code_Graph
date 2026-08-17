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
    tool_search_codebase,
    tool_scan_directory,
)
from src.qa.tools.grep_call_chain import grep_callers
from src.qa.triage import triage_functions_llm, triage_functions_batched, redescribe_missing_function
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
# prompt 模板统一放 prompts/ 目录，用 load_prompt 加载模板（缓存一次），调用处 .format 填充

from src.core.prompt_loader import load_prompt

REACT_PROMPT = load_prompt("react_investigation")
ANSWER_PROMPT = load_prompt("react_answer")
SUPERVISOR_PROMPT = load_prompt("react_supervisor")


def load_skill() -> str:
    """加载调查策略 skill（SkillOpt 试点的可训练文本状态）。

    优先级：QA_SKILL_PATH 环境变量 > data/qa_skill.md > prompts/qa_skill_default.txt
    """
    import os
    path = os.environ.get("QA_SKILL_PATH")
    if path and Path(path).exists():
        return Path(path).read_text(encoding="utf-8")
    trained = _ROOT / "data" / "qa_skill.md"
    if trained.exists():
        return trained.read_text(encoding="utf-8")
    return load_prompt("qa_skill_default")


class ReactAgent:
    RECALL_BATCH = 20

    def __init__(self, repo_root: Path, max_steps: int = 25, recall_pool: list[dict] | None = None,
                 keyword_hints: list[str] | None = None,
                 retriever=None, chunks_by_id: dict | None = None,
                 dir_probes: list | None = None, keyword_families: dict | None = None,
                 memory=None, file_maps: dict | None = None,
                 file_recon: bool = False, file_map_cache: dict | None = None):
        self.repo_root = repo_root
        self.max_steps = max_steps
        self.recall_pool = recall_pool or []
        self.keyword_hints = keyword_hints or []
        self.dir_probes = dir_probes or []
        self.file_maps = file_maps or {}      # 文件中心模式：file -> File Map（函数清单）
        self.file_recon = file_recon          # 进文件自动侦察：首次 read 成功附 File Map
        self.file_map_cache = file_map_cache  # 跨题共享的 File Map 缓存（--file-recon）
        self.files_shown_maps = set()         # 已展示过 File Map 的文件（自动侦察用）
        self.memory = memory  # 跨题调查记忆（InvestigationMemory，可为 None）
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
        self.suspicious_recalled = False  # "可疑"轻量干预（重新召回）已用过
        self.last_new_relevant_step = 0    # 最近一次新增相关函数的步号（提前 finish 提示用）
        self.early_finish_hinted = False   # 提前 finish 提示只给一次

        # ── Frontier 调查账本 ──
        # frontier: name@file -> {name, file, relation, source, why, discovered_step, priority}
        # 重复 find_callers/find_callees 时返回缓存状态而非单纯拒绝（消灭 51% 重复调用）
        self.frontier: dict = {}
        self.frontier_events: list = []    # 漏斗分析用日志 [{step, node, source, relation, status}]
        self.callers_checked: set = set()  # 已查过 callers 的函数（bare name）
        self.callees_checked: set = set()  # 已查过 callees 的函数
        # 初始召回首屏作为种子进入 frontier
        for i, f in enumerate(self.recall_pool[:self.recall_shown]):
            self._frontier_add(f["name"], f["file_path"], relation="SEED", source="初始召回",
                               why=f"初始召回候选（rank {i + 1}）", step=0)
            key = f"{f['name']}@{f['file_path']}"
            if key not in self.frontier:
                continue
            # 分诊高分（≥8.5/10）的召回种子直接给 HIGH：分诊是子 agent 读过完整代码后的判断，
            # 是最强先验，必须进自动跟进的瞄准镜（032 案例：gold 池内 rank 1、分诊 10 分，
            # 但种子只 MEDIUM、自动跟进只盯 HIGH，三发子弹全打在非 gold 上）
            if isinstance(f.get("triage"), (int, float)) and f["triage"] >= 8.5:
                self.frontier[key]["priority"] = "HIGH"
            # 召回 top-10 是检索系统的次强先验，至少 MEDIUM（不再被 callers 线索压底）
            elif i < 10 and self.frontier[key]["priority"] == "LOW":
                self.frontier[key]["priority"] = "MEDIUM"
        self.backfilled = []           # 结束前兜底补读的调用点
        self.skipped_unread = {}       # 分诊跳过的候选: name@file -> reason
        # 内容分诊低分（≤3/10）的候选直接进 skipped_unread（不误标 visited=False；
        # finish 守卫 A 会在零相关+多跳过时强制回读验证，兜住分诊误杀）
        for f in self.recall_pool:
            if isinstance(f.get("triage"), (int, float)) and f["triage"] <= 3.0:
                key = f"{f['name']}@{f['file_path']}"
                self.skipped_unread.setdefault(key, f"内容分诊低分({f['triage']}): {f.get('triage_reason', '')}")
        self.triage_rejected_once = False  # 分诊误杀兜底只拒一次
        self.skill = load_skill()      # 调查策略（SkillOpt 可训练文本状态）
        self.func_file_map = {}       # function_name -> file_path（read_function 成功时记录）
        self.file_status = {}         # file_path -> "relevant" / "irrelevant"
        self.action_counts = Counter()  # (action, canonical_input) -> count，通用防循环
        self.file_read_counts = defaultdict(int)  # read_function 按文件计数
        self.repo_structure = self._get_repo_structure(repo_root, max_depth=2)
        # ── 目录账本（人类式目录记忆）──
        self._dir_files_cache = {}   # directory -> [code file names]，避免重复 FS 遍历
        self._sibling_hinted = set() # 已给过"同目录未探索文件"提示的目录
        self._pending_sibling_files = []  # 新标相关的文件（等下一步 observation 附带同目录提示）
        self._all_names_blob = None  # 全量函数名小写 blob（监督关键词存在性校验用，惰性构建）
        self.replay_counts = Counter()  # 回放计数：同一内容回放 2 次后转硬拒绝，防"监督让重读"空转
        self.auto_follow_count = 0   # HIGH 线索挂起自动跟进次数（每次调查限 3 次）
        self.file_line_ranges = defaultdict(list)  # read_lines 已读区间 file -> [(s,e)]，区间覆盖防换皮重读
        self._last_supervisor_step = 0  # 监督按"已完成步数"触发，回放/守卫 continue 不再跳过监督

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
                # 函数知识记忆命中（仅限**此前真实读过完整实现**且**摘录完整**的函数）：
                # 返回缓存的职责+代码摘要，免重读（省 token），可引用。
                # 长函数（摘录被截断）不走捷径，落到下面真实重读——防止把片段当完整实现。
                if self.memory is not None and fp_clean:
                    entry = self.memory.read_cache.get(f"{fn}@{fp_clean}")
                    # 兼容旧缓存（无 truncated 标记）：摘录满 1500 字符按截断处理
                    entry_complete = entry and not entry.get(
                        "truncated", len(entry.get("code_excerpt", "")) >= 1500)
                    if entry and entry_complete:
                        obs_parts.append(
                            f"【记忆命中】函数 {fn}（{fp_clean}:{entry.get('start_line','?')}-{entry.get('end_line','?')}）\n"
                            f"职责：{entry['role']}\n```cpp\n{entry.get('code_excerpt','')}\n```\n"
                            "（来自此前调查的完整阅读，可直接引用此文件；如需更多请 read_lines）"
                        )
                        meta = {
                            "name": fn, "file": fp_clean,
                            "start_line": entry.get("start_line", 0),
                            "end_line": entry.get("end_line", 0),
                            "code": entry.get("code_excerpt", ""),
                        }
                        self.func_file_map.setdefault(fn, set()).add(fp_clean)
                        self.read_functions[f"{fn}@{fp_clean}"] = meta
                        all_files.append(fp_clean)
                        continue
                meta: dict = {}
                obs, nf = tool_read_function(fn, fp, self.repo_root, out_meta=meta)
                obs_parts.append(obs)
                used += len(obs)
                if nf:
                    self.func_file_map.setdefault(fn, set()).add(nf[0])
                    self.read_functions[f"{fn}@{nf[0]}"] = meta
                    self._frontier_mark_read(fn, nf[0], getattr(self, "current_step", 0))
                    all_files.append(nf[0])
                    # 自动侦察：首次进入文件时附上全文件函数清单（013 教训：
                    # 进了对文件没读关键函数——先给完整函数目录，再让 Agent 从清单里选）
                    # file-centric 模式用预建 file_maps；--file-recon 模式按需从名称索引构建
                    if nf[0] not in self.files_shown_maps:
                        fmap = self.file_maps.get(nf[0])
                        if fmap is None and self.file_recon and self.chunks_by_id:
                            cache = self.file_map_cache if self.file_map_cache is not None else self.file_maps
                            if nf[0] not in cache:
                                cache[nf[0]] = build_file_map(nf[0], self.chunks_by_id)
                            fmap = cache.get(nf[0])
                        if fmap:
                            self.files_shown_maps.add(nf[0])
                            listing = ", ".join(f"{f['name']}({f['start_line']}-{f['end_line']})" for f in fmap[:60])
                            obs_parts.append(
                                f"【File Map】{nf[0]} 共 {len(fmap)} 个函数：\n{listing}\n"
                                "（这是该文件的完整函数清单，找目标函数先看这里）"
                            )
                    # 真实读完写入缓存（职责+摘录，供后续问题免重读）
                    if self.memory is not None and meta.get("code"):
                        self.memory.note_read(fn, nf[0], meta["code"], meta.get("start_line", 0), meta.get("end_line", 0))
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
            fp = action_input.get("file_path", "")
            if isinstance(fp, list):  # LLM 偶尔传数组，逐个查
                parts, files = [], []
                for p in fp[:3]:
                    o, _ = tool_list_functions(p, self.repo_root)
                    parts.append(o)
                return "\n\n".join(parts), files
            return tool_list_functions(fp, self.repo_root)

        if action == "list_files":
            d = action_input.get("directory", "")
            if isinstance(d, list):
                parts = []
                for p in d[:3]:
                    o, _ = tool_list_files(p, self.repo_root)
                    parts.append(o)
                return "\n\n".join(parts), []
            return tool_list_files(d, self.repo_root)

        if action == "scan_directory":
            d = action_input.get("directory", "") or action_input.get("file_path", "")
            if isinstance(d, list):
                d = d[0] if d else ""
            obs, files = tool_scan_directory(d, self.repo_root, keywords=self.keyword_hints)
            # 标注已读/已结案状态，避免重复探索（目录账本的读侧）
            ann = []
            for line in obs.split("\n"):
                if line.startswith("■ "):
                    fname = line[2:].split("（")[0]
                    fp = f"{d.rstrip('/')}/{fname}"
                    st = self.file_status.get(fp)
                    n_read = sum(1 for k in self.read_functions if k.split("@", 1)[-1] == fp)
                    marks = []
                    if st == "relevant":
                        marks.append("含相关函数")
                    elif st == "irrelevant":
                        marks.append("已结案:不相关")
                    if n_read:
                        marks.append(f"已读{n_read}函数")
                    if marks:
                        line = line.rstrip() + f"  [{', '.join(marks)}]"
                ann.append(line)
            return "\n".join(ann), files

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
                self.callers_checked.add(n)
                step = getattr(self, "current_step", 0)
                # 记录调用点线索（供 finish 守卫检查是否已读）+ 进 frontier 账本
                leads = [(c["name"], c["file"]) for c in found if c.get("name")]
                if leads:
                    self.caller_leads.setdefault(n, []).extend(leads)
                    for cname, cfile in leads:
                        self._frontier_add(cname, cfile, relation="CALLER", source=n,
                                           why=f"find_callers({n}) 的调用方", step=step)
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
            self.callees_checked.update(names)
            step = getattr(self, "current_step", 0)
            obs_parts = []
            for func_name in names:
                callees_meta: list = []
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
                obs, _ = tool_find_callees(func_name, file_path, start, end, self.repo_root, out_meta=callees_meta)
                obs_parts.append(obs)
                # callees 进 frontier：grep_callees 只能给出名字，归属文件要用名称索引解析
                # （002/014 案例：llama_model_chat_template 进 frontier 时 file 错记为当前文件，
                #  Agent 按错文件读不到，线索烂在 frontier 里）
                resolved_notes = []
                for c in callees_meta:
                    cname = c["name"]
                    real_file = self._resolve_symbol_file(cname, fallback=file_path)
                    c["file"] = real_file
                    added = self._frontier_add(cname, real_file, relation="CALLEE",
                                               source=func_name, why=f"find_callees({func_name}) 的被调方", step=step)
                    if added and real_file != file_path:
                        resolved_notes.append(f"{cname} → {real_file}")
                if resolved_notes:
                    obs_parts.append(
                        "【跨文件被调方】以下被调函数定义在其他文件（已加入待调查 frontier）：\n"
                        + "\n".join(f"- {n}" for n in resolved_notes[:8])
                    )
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

        if action == "search_codebase":
            query = action_input.get("query", "") or action_input.get("symbol_name", "")
            obs, files, hits = tool_search_codebase(
                query, self.repo_root, retriever=self.retriever, chunks_by_id=self.chunks_by_id
            )
            # 发现的候选进入召回池前列 + frontier
            added = 0
            if hits:
                existing = {f["fid"] for f in self.recall_pool}
                new_hits = [h for h in hits if h["fid"] not in existing]
                self.recall_pool = new_hits + self.recall_pool
                self.recall_shown = min(self.recall_shown + len(new_hits), len(self.recall_pool))
                for h in hits:
                    if self._frontier_add(h["name"], h["file_path"], relation="SEARCH",
                                          source="search_codebase", why=f"search_codebase('{query}') 发现",
                                          step=getattr(self, "current_step", 0)):
                        added += 1
                obs += f"\n（{len(new_hits)} 个候选已加入召回池前列，{added} 个进入待调查 frontier）"
            return obs, files

        return f"未知工具: {action}", []

    # ── 目录账本 ─────────────────────────────────────────────────────

    def _dir_code_files(self, directory: str) -> list[str]:
        """目录下的代码文件名（带缓存）。"""
        if directory in self._dir_files_cache:
            return self._dir_files_cache[directory]
        names = []
        try:
            abs_dir = self.repo_root / directory
            if abs_dir.is_dir():
                names = sorted(
                    p.name for p in abs_dir.iterdir()
                    if p.is_file() and p.suffix in (".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx")
                )
        except Exception:
            pass
        self._dir_files_cache[directory] = names
        return names

    def _dir_ledger_text(self) -> str:
        """目录调查状态：每个摸过的目录一行——读了什么、结论是什么、还剩什么没看。"""
        # 收集有活动的目录：读过函数的文件 / 有状态标记的文件
        touched: dict[str, dict] = {}
        for key, meta in self.read_functions.items():
            fp = meta.get("file", key.split("@", 1)[-1])
            d = str(Path(fp).parent)
            e = touched.setdefault(d, {"read_rel": [], "read_unrel": [], "read_unmarked": 0})
            rel = self.visited_functions.get(key)
            name = meta.get("name", key.split("@")[0])
            if rel is True:
                e["read_rel"].append(Path(fp).name)
            elif rel is False:
                e["read_unrel"].append(Path(fp).name)
            else:
                e["read_unmarked"] += 1
        for fp, st in self.file_status.items():
            d = str(Path(fp).parent)
            touched.setdefault(d, {"read_rel": [], "read_unrel": [], "read_unmarked": 0})
        if not touched:
            return "(还没有探索过任何目录)"
        lines = []
        for d in sorted(touched):
            e = touched[d]
            all_files = self._dir_code_files(d)
            seen = set(e["read_rel"]) | set(e["read_unrel"])
            seen |= {Path(k.split("@", 1)[-1]).name for k in self.read_functions if str(Path(k.split("@", 1)[-1]).parent) == d}
            unexplored = [f for f in all_files if f not in seen]
            parts = []
            if e["read_rel"]:
                parts.append(f"相关: {', '.join(sorted(set(e['read_rel']))[:4])}")
            if e["read_unrel"]:
                parts.append(f"不相关: {', '.join(sorted(set(e['read_unrel']))[:4])}")
            if e["read_unmarked"]:
                parts.append(f"{e['read_unmarked']} 个已读未标")
            dir_closed = all(
                self.file_status.get(f'{d}/{f}') == "irrelevant" or f in seen
                for f in all_files
            ) and all_files and not e["read_rel"]
            head = f"- {d}/"
            if dir_closed:
                head += " 【已排除，勿回头】"
            body = "；".join(parts) or "（扫过未读函数）"
            tail = f"；未探索 {len(unexplored)} 个: {', '.join(unexplored[:6])}" if unexplored else "；目录内文件已全部看过"
            lines.append(head + " " + body + tail)
        return "\n".join(lines[:10])

    def _sibling_hint(self, newly_relevant_files: list[str]) -> str:
        """相关文件的同目录 sibling 提示：找到相关文件后，告知同目录还没看过什么（每目录一次）。"""
        hints = []
        for fp in newly_relevant_files:
            d = str(Path(fp).parent)
            if d in self._sibling_hinted:
                continue
            self._sibling_hinted.add(d)
            unexplored = []
            for name in self._dir_code_files(d):
                sib = f"{d}/{name}"
                if sib == fp:
                    continue
                if any(k.split("@", 1)[-1] == sib for k in self.read_functions):
                    continue
                if self.file_status.get(sib) == "irrelevant":
                    continue
                unexplored.append(name)
            if unexplored:
                hints.append(f"{d}/ 下还有 {len(unexplored)} 个未探索文件: {', '.join(unexplored[:8])}"
                             f"——相关文件的邻居常常也相关，可用 scan_directory(\"{d}\") 粗筛")
        return "\n".join(hints)

    def _top_nag(self) -> str:
        """每步置顶的"最重要未跟进线索"：从 frontier 取最高优先级、最早发现的一项。

        动机（V2-013/044 轨迹）：正确线索在 observation/frontier 里被明确展示过，
        但位置不显眼，模型扫一眼就过去继续老循环——需要一个每步都在的置顶提醒。
        """
        if not self.frontier:
            return "(无)"
        prio_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
        items = sorted(self.frontier.values(),
                       key=lambda e: (prio_order.get(e["priority"], 3), e["discovered_step"]))
        top = items[0]
        line = (f"- [{top['priority']}] {top['name']} @ {top['file']}（{top['why']}，step {top['discovered_step']} 发现，"
                f"已挂起 {max(0, getattr(self, 'current_step', 0) - top['discovered_step'])} 步）")
        if len(items) > 1:
            line += f"\n  另有 {len(items) - 1} 个待跟进线索，见下方 frontier 清单"
        return line

    # ── Frontier 账本操作 ────────────────────────────────────────────

    def _frontier_priority(self, name: str, relation: str, source_rel: bool) -> str:
        """简单三档优先级：命中关键词或来自相关函数=HIGH；结构化线索=MEDIUM；搜索/召回=LOW。"""
        nl = name.lower()
        if any(kw.lower() in nl for kw in self.keyword_hints if len(kw) >= 4):
            return "HIGH"
        if relation in ("CALLER", "CALLEE") and source_rel:
            return "HIGH"
        if relation in ("CALLER", "CALLEE"):
            return "MEDIUM"
        return "LOW"

    def _frontier_add(self, name: str, file_path: str, relation: str, source: str, why: str, step: int) -> bool:
        """加入 frontier（去重；已读函数不进）。返回是否新增。"""
        if not name or not file_path:
            return False
        key = f"{name}@{file_path}"
        if key in self.read_functions or key in self.frontier:
            return False
        source_rel = any(
            k.split("@")[0] == source and v is True
            for k, v in self.visited_functions.items()
        )
        self.frontier[key] = {
            "name": name, "file": file_path, "relation": relation, "source": source,
            "why": why, "discovered_step": step,
            "priority": self._frontier_priority(name, relation, source_rel),
        }
        self.frontier_events.append({"step": step, "node": key, "source": source, "relation": relation, "status": "discovered"})
        return True

    def _frontier_mark_read(self, name: str, file_path: str, step: int) -> None:
        key = f"{name}@{file_path}"
        if key in self.frontier:
            self.frontier_events.append({"step": step, "node": key, "source": self.frontier[key]["source"],
                                         "relation": self.frontier[key]["relation"], "status": "read"})
            del self.frontier[key]

    @staticmethod
    def _digest_of(meta: dict) -> str:
        """已读函数的一句话摘要：签名首行（≈职责提示）。防止 Agent 忘记自己读过什么。"""
        code = (meta.get("code") or "").strip()
        if not code:
            return ""
        for ln in code.split("\n"):
            ln = ln.strip()
            if ln and not ln.startswith(("//", "/*", "*", "#")):
                return ln[:70]
        return ""

    def _ledger_text(self) -> tuple[str, str]:
        """返回 (已调查文本, 待调查 frontier 文本)。"""
        explored = []
        for key, meta in self.read_functions.items():
            name = meta.get("name", key.split("@")[0])
            rel = self.visited_functions.get(key)
            rel_s = "相关" if rel is True else "不相关" if rel is False else "未标"
            flags = []
            if name in self.callers_checked:
                flags.append("callers✓")
            if name in self.callees_checked:
                flags.append("callees✓")
            digest = self._digest_of(meta)
            explored.append(
                f"- {name}（{rel_s}{'，' + ' '.join(flags) if flags else ''}）"
                + (f" — {digest}" if digest else "")
            )
        explored_text = "\n".join(explored) or "(无)"

        prio_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
        items = sorted(self.frontier.values(), key=lambda e: (prio_order.get(e["priority"], 3), e["discovered_step"]))
        lines = []
        for e in items[:12]:
            lines.append(
                f"- [{e['priority']}] {e['name']} @ {e['file']}（{e['why']}，step {e['discovered_step']}）"
            )
        if len(items) > 12:
            lines.append(f"- ... 还有 {len(items) - 12} 个")
        frontier_text = "\n".join(lines) or "(空——没有待调查的线索)"
        return explored_text, frontier_text

    # ── 主循环 ───────────────────────────────────────────────────────

    @staticmethod
    def _parse_read_targets(action_input: dict) -> list[tuple[str, str]]:
        """解析 read_function 的各种参数写法为 [(name, file)]，用于重复检测/回放。"""
        names = action_input.get("function_name", "")
        if isinstance(names, str):
            names = [names] if names else []
        extra = action_input.get("function_names")
        if isinstance(extra, list):
            names = list(names) + [n for n in extra if isinstance(n, str)]
        fps = action_input.get("file_paths")
        if not isinstance(fps, list):
            fp0 = action_input.get("file_path", "")
            if isinstance(fp0, list):
                fps = fp0
            else:
                fps = [fp0] if fp0 else []
        out = []
        for i, n in enumerate(names[:8]):
            if not isinstance(n, str) or not n:
                continue
            fp = fps[i] if i < len(fps) and fps[i] else (fps[0] if fps else "")
            out.append((n, str(fp).split(":")[0]))
        return out

    def _replay_read(self, action: str, action_input: dict) -> tuple[str, list]:
        """重读已读内容：直接回放，不拒绝、不触发升级干预。

        背景：历史窗口只保留最近几步，旧 observation 滑出后 Agent 会以为"没读过"而重读；
        拒绝会让它在"重读-被拒-换参数再读"里空转（0809 复盘：010/013/037/044 的主死因）。
        正确做法是把内容还给它。
        """
        if action == "read_lines":
            obs, files = tool_read_lines(
                action_input.get("file_path", ""),
                int(action_input.get("start_line", 1) or 1),
                int(action_input.get("end_line", 50) or 50),
                self.repo_root,
            )
            return "【回放：这个区域你之前读过，内容如下】\n" + obs, files
        # read_function：从证据注册表重建（代码全文都在注册表里）
        parts, files = [], []
        for n, fp in self._parse_read_targets(action_input):
            matched = [
                (k, m) for k, m in self.read_functions.items()
                if k.split("@")[0] == n and (not fp or k.split("@", 1)[1] == fp)
            ]
            if not matched:
                continue
            k, m = matched[0]
            code = m.get("code", "")
            if len(code) > 8000:
                code = code[:8000] + "\n... (回放截断，需要更多请 read_lines)"
            parts.append(
                f"【回放：该函数你之前已读过完整实现】函数 {m.get('name', n)} "
                f"({m.get('file', fp)}:{m.get('start_line', '?')}-{m.get('end_line', '?')}):\n```cpp\n{code}\n```"
            )
            files.append(m.get("file", fp))
        return "\n\n".join(parts) or "【回放】未找到已读记录", files

    def _unread_leads(self) -> list[tuple[str, str]]:
        """相关函数的调用点线索中还没 read_function 读过的 [(name, file)]。

        排除 tests/ 目录：测试函数的调用方对"生产调用方契约"类问题没有证据价值，
        推给 Agent 只会浪费步数（007 案例：finish 守卫要求查测试函数的调用链）。
        """
        unread, seen = [], set()
        for fn, rel in self.visited_functions.items():
            if rel is not True:
                continue
            for caller_name, caller_file in self.caller_leads.get(fn.split("@")[0], []):
                if caller_file.startswith(("tests/", "test/")):
                    continue
                key = f"{caller_name}@{caller_file}"
                if key in seen or key in self.read_functions:
                    continue
                seen.add(key)
                unread.append((caller_name, caller_file))
        return unread

    # grep_callees 词法分析产生的泛名（标准库调用、常见动词），跨文件解析纯属噪声
    # （032 案例：~ggml_backend_registry 里的 entry.handle.release() 被解析到 server-context.cpp）
    _GENERIC_CALLEE_NAMES = frozenset({
        "release", "get", "find", "empty", "size", "max", "min", "end", "begin", "lock", "unlock",
        "getenv", "string", "assert", "and", "or", "not", "c_str", "what", "get_token", "substr",
        "push_back", "emplace", "emplace_back", "insert", "erase", "clear", "count", "contains",
        "fopen", "fclose", "fprintf", "printf", "malloc", "free", "new", "delete", "reset", "getenv",
        "stoi", "stol", "stoul", "to_string", "move", "forward", "make_unique", "make_shared",
    })

    def _resolve_symbol_file(self, name: str, fallback: str) -> str:
        """解析函数名的定义文件：已读注册表 → func_file_map → 名称索引（精确匹配，优先实现文件）。

        泛名（release/get/find/...）不解析——它们在词法 callee 列表里是噪声，跨文件归属必是误配。"""
        if name in self._GENERIC_CALLEE_NAMES or len(name) < 4:
            return fallback
        for k in self.read_functions:
            if k.split("@")[0] == name:
                return k.split("@", 1)[1]
        if name in self.func_file_map:
            return sorted(self.func_file_map[name])[0]
        if self.chunks_by_id:
            exact = []
            for ch in self.chunks_by_id.values():
                meta = ch.get("meta") or {}
                if meta.get("name") == name and meta.get("file_path"):
                    exact.append(meta["file_path"])
            if exact:
                # 优先实现文件（.c/.cpp/.cc），其次头文件；同优先级取最短路径（更可能是主文件）
                impl = [f for f in exact if Path(f).suffix in (".c", ".cc", ".cpp", ".cxx")]
                pool = impl or exact
                return sorted(pool, key=len)[0]
        return fallback

    def _symbol_exists(self, kw: str) -> bool:
        """监督建议符号的存在性校验：全量函数名 blob 子串匹配（毫秒级）。

        0809 复盘：监督者每题幻觉 3-4 个仓库里不存在的符号（switch_device/find_template/...），
        Agent 老实执行搜索全部空手而归（40 步空搜索/12 题）。建议注入前先验货。
        """
        kw = (kw or "").strip().lower()
        if len(kw) < 3:
            return False
        if self._all_names_blob is None:
            names = []
            if self.chunks_by_id:
                for ch in self.chunks_by_id.values():
                    n = ((ch.get("meta") or {}).get("name") or "").lower()
                    if n:
                        names.append(n)
            self._all_names_blob = "\n".join(names)
        return kw in self._all_names_blob

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
            # 非 dict（数组/字符串/None）：此前是静默丢弃，监督意见凭空消失（044 案例：5 次监督只记录 1 次）
            print(f"[SUPERVISOR_WARN] 监督输出非 dict，已丢弃: {str(note)[:120]}", file=sys.stderr, flush=True)
            return
        note["step"] = steps[-1]["step"] if steps else 0
        self.supervisor_notes.append(note)
        kws = note.get("suggest_keywords") or []
        # 幻觉过滤：建议符号先过名称索引，不存在的剔除并如实告知（防止 Agent 搜空气）
        if kws:
            valid_kws = [k for k in kws if self._symbol_exists(k)]
            phantom = [k for k in kws if k not in valid_kws]
            if phantom:
                note["phantom_keywords"] = phantom
            kws = valid_kws
        self.latest_guidance = (
            f"方向评估：{note.get('direction', '未知')}（{note.get('assessment', '')}）\n"
            f"监督指令：{note.get('guidance', '')}"
            + (f"\n建议搜索关键词：{', '.join(kws)}" if kws else "")
            + (f"\n（监督者建议的 {', '.join(note['phantom_keywords'])} 在仓库符号索引中不存在，已忽略，请勿搜索）"
               if note.get("phantom_keywords") else "")
        )

        # 强制转向（每次调查限一次）：方向判"错误" →
        # 1) 最近在读但无相关产出的文件标记"不相关结案"，防止回头
        # 2) 用 supervisor 给的关键词**重新召回**（名称查找 + embedding 重查）注入候选池
        # 3) 下一步系统强制执行监督建议的搜索/扩展
        # 判"可疑"的轻量干预：只注入重新召回，不强制执行、不结案（也限一次）
        if note.get("direction") == "可疑" and kws and not self.suspicious_recalled:
            self.suspicious_recalled = True
            new_candidates = []
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
                    for r in self.retriever.retrieve(_np.asarray([q2], dtype=_np.float32), top_k=15):
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
                    print(f"[SUPERVISOR_ERROR] 可疑方向重新召回失败: {e}", file=sys.stderr, flush=True)
            if new_candidates:
                new_candidates = new_candidates[:15]
                self.recall_pool = new_candidates + self.recall_pool
                self.recall_shown = min(self.recall_shown + len(new_candidates), len(self.recall_pool))
                self.latest_guidance += f"\n【可疑方向·重新召回】已用建议关键词补充 {len(new_candidates)} 个候选到池前列，请优先考察。"

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

    def investigate(self, question: str, model: str, qa_id: str = "") -> dict:
        self.current_qa_id = qa_id
        steps = []
        files_content = defaultdict(list)  # file_path -> [读到的内容块]

        for step_num in range(1, self.max_steps + 1):
            self.current_step = step_num
            # 监督按"已完成步数"触发（放在循环顶部）：回放/finish 守卫等早退 continue
            # 不再跳过监督（044 案例：30 步里 4 次监督被回放路径吞掉，纠偏没上班）
            completed = step_num - 1
            if (completed % 5 == 0 and completed > self._last_supervisor_step
                    and completed < self.max_steps
                    and steps and steps[-1]["action"] != "finish"):
                self._last_supervisor_step = completed
                self._call_supervisor(question, steps, model)
            # 候选列表动态标注已读状态，避免 Agent 反复回到同一函数
            if self.file_maps:
                # 文件中心模式：按文件分组展示 File Map
                candidate_lines = []
                for fp, fmap in self.file_maps.items():
                    read_marks = []
                    for f in fmap:
                        key = f"{f['name']}@{fp}"
                        rel = self.visited_functions.get(key)
                        m = ""
                        if rel is True:
                            m = "✓相关"
                        elif rel is False:
                            m = "✗不相关"
                        elif key in self.visited_functions:
                            m = "已读"
                        read_marks.append(f"{f['name']}({f['start_line']}-{f['end_line']}){':'+m if m else ''}")
                    candidate_lines.append(f"■ {fp}（{len(fmap)} 个函数）\n    " + ", ".join(read_marks[:40]))
            else:
                candidate_lines = []
                for f in self.recall_pool[:self.recall_shown]:
                    key = f"{f['name']}@{f['file_path']}"
                    rel = self.visited_functions.get(key)
                    mark = ""
                    if f.get("name_match"):
                        mark = " 【名称匹配】"
                    if f.get("triage") is not None:
                        mark += f" 【分诊:{f['triage']}分】"
                    if f.get("hyde"):
                        mark += " 【反查召回】"
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
                        + (f"\n    签名: {f['signature'][:100]}" if f.get("signature") else "")
                        + (f"\n    职责: {role}" if (role := (self.memory.lookup_role(f["name"], f["file_path"]) if self.memory else "")) else "")
                    )
            candidate_functions = "\n".join(candidate_lines) or "(无)"

            # 调查账本（已调查 + 待调查 frontier）与目录账本
            ledger_explored, ledger_frontier = self._ledger_text()
            dir_status = self._dir_ledger_text()

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
                ledger_explored=ledger_explored,
                ledger_frontier=ledger_frontier,
                dir_status=dir_status,
                top_nag=self._top_nag(),
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
                    "search_codebase": "query",
                    "find_callers": "function_name",
                    "read_function": "function_name",
                    "list_functions": "file_path",
                    "list_files": "directory",
                    "scan_directory": "directory",
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
            prev_rel_files = {fp for fp, st in self.file_status.items() if st == "relevant"}
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
                        self.last_new_relevant_step = step_num  # 提前 finish 机制用
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

            # 新标相关的文件入同目录提示队列（人类式跟随：这个文件相关 → 它的邻居可能也相关）
            self._pending_sibling_files.extend(
                fp for fp, st in self.file_status.items()
                if st == "relevant" and fp not in prev_rel_files
            )

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
                    and not fn.split("@", 1)[1].startswith(("tests/", "test/"))  # 测试函数无契约证据价值（007 案例）
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
            is_repeat = action not in ("expand_recall", "finish") and self.action_counts[canonical] >= 1

            # 读取类动作的"换皮重复"：参数写法不同（file_path/file_paths、顺序、数组），
            # 但目标函数全部已读过 → 也视为重复，走回放而不是重新执行/拒绝
            if not is_repeat and action == "read_function":
                targets = self._parse_read_targets(action_input)
                if targets and all(
                    any(k.split("@")[0] == n and (not fp or k.split("@", 1)[1] == fp)
                        for k in self.read_functions)
                    for n, fp in targets
                ):
                    is_repeat = True

            # read_lines 区间覆盖检测：请求区间 ≥80% 已被此前读取覆盖 → 按重复处理（回放）
            # （032/036 案例：同一文件换行号区间重读 7-17 次，精确去重抓不到换皮区间）
            if not is_repeat and action == "read_lines":
                fp_rl = str(action_input.get("file_path", "")).split(":")[0]
                try:
                    s_rl = int(action_input.get("start_line", 1) or 1)
                    e_rl = int(action_input.get("end_line", 50) or 50)
                except (TypeError, ValueError):
                    s_rl, e_rl = 1, 50
                span = e_rl - s_rl + 1
                prev_ranges = self.file_line_ranges.get(fp_rl, [])
                if prev_ranges and span > 0:
                    covered = 0
                    for ps, pe in prev_ranges:
                        covered += max(0, min(e_rl, pe) - max(s_rl, ps) + 1)
                    if min(covered, span) / span >= 0.8:
                        is_repeat = True

            # 读取类重复：回放内容，不拒绝（重读的根因是旧 observation 滑出上下文，
            # 拒绝只会制造"重读-被拒-换皮再读"死循环）。
            # 但同一内容回放 2 次后转入正常拒绝路径——044 案例：监督者反复下令"重读"，
            # Agent 服从了 13 次，回放内容满足不了"执行命令"这个动作循环，必须硬打断。
            if is_repeat and action in ("read_function", "read_lines"):
                replay_key = canonical
                if action == "read_function":
                    replay_key = ("read_function", json.dumps(
                        sorted(self._parse_read_targets(action_input)), ensure_ascii=False))
                observation, new_files = self._replay_read(action, action_input)
                if observation != "【回放】未找到已读记录" and self.replay_counts[replay_key] < 2:
                    self.replay_counts[replay_key] += 1
                    rejected = False
                    steps.append({
                        "step": step_num, "thought": thought, "action": action,
                        "action_input": action_input, "reason": reason,
                        "observation": observation, "files_accessed": new_files, "rejected": False,
                    })
                    continue
                if observation != "【回放】未找到已读记录":
                    observation = (
                        f"【禁止重读】这已经是你第 {self.replay_counts[replay_key] + 1} 次读取完全相同的内容，"
                        "内容不会有任何变化。你手里已经有这些代码了——请直接基于已读内容给出分析结论"
                        "（在 function_relevance 中标记、或 finish 写答案），或者转向一个你没看过的全新方向。"
                    )
                    rejected = True
                    self.rejection_count += 1  # 计入升级干预，让它推送其他未读线索
                    steps.append({
                        "step": step_num, "thought": thought, "action": action,
                        "action_input": action_input, "reason": reason,
                        "observation": observation, "files_accessed": [], "rejected": True,
                    })
                    continue
                is_repeat = False  # 上次读取失败（无记录）→ 正常执行，把真实错误再给它看

            if is_repeat:
                # find_callers/find_callees 首次重复：返回缓存结果+当前状态，不拒绝（消灭无效重复）
                if action in ("find_callers", "find_callees") and self.action_counts[canonical] == 1:
                    prev = self.past_observations.get(canonical, "")
                    observation = (
                        f"【已查询过，返回缓存状态】{action} 之前的结果：\n{prev}\n"
                        f"当前待调查 frontier：{len(self.frontier)} 个；已读函数 {len(self.read_functions)} 个。\n"
                        "请直接从 frontier 中挑一个未读的 read_function，不要重复查询。"
                    )
                    new_files = []
                    rejected = False
                    steps.append({
                        "step": step_num, "thought": thought, "action": action,
                        "action_input": action_input, "reason": reason,
                        "observation": observation, "files_accessed": [], "rejected": False,
                    })
                    self.action_counts[canonical] += 1
                    continue
                prev = self.past_observations.get(canonical, "")
                prev_summary = prev[:300] + ("..." if len(prev) > 300 else "") if prev else "(无记录)"
                observation = (
                    f"【重复动作被拒绝】你已经执行过 {action} 同样的参数，结果不会变化。\n"
                    f"上次执行的结果：\n{prev_summary}\n"
                    f"当前已读函数：{len(self.visited_functions)} 个，文件状态：{len(self.file_status)} 个已标记。\n"
                    "请换一个未探索的方向：\n"
                    "- 对待调查 frontier 里的线索用 read_function 跟进\n"
                    "- 对相关函数用 find_callers / find_callees 扩展调用链\n"
                    "- 用 scan_directory 粗筛你怀疑的目录（只看函数名，成本低）\n"
                    "- 用 search_codebase 描述方向找新区域，或 expand_recall 看更多召回候选"
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
                    if action == "read_lines":
                        fp_rl = str(action_input.get("file_path", "")).split(":")[0]
                        try:
                            s_rl = int(action_input.get("start_line", 1) or 1)
                            e_rl = int(action_input.get("end_line", 50) or 50)
                            self.file_line_ranges[fp_rl].append((s_rl, e_rl))
                        except (TypeError, ValueError):
                            pass
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

            # 提前 finish 提示（只提示不强制）：已有相关函数且连续 4 步无新增相关 →
            # 证据可能已足够。手里没有任何相关函数的题永远不会收到此提示（防漏证据）
            if (not self.early_finish_hinted
                    and self.last_new_relevant_step > 0
                    and step_num - self.last_new_relevant_step >= 4):
                self.early_finish_hinted = True
                observation += (
                    f"\n\n【系统提示】自第 {self.last_new_relevant_step} 步以来没有新增相关函数，"
                    "你手里的证据可能已经足够回答问题。请做个盘点："
                    "如果相关函数及其调用链已经查清，请直接 finish，不要把步数花在重复确认上；"
                    "如果还有明确的未探索方向（具体的函数/文件/调用方），请继续。"
                )

            # 同目录线索：新发现相关文件 → 提示同目录还没看过什么（013/042 案例：进了目录没进对文件）
            if self._pending_sibling_files:
                hint = self._sibling_hint(self._pending_sibling_files)
                self._pending_sibling_files = []
                if hint:
                    observation += "\n\n【同目录线索】相关文件的邻居常常也相关：\n" + hint

            # HIGH 线索挂起自动跟进：HIGH frontier 挂起 ≥5 步没人读，系统直接读（限 3 次）。
            # （V57-044 案例：llama_init_from_model 被搜索结果和 frontier 展示了两次，
            #  Agent 到结束都没跟——展示的线索不能指望模型自觉，挂起太久就确定性补读）
            if self.auto_follow_count < 3:
                high_pending = [e for e in self.frontier.values()
                                if e["priority"] == "HIGH" and step_num - e["discovered_step"] >= 5
                                # 泛名/宏/日志函数不值得自动跟进（LOGe/max 这种只会浪费弹药）
                                and e["name"] not in self._GENERIC_CALLEE_NAMES
                                and len(e["name"]) >= 4
                                and not e["name"].isupper()]
                if high_pending:
                    top = sorted(high_pending, key=lambda e: e["discovered_step"])[0]
                    meta: dict = {}
                    robs, rnf = tool_read_function(top["name"], top["file"], self.repo_root, out_meta=meta)
                    if rnf:
                        self.auto_follow_count += 1
                        self.func_file_map.setdefault(top["name"], set()).add(rnf[0])
                        self.read_functions[f"{top['name']}@{rnf[0]}"] = meta
                        self._frontier_mark_read(top["name"], rnf[0], step_num)
                        new_files.append(rnf[0])
                        observation += (
                            f"\n\n【系统自动跟进】HIGH 优先级线索 {top['name']} @ {rnf[0]} "
                            f"自 step {top['discovered_step']} 起已挂起 {step_num - top['discovered_step']} 步未跟进，"
                            f"系统已自动读取（如与问题无关请在 function_relevance 标记不相关）：\n{robs}"
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

            # （监督已移到循环顶部按已完成步数触发，此处不再重复调用）

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

        # v2 记忆在真实读取时已写入 read_cache（note_read），无需记录问题级判定
        if self.memory is not None:
            self.memory.save()

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
            "frontier_events": self.frontier_events,
            "token_usage": token_usage,
        }


# ── Main ────────────────────────────────────────────────────────────

def extract_keywords_llm(question: str, model: str) -> list[str]:
    """用 LLM 从中文问题推测 C++ 代码中可能出现的英文标识符（HyDE 轻量版）。"""
    prompt = load_prompt("keyword_extract").format(question=question)
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
    verification = (
        ("\n".join(hit_lines) or "（全部未命中）")
        + f"\n\n未命中: {', '.join(missed) or '（无）'}"
    )
    prompt = load_prompt("keyword_refine").format(verification=verification, question=question)
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


def build_oracle_entries(item: dict, chunks_by_id: dict, level: str) -> list[dict]:
    """Oracle 粒度实验：从 benchmark gold 证据构造不同粒度的锚点候选。

    level: function（gold 函数）/ file（gold 文件全部函数）/ directory（gold 目录全部函数）
    """
    gold_files = {e["file"] for e in item.get("gold_evidence", [])}
    gold_funcs = {(e["file"], e.get("symbol", "")) for e in item.get("gold_evidence", []) if e.get("symbol")}
    gold_dirs = {"/".join(f.split("/")[:2]) for f in gold_files}

    entries = []
    for fid, ch in chunks_by_id.items():
        meta = ch.get("meta") or {}
        fp = meta.get("file_path", "")
        name = meta.get("name", "")
        if level == "function" and (fp, name) not in gold_funcs:
            continue
        if level == "file" and fp not in gold_files:
            continue
        if level == "directory" and "/".join(fp.split("/")[:2]) not in gold_dirs:
            continue
        entries.append({
            "fid": fid, "name": name, "file_path": fp,
            "start_line": meta.get("start_line", 0), "end_line": meta.get("end_line", 0),
            "signature": meta.get("signature", ""), "score": 1.0, "name_match": True,
        })
    return entries


def compute_file_ranking(q_emb: np.ndarray, retriever, chunks_by_id: dict, top_files: int = 8) -> list[dict]:
    """文件级检索：RRF 融合 File Profile 排名 + 函数聚合排名。

    File Profile 走 data/file_embedding_index.json（若存在）；
    函数聚合 = 函数 embedding top-100 中每个文件的最佳排名。
    """
    agg_rank: dict[str, int] = {}
    sims = retriever.doc_matrix @ q_emb
    idx_to_chunk = retriever.chunks
    order = np.argsort(-sims)[:100]
    for r, idx in enumerate(order):
        fp = idx_to_chunk[idx]["meta"]["file_path"]
        if fp not in agg_rank:
            agg_rank[fp] = r

    prof_rank: dict[str, int] = {}
    fi_path = _ROOT / "data" / "file_embedding_index.json"
    if fi_path.exists():
        fi = json.load(open(fi_path))
        fm = np.asarray(fi["embeddings"], dtype=np.float32)
        fsims = fm @ q_emb
        for r, idx in enumerate(np.argsort(-fsims)):
            prof_rank[fi["profiles"][idx]["file_path"]] = r

    rrf: dict[str, float] = {}
    for fp, r in agg_rank.items():
        rrf[fp] = rrf.get(fp, 0.0) + 1.0 / (60 + r + 1)
    for fp, r in prof_rank.items():
        rrf[fp] = rrf.get(fp, 0.0) + 1.0 / (60 + r + 1)
    ranked = sorted(rrf.items(), key=lambda kv: -kv[1])
    return [{"file_path": fp, "score": sc} for fp, sc in ranked[:top_files]]


def build_file_map(file_path: str, chunks_by_id: dict) -> list[dict]:
    """File Map：文件内全部函数（名称+行号+签名），按行号排序。"""
    funcs = []
    for fid, ch in chunks_by_id.items():
        meta = ch.get("meta") or {}
        if meta.get("file_path") == file_path:
            funcs.append({
                "fid": fid, "name": meta.get("name", ""), "file_path": file_path,
                "start_line": meta.get("start_line", 0), "end_line": meta.get("end_line", 0),
                "signature": meta.get("signature", ""), "score": 0.5,
            })
    funcs.sort(key=lambda f: f["start_line"])
    return funcs


def process_one(item, q_emb, ca, retriever, chunks_by_id, repo_root, model, max_steps, memory=None, oracle_level=None, file_centric=False, file_recon=False, file_map_cache=None, pool_triage=False):
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

    # Oracle 粒度实验：注入 gold 锚点（仅实验用）
    if oracle_level:
        oracle_entries = build_oracle_entries(item, chunks_by_id, oracle_level)
        existing = {f["fid"] for f in recall_pool}
        oracle_entries = [e for e in oracle_entries if e["fid"] not in existing]
        recall_pool = oracle_entries + recall_pool
        print(f"[ORACLE-{oracle_level}] 注入 {len(oracle_entries)} 个锚点", flush=True)

    # 2.5 文件中心模式：Stage 1 输出 top-K 文件 + File Map，池改为按文件组织
    file_maps = {}
    if file_centric:
        top_files = compute_file_ranking(q_emb, retriever, chunks_by_id, top_files=8)
        old_scores = {f["fid"]: f["score"] for f in recall_pool}
        new_pool = []
        for tf in top_files:
            fp = tf["file_path"]
            fmap = build_file_map(fp, chunks_by_id)
            if not fmap:
                continue
            file_maps[fp] = fmap
            for f in fmap:
                f["score"] = old_scores.get(f["fid"], tf["score"])
                new_pool.append(f)
        # 保留原池中不在 top 文件里的高分函数（尾部，expand_recall 兜底用）
        in_files = set(file_maps)
        new_pool.extend(f for f in recall_pool if f["file_path"] not in in_files)
        recall_pool = new_pool

    # 2.6 批量内容分诊（--pool-triage）：子 agent 读池内前 96 个候选的完整代码（并发分批），
    #     逐个打 0-10 相关性分，按分数重排；≤3 分进 skipped_unread（finish 守卫 A 兜误杀）。
    #     动机：013 案例——get_cache_acl_tensor 名字不像扩容，名字级分诊必误杀，内容级能抓住
    triage_result = {}
    if pool_triage and recall_pool:
        triage_result = triage_functions_batched(question, recall_pool[:96], repo_root, model)
        # HyDE 反查：全场低分（最高 <6）说明"问题词汇 ≠ 代码词汇"，
        # 让 LLM 描述"正确函数应该长什么样"，用描述做 embedding 重新召回（013/044 的死因）
        max_score = max((s["score"] for s in triage_result.values()), default=0.0)
        if triage_result and max_score < 6.0:
            desc = redescribe_missing_function(question, model)
            if desc:
                hyde_hits = []
                existing = {f["fid"] for f in recall_pool}
                try:
                    qd = retriever.encode_queries([desc])[0]
                    for r in retriever.retrieve(np.asarray([qd], dtype=np.float32), top_k=15):
                        md = r["metadata"]
                        fid = f"{md['file_path']}:{md['name']}:{md['start_line']}"
                        if fid in existing:
                            continue
                        hyde_hits.append({
                            "fid": fid, "name": md.get("name", ""), "file_path": md.get("file_path", ""),
                            "start_line": md.get("start_line", 0), "end_line": md.get("end_line", 0),
                            "signature": md.get("signature", ""), "score": r.get("score", 0.0), "hyde": True,
                        })
                except Exception as e:
                    print(f"[HYDE_ERROR] embedding 反查失败: {e}", file=sys.stderr, flush=True)
                # 名称索引补充：假设描述里的标识符直接查函数名
                terms = [t for t in re.findall(r"[a-z][a-z0-9_]{3,}", desc.lower())][:8]
                for h in lookup_functions_by_keywords(terms, chunks_by_id, limit=6):
                    if h["fid"] not in existing and all(h["fid"] != x["fid"] for x in hyde_hits):
                        h["hyde"] = True
                        hyde_hits.append(h)
                hyde_hits = hyde_hits[:20]
                if hyde_hits:
                    # 反查候选也过分诊（单批），分数合并后统一排序
                    triage_result.update(triage_functions_llm(question, hyde_hits, repo_root, model))
                    recall_pool = hyde_hits + recall_pool
                    print(f"[HYDE] {qa_id}: 最高分 {max_score} 触发反查，注入 {len(hyde_hits)} 个候选", flush=True)
        if triage_result:
            for f in recall_pool:
                s = triage_result.get(f"{f['name']}@{f['file_path']}")
                if s:
                    f["triage"] = s["score"]
                    f["triage_reason"] = s["reason"]
            # 稳定降序：未评分的按 5.0 中立位
            recall_pool.sort(key=lambda f: -(float(f["triage"]) if f.get("triage") is not None else 5.0))
            scores_str = ",".join(str(f.get("triage", "-")) for f in recall_pool[:12])
            print(f"[POOL_TRIAGE] {qa_id}: top12 分数 [{scores_str}]", flush=True)

    # 2.7 同文件限流：整个召回池排序后同一文件最多保留 3 个在前列，溢出沉到整个池尾。
    #     031 案例：miniaudio 的 ma_device_init__* 同族变体占满首屏，gold 被挤出开局阅读范围。
    #     注意必须对**整个池子**做（V61 教训：只对前 20 做，队尾还是同族变体，回填后首屏仍是 12 个）
    PER_FILE_CAP = 3
    if len(recall_pool) > 10:
        fc: dict = {}
        keep, overflow = [], []
        for f in recall_pool:
            fp = f["file_path"]
            fc[fp] = fc.get(fp, 0) + 1
            (keep if fc[fp] <= PER_FILE_CAP else overflow).append(f)
        recall_pool = keep + overflow

    # 3. ReAct 调查
    # 初始池快照：investigate() 期间 recall_pool 会被中途注入（监督重召回/search_codebase）修改，
    # 结果文件里记录的必须是这一步的池子，否则"gold 在池内 rank 几"的分析会被污染
    initial_pool_snapshot = [dict(f) for f in recall_pool[:20]]
    agent = ReactAgent(repo_root, max_steps=max_steps, recall_pool=recall_pool,
                       keyword_hints=extra_symbols, retriever=retriever, chunks_by_id=chunks_by_id,
                       dir_probes=dir_probes, keyword_families=keyword_families, memory=memory,
                       file_maps=file_maps, file_recon=file_recon, file_map_cache=file_map_cache)
    result = agent.investigate(question, model, qa_id=qa_id)

    return {
        "qa_id": qa_id,
        "question": question,
        "answer": result["answer"],
        "initial_functions": initial_pool_snapshot,
        "visited_files": result["visited_files"],
        "visited_functions": result["visited_functions"],
        "file_status": result["file_status"],
        "recall_expansions": result["recall_expansions"],
        "answer_repair": result["answer_repair"],
        "direction_warnings": result["direction_warnings"],
        "supervisor_notes": result["supervisor_notes"],
        "backfilled": result["backfilled"],
        "skipped_unread": result.get("skipped_unread", {}),
        "pool_triage": {f"{f['name']}@{f['file_path']}": f"{f.get('triage')}:{f.get('triage_reason','')}"
                        for f in recall_pool if f.get("triage") is not None},
        "frontier_events": result.get("frontier_events", []),
        "token_usage": result["token_usage"],
        "steps": result["steps"],
        "model": model,
    }


def _sc_score(result: dict) -> float:
    """self-consistency 选题分（无 gold 信息，自洽代理指标）。

    越高越好：证据落地量（读过且引用的文件数）+ 调查深度（相关函数数）
    - 投降惩罚（零引用还答"无法确认"）。
    """
    answer = result.get("answer", "")
    read_files = set()
    for s in result.get("steps", []):
        if s.get("action") in ("read_function", "read_lines"):
            read_files.update(s.get("files_accessed", []))
        elif s.get("action") in ("find_callers", "search_symbol") and "[系统自动" in s.get("observation", ""):
            read_files.update(s.get("files_accessed", []))
    cited = sum(1 for f in read_files if f in answer)
    n_relevant = sum(1 for v in result.get("visited_functions", {}).values() if v is True)
    surrender = ("无法确认" in answer) and cited == 0
    return cited + 0.5 * n_relevant - (3.0 if surrender else 0.0)


def process_one_sc(item, q_emb, ca, retriever, chunks_by_id, repo_root, model, max_steps, memory, n_runs):
    """self-consistency：同一题独立调查 n 次，按 _sc_score 选最优。"""
    results = [process_one(item, q_emb, ca, retriever, chunks_by_id, repo_root, model, max_steps, memory)
               for _ in range(n_runs)]
    scored = [(_sc_score(r), i, r) for i, r in enumerate(results)]
    scored.sort(key=lambda x: -x[0])
    best = scored[0][2]
    best["sc"] = {
        "n_runs": n_runs,
        "chosen_run": scored[0][1],
        "scores": [round(s, 2) for s, _, _ in scored],
    }
    return best


def main():
    parser = argparse.ArgumentParser(description="Concept-Symbol + ReAct QA")
    parser.add_argument("--model", default=None, help="LLM model")
    parser.add_argument("--max-steps", type=int, default=25)
    parser.add_argument("--workers", type=int, default=30)
    parser.add_argument("--output", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--memory", action="store_true",
                        help="启用跨题调查记忆（data/investigation_memory.json）")
    parser.add_argument("--sc", type=int, default=1,
                        help="self-consistency：每题独立调查 N 次取最优（成本×N）")
    parser.add_argument("--qa-ids", default=None,
                        help="只跑指定题号（逗号分隔，如 posthoc_public_001,posthoc_public_013），用于失败子集冒烟")
    parser.add_argument("--oracle-level", choices=["function", "file", "directory"], default=None,
                        help="Oracle 粒度实验：向召回池注入 gold 锚点（仅实验用）")
    parser.add_argument("--file-centric", action="store_true",
                        help="文件中心调查：Stage 1 输出 top-K 文件 + File Map，进入文件自动侦察")
    parser.add_argument("--file-recon", action="store_true",
                        help="文件内导航：首次进入任何文件时自动附该文件完整函数清单（File Map）")
    parser.add_argument("--pool-triage", action="store_true",
                        help="批量内容分诊：子 agent 读召回池前 24 个候选的真实代码判相关性，高排前、低跳过")
    args = parser.parse_args()

    model = args.model or LLM_MODEL or "deepseek-v4-flash"

    benchmark_path = _ROOT / "datasets" / "benchmark_hard.json"
    index_path = Path(os.environ.get("QA_INDEX_PATH", "")) if os.environ.get("QA_INDEX_PATH") else _ROOT / "data" / "qa_embedding_index.json"
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

    # 跨题函数知识记忆（--memory 启用；基底=全量函数职责，问题无关）
    memory = None
    if args.memory:
        from src.qa.memory import FunctionMemory
        memory = FunctionMemory(
            _ROOT / "data" / "function_memory.json",
            summaries_path=_ROOT / "data" / "function_summaries.json",
        )
        print(f"Memory: 基底 {len(memory.base_roles)} 条职责，缓存 {len(memory.read_cache)} 条已读")

    results = []
    file_map_cache = {}  # 跨题共享 File Map 缓存（--file-recon 按需构建）
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        if args.sc > 1:
            futures = {
                executor.submit(process_one_sc, item, q_emb, ca, retriever, chunks_by_id, repo_root, model, args.max_steps, memory, args.sc): idx
                for idx, (item, q_emb) in enumerate(zip(items, q_embs))
            }
        else:
            futures = {
                executor.submit(process_one, item, q_emb, ca, retriever, chunks_by_id, repo_root, model, args.max_steps, memory, args.oracle_level, args.file_centric, args.file_recon, file_map_cache, args.pool_triage): idx
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
    if memory is not None:
        memory.save()
        print(f"Memory saved: {len(memory.data)} 条")


if __name__ == "__main__":
    main()
