"""跨题函数知识记忆（v2）：记住"函数是干什么的"，而不是"对某题相关吗"。

设计要点（吸取 v1 负结果教训）：
- 只存**问题无关的函数知识**：职责一句话、行号范围、签名
- 绝不存"对某题相关/不相关"——不同问题关心的完全不同
- read_function 命中记忆时返回摘要（免重读省 token），知识来源是此前的真实阅读
- 职责基底来自全量 function_summaries.json（23689 个），调查中可增量补充
"""
from __future__ import annotations

import json
import threading
from pathlib import Path


class FunctionMemory:
    def __init__(self, path: Path | str, summaries_path: Path | str | None = None):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.data = json.load(open(path)) if self.path.exists() else {}
        # 真实阅读缓存：只有实际读过完整实现的函数才进这里
        self.read_cache: dict = self.data.setdefault("read_cache", {})
        # 职责基底（只读，用于展示增强，不参与免读）
        self.base_roles: dict = {}
        if summaries_path and Path(summaries_path).exists():
            base = json.load(open(summaries_path))
            for fid, role in base.items():
                parts = fid.rsplit(":", 2)
                if len(parts) == 3:
                    self.base_roles[f"{parts[1]}@{parts[0]}"] = role

    def lookup_role(self, name: str, file_path: str) -> str:
        """职责一句话（展示用）：优先真实阅读缓存，其次基底。"""
        key = f"{name}@{file_path}"
        e = self.read_cache.get(key)
        if e:
            return e["role"]
        return self.base_roles.get(key, "")

    def note_read(self, name: str, file_path: str, code: str, start_line: int, end_line: int) -> None:
        """真实读完后写入缓存：职责（取基底或代码首行）+ 代码摘录。

        摘录超过 1500 字符被截断时标 truncated——命中回放只允许"完整"条目，
        长函数必须重新真读（否则 Agent 会拿着 1500 字符的片段以为是完整实现，
        答案证据注册表也会被污染）。
        """
        key = f"{name}@{file_path}"
        with self._lock:
            if key in self.read_cache:
                return
            role = self.base_roles.get(key) or code.strip().split("\n")[0][:60]
            self.read_cache[key] = {
                "role": role,
                "code_excerpt": code[:1500],
                "truncated": len(code) > 1500,
                "start_line": start_line,
                "end_line": end_line,
            }

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1))
