"""
Pure ReAct Investigation Agent — 文件级导航式代码审计。

支持三种模式：
1. Pure ReAct: Question → search_symbol → read → find_callers → read → ... → finish
2. Top20 + ReAct: Symbol Fast Path → Top20 → Agent 逐步调查 → finish
3. Baseline: Symbol Fast Path → Top20 → 一次性塞入 LLM → Answer（已有 pipeline）

Author: zzy
"""
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional
from dataclasses import dataclass, field

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))

from config import OPENAI_API_KEY, OPENAI_BASE_URL
from openai import OpenAI

from src.search.grep_search_v2 import grep_files, grep_codebase_v2
from src.search.code_reader import read_full_file, read_file_lines
from src.search.call_chain import get_callers, get_callees
from src.search.semantic_search import search_functions_by_text

client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)
JUDGE_MODEL = "gpt-4.1-mini"


# ── Data Structures ─────────────────────────────────────────────────

@dataclass
class InvestigationStep:
    step: int
    thought: str
    action: str
    action_input: Dict
    observation: str
    files_accessed: List[str] = field(default_factory=list)


@dataclass
class InvestigationResult:
    qa_id: str
    question: str
    answer: str
    steps: List[InvestigationStep]
    visited_files: List[str]
    candidate_files: List[str]
    final_coverage: float = 0.0
    mode: str = "pure_react"


# ── Prompts ─────────────────────────────────────────────────────────

PURE_REACT_PROMPT = """你是一位代码审计专家。你的任务是调查代码仓库，找到与问题相关的证据，然后回答问题。

【当前问题】
{question}

【你已访问的文件】
{visited_files}

【当前候选文件队列】（按优先级排序）
{candidate_files}

---

你可以使用以下工具：

1. **search_symbol(symbol_name)** — 搜索仓库中包含该符号（函数/变量/类）的所有文件
2. **search_text(keyword)** — 用关键词搜索仓库（类似 grep）
3. **read_file(file_path)** — 读取完整文件内容
4. **read_lines(file_path, start_line, end_line)** — 读取文件的指定行号范围
5. **find_callers(function_name)** — 找到调用该函数的所有位置
6. **find_callees(function_name)** — 找到该函数调用的所有函数
7. **finish(reason)** — 认为已有足够证据，结束调查并生成答案

---

【决策规则】
- 如果问题中包含明确的函数名（如 `func_name`），先用 search_symbol 定位
- 读完一个文件后，根据内容决定下一步：深入读更多行、读相关文件（callers/callees）、或停止
- 每次只选择一个工具，给出明确的理由
- 最多 {max_steps} 步，当前第 {current_step} 步
- 如果连续 2 步未获得新信息，考虑停止

【输出格式】（必须是有效的 JSON）
{{
  "thought": "你的思考过程...",
  "action": "search_symbol|search_text|read_file|read_lines|find_callers|find_callees|finish",
  "action_input": {{"参数名": "参数值"}},
  "reason": "为什么选择这个行动"
}}
"""

TOP20_REACT_PROMPT = """你是一位代码审计专家。检索系统已经为你找到了一批相关文件，你需要从中选择最重要的文件逐步阅读，构建证据链。

【当前问题】
{question}

【检索系统提供的候选文件】（按相关性排序）
{candidate_files}

【你已访问的文件】
{visited_files}

---

你可以使用以下工具：

1. **read_file(file_path)** — 读取完整文件内容
2. **read_lines(file_path, start_line, end_line)** — 读取文件的指定行号范围
3. **find_callers(function_name)** — 找到调用该函数的所有位置
4. **find_callees(function_name)** — 找到该函数调用的所有函数
5. **search_symbol(symbol_name)** — 搜索仓库中包含该符号的所有文件（仅限必要时扩展搜索）
6. **finish(reason)** — 认为已有足够证据，结束调查并生成答案

---

【决策规则】
- 从候选文件中选择最关键的 1-3 个开始读
- 读完一个文件后，决定：继续深入读更多行 / 读相关文件（callers/callees）/ 换下一个候选文件 / 停止
- 每次只选择一个工具，给出明确的理由
- 最多 {max_steps} 步，当前第 {current_step} 步
- 不需要读完所有候选文件，只要证据充分就可以停止

【输出格式】（必须是有效的 JSON）
{{
  "thought": "你的思考过程...",
  "action": "read_file|read_lines|find_callers|find_callees|search_symbol|finish",
  "action_input": {{"参数名": "参数值"}},
  "reason": "为什么选择这个行动"
}}
"""

ANSWER_GENERATION_PROMPT = """基于你的调查过程和收集到的证据，回答问题。

【原始问题】
{question}

【调查过程】
{investigation_log}

【已访问的文件及关键发现】
{files_content}

---

请生成最终答案：
1. 直接回答原始问题
2. 引用你访问过的文件作为证据（格式：`file.cpp:start-end`）
3. 列出参考文件清单
4. 如果不确定，明确说明"无法确认"
"""


# ── Tool Execution ──────────────────────────────────────────────────

class ToolExecutor:
    """执行 Agent 选择的工具。"""

    def __init__(self, repo_path: str = None):
        self.repo_path = repo_path
        self.visited_files = set()
        self.file_cache = {}  # file_path -> content

    def execute(self, action: str, action_input: Dict) -> tuple[str, List[str]]:
        """执行工具，返回 (observation, new_files)。"""
        observation = ""
        new_files = []

        if action == "search_symbol":
            symbol = action_input.get("symbol_name", "")
            files = grep_files(symbol, self.repo_path, limit=10)
            observation = f"找到 {len(files)} 个文件包含 '{symbol}':\n" + "\n".join(f"- {f}" for f in files)
            new_files = files

        elif action == "search_text":
            keyword = action_input.get("keyword", "")
            results = grep_codebase_v2(keyword, self.repo_path, top_n=10, output_json=True)
            files = [r.get("file", "") for r in results if r.get("file")]
            observation = f"找到 {len(files)} 个文件包含 '{keyword}':\n" + "\n".join(f"- {f}" for f in files[:10])
            new_files = files

        elif action == "read_file":
            file_path = action_input.get("file_path", "")
            content = self._get_file_content(file_path)
            self.visited_files.add(file_path)
            observation = f"文件 {file_path} 内容:\n```cpp\n{content[:3000]}\n```"
            if len(content) > 3000:
                observation += f"\n... (截断，共 {len(content)} 字符)"
            new_files = [file_path]

        elif action == "read_lines":
            file_path = action_input.get("file_path", "")
            start = action_input.get("start_line", 1)
            end = action_input.get("end_line", start + 50)
            content = read_file_lines(file_path, start, end)
            self.visited_files.add(file_path)
            observation = f"文件 {file_path} 第 {start}-{end} 行:\n```cpp\n{content}\n```"
            new_files = [file_path]

        elif action == "find_callers":
            func_name = action_input.get("function_name", "")
            callers = get_callers(func_name, limit=10)
            files = list(set(c.get("file", "") for c in callers if c.get("file")))
            observation = f"找到 {len(callers)} 个调用者:\n" + "\n".join(
                f"- {c.get('caller_function', '?')} @ {c.get('file', '?')}:{c.get('line', '?')}"
                for c in callers[:10]
            )
            new_files = files

        elif action == "find_callees":
            func_name = action_input.get("function_name", "")
            callees = get_callees(func_name, limit=10)
            files = list(set(c.get("file", "") for c in callees if c.get("file")))
            observation = f"找到 {len(callees)} 个被调用者:\n" + "\n".join(
                f"- {c.get('callee_function', '?')} @ {c.get('file', '?')}"
                for c in callees[:10]
            )
            new_files = files

        elif action == "finish":
            reason = action_input.get("reason", "证据充分")
            observation = f"结束调查: {reason}"

        else:
            observation = f"未知工具: {action}"

        return observation, new_files

    def _get_file_content(self, file_path: str) -> str:
        if file_path not in self.file_cache:
            self.file_cache[file_path] = read_full_file(file_path)
        return self.file_cache[file_path]


# ── Agent ───────────────────────────────────────────────────────────

class ReactInvestigationAgent:
    """ReAct 调查 Agent。"""

    def __init__(self, mode: str = "pure_react", max_steps: int = 10):
        self.mode = mode
        self.max_steps = max_steps
        self.executor = ToolExecutor()

    def investigate(self, question: str, qa_id: str = "", initial_files: List[str] = None) -> InvestigationResult:
        """
        执行调查。

        Args:
            question: 原始问题
            qa_id: 题目 ID
            initial_files: 初始候选文件列表（Top20 模式用）
        """
        steps = []
        candidate_files = list(initial_files) if initial_files else []
        visited_files_content = {}  # file_path -> content snapshot

        for step_num in range(1, self.max_steps + 1):
            # Build prompt
            if self.mode == "pure_react":
                prompt = PURE_REACT_PROMPT.format(
                    question=question,
                    visited_files="\n".join(f"- {f}" for f in sorted(self.executor.visited_files)) or "(无)",
                    candidate_files="\n".join(f"- {f}" for f in candidate_files[:15]) or "(无)",
                    max_steps=self.max_steps,
                    current_step=step_num,
                )
            else:  # top20_react
                prompt = TOP20_REACT_PROMPT.format(
                    question=question,
                    candidate_files="\n".join(f"- {f}" for f in (initial_files or [])[:20]) or "(无)",
                    visited_files="\n".join(f"- {f}" for f in sorted(self.executor.visited_files)) or "(无)",
                    max_steps=self.max_steps,
                    current_step=step_num,
                )

            # LLM Decision
            try:
                resp = client.chat.completions.create(
                    model=JUDGE_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.2,
                    max_tokens=600,
                    response_format={"type": "json_object"},
                )
                decision = json.loads(resp.choices[0].message.content.strip())
            except Exception as e:
                decision = {
                    "thought": f"决策出错: {e}",
                    "action": "finish",
                    "action_input": {"reason": f"决策失败: {e}"},
                    "reason": "出错停止",
                }

            action = decision.get("action", "finish")
            action_input = decision.get("action_input", {})
            thought = decision.get("thought", "")
            reason = decision.get("reason", "")

            # Execute
            observation, new_files = self.executor.execute(action, action_input)

            # Record file content if read
            if action in ("read_file", "read_lines"):
                file_path = action_input.get("file_path", "")
                if file_path:
                    content = self.executor._get_file_content(file_path)
                    visited_files_content[file_path] = content[:5000]

            # Update candidates
            for f in new_files:
                if f not in candidate_files and f not in self.executor.visited_files:
                    candidate_files.append(f)

            # Record step
            step = InvestigationStep(
                step=step_num,
                thought=thought,
                action=action,
                action_input=action_input,
                observation=observation,
                files_accessed=new_files,
            )
            steps.append(step)

            # Check stop
            if action == "finish":
                break

        # Generate answer
        answer = self._generate_answer(question, steps, visited_files_content)

        return InvestigationResult(
            qa_id=qa_id,
            question=question,
            answer=answer,
            steps=steps,
            visited_files=list(self.executor.visited_files),
            candidate_files=candidate_files,
            mode=self.mode,
        )

    def _generate_answer(self, question: str, steps: List[InvestigationStep], files_content: Dict[str, str]) -> str:
        """基于调查过程生成答案。"""
        investigation_log = "\n\n".join(
            f"Step {s.step}: {s.action}({json.dumps(s.action_input, ensure_ascii=False)})\n"
            f"Thought: {s.thought}\n"
            f"Observation: {s.observation[:500]}"
            for s in steps
        )

        files_summary = "\n\n".join(
            f"=== {fp} ===\n{content[:2000]}"
            for fp, content in files_content.items()
        )

        prompt = ANSWER_GENERATION_PROMPT.format(
            question=question,
            investigation_log=investigation_log,
            files_content=files_summary,
        )

        try:
            resp = client.chat.completions.create(
                model=JUDGE_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=4000,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            return f"答案生成失败: {e}"


# ── Quick Test ──────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--question", type=str, required=True)
    parser.add_argument("--mode", choices=["pure_react", "top20_react"], default="pure_react")
    parser.add_argument("--initial-files", type=str, help="逗号分隔的初始文件列表（top20_react 用）")
    parser.add_argument("--max-steps", type=int, default=10)
    args = parser.parse_args()

    initial_files = args.initial_files.split(",") if args.initial_files else None

    agent = ReactInvestigationAgent(mode=args.mode, max_steps=args.max_steps)
    result = agent.investigate(args.question, initial_files=initial_files)

    print(f"\n{'='*60}")
    print(f"Mode: {result.mode}")
    print(f"Steps: {len(result.steps)}")
    print(f"Visited files: {result.visited_files}")
    print(f"{'='*60}")
    print("\nAnswer:")
    print(result.answer)
    print(f"\n{'='*60}")
    print("Investigation Log:")
    for s in result.steps:
        print(f"\nStep {s.step}: {s.action}")
        print(f"  Thought: {s.thought[:200]}...")
        print(f"  Observation: {s.observation[:200]}...")
