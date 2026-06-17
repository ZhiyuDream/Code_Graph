"""
Repository Investigation 基础模块。

核心概念：
- SuspicionState: Agent 当前的怀疑状态（子问题 + 怀疑符号 + 怀疑文件）
- Evidence → Suspicion → Search Shift

Static vs Dynamic 的区别：
- Static: SuspicionState 初始化后固定不变，每步基于固定 suspicion 排序 frontier
- Dynamic: 每步根据新证据更新 SuspicionState，从而改变搜索方向
"""
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_ROOT))

from config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, get_repo_root
from openai import OpenAI

from src.search.code_reader import read_full_file
from src.search.grep_search_v2 import grep_files


def load_prompt(name: str) -> str:
    """加载 prompts/ 目录下的 prompt 文件。"""
    path = _ROOT / "prompts" / f"{name}.txt"
    return path.read_text(encoding="utf-8")


class LLMClient:
    """统一的 DeepSeek v4-pro 调用客户端。"""

    def __init__(self):
        self.client = OpenAI(
            api_key=DEEPSEEK_API_KEY,
            base_url=DEEPSEEK_BASE_URL or "https://api.deepseek.com/v1",
        )
        self.model = "deepseek-v4-pro"

    def call(self, prompt: str, temperature: float = 0.2, max_tokens: int = 8000) -> str:
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            return json.dumps({"error": str(e)})

    def call_json(self, prompt: str, temperature: float = 0.2, max_tokens: int = 8000) -> dict:
        text = self.call(prompt, temperature, max_tokens)
        try:
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                text = text.split("```")[1].split("```")[0].strip()
            return json.loads(text)
        except Exception as e:
            return {"error": f"JSON parse failed: {e}", "raw": text}


@dataclass
class SuspicionState:
    """怀疑状态：Agent 当前关注什么。"""
    current_question: str = ""
    suspicious_symbols: List[str] = field(default_factory=list)
    suspicious_files: List[str] = field(default_factory=list)


@dataclass
class InvestigationState:
    """调查状态。"""
    question: str = ""
    entry_file: str = ""
    max_steps: int = 10
    visited_files: List[str] = field(default_factory=list)
    frontier_files: List[str] = field(default_factory=list)
    evidence_log: List[Dict] = field(default_factory=list)
    suspicion: SuspicionState = field(default_factory=SuspicionState)
    files_content: Dict[str, str] = field(default_factory=dict)


class BaseInvestigator:
    """调查器基类。"""

    def __init__(self, max_steps: int = 10, repo_path: Optional[str] = None):
        self.max_steps = max_steps
        root = get_repo_root()
        self.repo_path = repo_path or (str(root) if root else None)
        self.repo_path_obj = Path(self.repo_path) if self.repo_path else None
        self.llm = LLMClient()
        self.extract_prompt = load_prompt("extract_evidence")
        self.answer_prompt = load_prompt("generate_answer")

    def _normalize_file_path(self, file_path: str) -> str:
        """统一文件路径为相对仓库根目录的路径。"""
        if not file_path:
            return file_path
        if self.repo_path_obj and file_path.startswith(str(self.repo_path_obj)):
            rel = Path(file_path).relative_to(self.repo_path_obj)
            return str(rel)
        return file_path

    def read_file(self, file_path: str) -> str:
        """读取文件内容。"""
        norm_path = self._normalize_file_path(file_path)
        if norm_path not in self.state.files_content:
            self.state.files_content[norm_path] = read_full_file(norm_path)
        return self.state.files_content[norm_path]

    def extract_symbols(self, content: str) -> List[str]:
        """从代码内容中提取函数/类/结构体名（启发式）。"""
        symbols = set()

        # 1) Function definitions: allow namespace-qualified return types
        # e.g., "inline dpct::err0 foo(...) {" or "static void bar(...) try {"
        func_pattern = re.compile(
            r'(?:^|\n)\s*(?:static\s+|inline\s+|virtual\s+|constexpr\s+)?'
            r'(?:[\w:\*\&<>\[\]]+\s+)+'
            r'(\w+)\s*\([^)]*\)\s*(?:const\s*)?(?:noexcept\s*)?(?:override\s*)?\s*(?:try\s*)?\{',
            re.MULTILINE
        )
        for m in func_pattern.finditer(content):
            symbols.add(m.group(1))

        # 2) Function declarations without body (e.g., in headers)
        decl_pattern = re.compile(
            r'(?:^|\n)\s*(?:static\s+|inline\s+|virtual\s+|constexpr\s+)?'
            r'(?:[\w:\*\&<>\[\]]+\s+)+'
            r'(\w+)\s*\([^)]*\)\s*(?:const\s*)?(?:noexcept\s*)?(?:override\s*)?\s*;',
            re.MULTILINE
        )
        for m in decl_pattern.finditer(content):
            symbols.add(m.group(1))

        # 3) Class/struct definitions
        class_pattern = re.compile(
            r'(?:^|\n)\s*(?:class|struct)\s+(?:[A-Z_]+\s+)?(\w+)',
            re.MULTILINE
        )
        for m in class_pattern.finditer(content):
            symbols.add(m.group(1))

        # Filter out common non-identifiers and C++ keywords
        stopwords = {
            'if', 'while', 'for', 'switch', 'return', 'else', 'catch', 'try',
            'class', 'struct', 'namespace', 'using', 'template', 'public',
            'private', 'protected', 'default', 'delete', 'override', 'final',
            'const', 'static', 'inline', 'virtual', 'explicit', 'operator',
            'true', 'false', 'nullptr', 'NULL', 'void', 'int', 'bool', 'size_t',
            'char', 'float', 'double', 'long', 'short', 'unsigned', 'signed',
            'auto', 'decltype', 'typename', 'public', 'private', 'protected',
            'noexcept', 'constexpr', 'consteval', 'constinit', 'mutable',
            'volatile', 'register', 'extern', 'friend', 'typedef', 'union',
            'enum', 'goto', 'case', 'break', 'continue', 'throw', 'new', 'delete',
        }
        filtered = [s for s in symbols if s not in stopwords and len(s) >= 2]
        return filtered

    def _prepare_content(self, file_path: str, content: str, max_chars: int = 12000) -> str:
        """为 LLM 准备文件内容：小文件全读，大文件保留头部和尾部。"""
        if len(content) <= max_chars:
            return content
        # Keep first half (often contains key declarations/definitions) and last half
        head_len = max_chars // 2
        tail_len = max_chars - head_len
        head = content[:head_len]
        tail = content[-tail_len:]
        return head + "\n\n... [中间内容省略] ...\n\n" + tail

    def extract_evidence(self, file_path: str, content: str) -> Dict:
        """使用 LLM 从文件中提取证据和新的怀疑对象。候选符号由本地正则提取，避免 LLM 幻觉。"""
        prepared = self._prepare_content(file_path, content)
        candidate_symbols = self.extract_symbols(content)
        candidate_list = ", ".join(candidate_symbols[:30]) if candidate_symbols else "(无)"
        prompt = self.extract_prompt.format(
            question=self.state.question,
            file_path=file_path,
            current_question=self.state.suspicion.current_question,
            content=prepared,
            candidate_symbols=candidate_list,
        )
        result = self.llm.call_json(prompt)
        # Filter returned symbols to only those that actually appear in content
        returned_symbols = result.get("suspicious_symbols", [])
        valid_symbols = [s for s in returned_symbols if s in content]
        return {
            "file_path": file_path,
            "key_facts": result.get("key_facts", []),
            "new_hypothesis": result.get("new_hypothesis", ""),
            "suspicious_symbols": valid_symbols if valid_symbols else candidate_symbols[:10],
            "suspicious_files": result.get("suspicious_files", []),
            "raw": result,
        }

    def expand_frontier_from_symbols(self, symbols: List[str], limit: int = 5) -> List[str]:
        """基于可疑符号，用 grep 扩展 frontier。"""
        new_files = set()
        for symbol in symbols:
            if not symbol or len(symbol) < 2:
                continue
            try:
                files = grep_files(symbol, self.repo_path, limit=limit)
                for f in files:
                    f = self._normalize_file_path(f)
                    if f not in self.state.visited_files and f not in self.state.frontier_files:
                        new_files.add(f)
            except Exception:
                continue
        return sorted(new_files)

    def update_frontier(self, evidence: Dict, limit: int = 5) -> List[str]:
        """基于证据中的 suspicious_symbols 更新 frontier。"""
        symbols = evidence.get("suspicious_symbols", [])
        # Also include suspicious_files if they exist
        files = [self._normalize_file_path(f) for f in evidence.get("suspicious_files", [])]
        new_from_symbols = self.expand_frontier_from_symbols(symbols, limit=limit)
        new_from_files = [f for f in files if f not in self.state.visited_files and f not in self.state.frontier_files]
        new_files = sorted(set(new_from_symbols + new_from_files))
        self.state.frontier_files.extend(new_files)
        return new_files

    def bootstrap_frontier(self, symbols: List[str], top_k_symbols: int = 2) -> List[str]:
        """初始阶段：对最靠前的几个符号做全面 grep，获取所有相关文件。"""
        new_files = set()
        for symbol in symbols[:top_k_symbols]:
            if not symbol or len(symbol) < 2:
                continue
            try:
                files = grep_files(symbol, self.repo_path, limit=100)
                for f in files:
                    f = self._normalize_file_path(f)
                    if f not in self.state.visited_files and f not in self.state.frontier_files:
                        new_files.add(f)
            except Exception:
                continue
        new_files = sorted(new_files)
        self.state.frontier_files.extend(new_files)
        return new_files

    def generate_answer(self) -> str:
        """基于调查过程生成答案。"""
        evidence_log = "\n\n".join(
            f"=== {e['file_path']} ===\n"
            f"Key facts: {e.get('key_facts', [])}\n"
            f"Hypothesis: {e.get('new_hypothesis', '')}\n"
            f"Suspicious symbols: {e.get('suspicious_symbols', [])}"
            for e in self.state.evidence_log
        )

        files_summary = "\n\n".join(
            f"=== {fp} ===\n{content[:1500]}"
            for fp, content in self.state.files_content.items()
        )

        prompt = self.answer_prompt.format(
            question=self.state.question,
            evidence_log=evidence_log,
            files_summary=files_summary,
        )
        return self.llm.call(prompt)

    def run(self, question: str, entry_file: str) -> Dict:
        """运行调查。子类必须实现。"""
        raise NotImplementedError
