"""渐进式代码展开管理器。

为了节省token，先给LLM看函数签名列表，
让LLM决定哪些函数值得展开看完整实现，
再决定是否需要看完整类或完整文件。
"""
from __future__ import annotations

from .models import RetrievedFunction, ExpandLevel
from .tools.file_reader import read_function, extract_signature, read_full_file
from .tools.class_reader import expand_class





class CodeExpander:
    """
    管理函数代码的渐进式展开：
    SIGNATURE → BODY → CLASS → FULL_FILE
    """

    def __init__(self, signature_budget: int = 4000, body_budget: int = 40000):
        """
        Args:
            signature_budget: 签名列表的token预算（约字符数/4）
            body_budget: 完整实现的token预算
        """
        self.signature_budget = signature_budget
        self.body_budget = body_budget

    @staticmethod
    def from_retrieval_result(result) -> RetrievedFunction:
        """将 RetrievalResult 转为 RetrievedFunction"""
        meta = result.metadata or {}
        name = meta.get("name", result.id)
        if not name or name.startswith("grep:"):
            # 从id解析：grep:file:line
            parts = result.id.split(":")
            if len(parts) >= 3:
                name = f"{parts[1].split('/')[-1]}:{parts[2]}"
            else:
                name = result.id

        start_line = meta.get("start_line", 0) or meta.get("line", 0) or 0
        end_line = meta.get("end_line", 0) or 0

        signature = meta.get("signature", "")
        # 保留所有 retriever 的 content 作为初始 body（grep 的上下文、graph 的代码等）
        body = result.content or ""

        # 如果 signature 为空但 content/body 中有代码，尝试提取签名
        if not signature and body:
            sig = extract_signature(body.splitlines(), name)
            if sig and len(sig) > 5:
                signature = sig

        # 不再预标记为 BODY：embedding/graph 的 content 只是摘要文本，
        # 完整代码应由 build_body_context 中统一调用 read_function 加载。
        expand_level = ExpandLevel.SIGNATURE

        return RetrievedFunction(
            name=name,
            file_path=meta.get("file_path", ""),
            start_line=start_line,
            end_line=end_line,
            signature=signature,
            body=body,
            score=result.score,
            source=result.source,
            metadata=meta,
            expand_level=expand_level,
        )

    def expand(
        self,
        func: RetrievedFunction,
        level: ExpandLevel = ExpandLevel.BODY,
    ) -> RetrievedFunction:
        """将函数展开到指定级别"""
        if func.expand_level.value >= level.value:
            return func

        # LEVEL_BODY: 读取完整函数实现
        # 条件：必须有准确的 start_line + end_line（来自 Neo4j / embedding index）
        # 没有 end_line 的情况通常是 grep 单行匹配，其 content 已作为 body 保留
        if level.value >= ExpandLevel.BODY.value and func.file_path and func.start_line and func.end_line:
            code = read_function(func.file_path, func.start_line, func.end_line)
            if code:
                func.body = code
                if not func.signature:
                    func.signature = extract_signature(code.splitlines(), func.name)
                func.expand_level = ExpandLevel.BODY

        # LEVEL_CLASS: 读取完整类实现
        if level.value >= ExpandLevel.CLASS.value:
            class_result = expand_class(func.name)
            if class_result:
                func.body = f"// ===== 类: {class_result.name} =====\n{class_result.body}\n\n// ===== 函数在类中的位置 =====\n{func.body}"
                func.expand_level = ExpandLevel.CLASS

        # LEVEL_FULL_FILE: 读取完整文件
        if level.value >= ExpandLevel.FULL_FILE.value and func.file_path:
            full = read_full_file(func.file_path)
            if not full.startswith("//"):
                func.body = f"// ===== 文件: {func.file_path} =====\n{full}\n\n// ===== 目标函数位置: {func.start_line}-{func.end_line} =====\n{func.body}"
                func.expand_level = ExpandLevel.FULL_FILE

        return func

    def build_signature_context(self, functions: list[RetrievedFunction]) -> str:
        """构建仅含签名的上下文（最省token）"""
        lines = ["【相关函数签名列表】"]
        if len(functions) > 15:
            lines.append(f"（共 {len(functions)} 个函数，按代码中出现顺序排列）")
        for i, f in enumerate(functions, 1):
            sig = f.signature or f.name
            lines.append(f"{i}. {f.name} @ {f.file_path}:{f.start_line}-{f.end_line}")
            lines.append(f"   签名: {sig[:200]}")
            lines.append(f"   来源: {f.source}  分数: {f.score:.3f}")
        return "\n".join(lines)

    def build_body_context(
        self,
        functions: list[RetrievedFunction],
        budget_chars: int = 60000,
        priority_names: list[str] | None = None,
    ) -> str:
        """构建含完整实现的上下文（按budget截断）。priority_names 中的函数会排在最前面。"""
        # 把 priority_names 匹配的函数排到前面
        if priority_names:
            name_set = set(priority_names)
            priority_funcs = [f for f in functions if f.name in name_set]
            other_funcs = [f for f in functions if f.name not in name_set]
            functions = priority_funcs + other_funcs

        lines = ["【相关函数详细实现】"]
        total = 0
        for i, f in enumerate(functions, 1):
            # 确保已展开到BODY
            if f.expand_level.value < ExpandLevel.BODY.value:
                self.expand(f, ExpandLevel.BODY)

            header = f"\n--- {i}. {f.name} ({f.file_path}:{f.start_line}-{f.end_line}) [{f.source}] ---\n"
            body = f.display_text
            block = header + body
            if total + len(block) > budget_chars:
                remaining = budget_chars - total
                if remaining > 200:
                    lines.append(block[:remaining] + "\n... (truncated)")
                lines.append(f"\n... 还有 {len(functions) - i} 个函数未展示（预算限制）")
                break
            lines.append(block)
            total += len(block)
        return "\n".join(lines)

    def build_full_context(
        self,
        functions: list[RetrievedFunction],
        issues: list = None,
        budget_chars: int = 100000,
        priority_names: list[str] | None = None,
    ) -> str:
        """构建完整上下文（含函数实现+Issue）"""
        parts = []
        parts.append(self.build_body_context(functions, budget_chars=int(budget_chars * 0.8), priority_names=priority_names))

        if issues:
            parts.append("\n\n【相关Issue】")
            for i, issue in enumerate(issues[:3], 1):
                parts.append(f"{i}. {issue.id}: {issue.content[:300]}")

        return "\n".join(parts)
