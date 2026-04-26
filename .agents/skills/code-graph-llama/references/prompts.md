# Code_Graph v7 提示词模板参考

> 本文件包含经过多轮实验验证的优化提示词模板。
> 实验背景：360 题 llama.cpp 代码库 QA benchmark。
> 关键发现：P0（混合策略+智能停止）准确率 ~74%，P1（LLM 路由）~71%，V7 Minimal（移除扩展）~66%。

---

## 1. ReAct 决策提示词 (`react_decide`)

**用途**：在每一轮扩展前，判断当前检索到的信息是否足以准确回答问题。

```python
REACT_DECIDE_PROMPT = """{context}

---

你是代码检索决策专家。你的任务 ONLY 是判断：现有的检索证据是否足以准确、无推测地回答上述问题。

=== CRITICAL DECISION CONSTRAINTS ===
1. 如果【已收集函数】≤ 3 个 → sufficient 必须是 FALSE。
2. 如果问题涉及调用关系（调用、caller、callee、流程、依赖）且【已扩展调用链】为空 → sufficient 必须是 FALSE。
3. 如果问题涉及文件结构（"文件中有哪些函数/结构"）且未使用 get_file_functions → sufficient 必须是 FALSE。
4. 如果 top_score < 0.5 且未触发 grep_fallback → sufficient 必须是 FALSE。

=== 分析过程 (在 <analysis> 标签中完成) ===
在给出决策前，你必须先在 <analysis> 中完成以下检查：
1. 问题类型判断：[具体实现 / 调用链 / 文件结构 / 设计原理 / Bug排查]
2. 信息缺口：列出当前证据中缺失的关键信息
3. 每个已检索到的函数与问题的相关性评估（1-2句话）
4. 停止风险评估：如果现在停止，有哪些论断是基于推测而非证据的？

=== 你会想找的借口 — 不要上当 ===
- "现有函数覆盖了核心概念" → 如果 ≤3 个函数，这是谎言。
- "基于函数名可以推断..." → 停止。去调用工具看代码。
- "Issue 搜索没找到，可能不需要" → Bug 类问题必须有 Issue 证据。
- "再扩展调用链收益不高" → 对于调用关系问题，没看调用链就是没证据。

=== 最终决策 ===
在 <decision> 标签中输出以下 JSON（不要加 markdown 代码块）：
{{
    "sufficient": true/false,
    "action": "expand_callers|expand_callees|explore_file|read_source|search_issues|sufficient",
    "target": "目标函数名或文件路径（如 action 为 sufficient 则留空）",
    "reason": "20字以内的决策原因"
}}

CRITICAL: 只输出 <analysis>...</analysis> 和 <decision>...</decision>，不要其他内容。
"""
```

**解析逻辑**：
```python
import re

def parse_react_decision(response_text: str) -> dict:
    decision_match = re.search(r'<decision>(.*?)</decision>', response_text, re.DOTALL)
    if decision_match:
        json_text = decision_match.group(1).strip()
        json_text = re.sub(r'^```json\s*', '', json_text)
        json_text = re.sub(r'\s*```$', '', json_text)
        return json.loads(json_text)
    # fallback: 直接找最像 JSON 的大括号块
    ...
```

---

## 2. 答案生成提示词 (`generate_answer`)

**用途**：基于检索到的证据生成最终答案。

```python
ANSWER_GENERATION_PROMPT = """基于以下检索到的证据，回答用户的问题。

=== CRITICAL: 证据审计规则 ===
在最终答案前，你必须在 <evidence_audit> 标签中完成以下检查：
1. 列出你计划回答的每一个要点
2. 为每个要点标明支持它的具体证据（函数名、文件路径、Issue 编号、代码片段）
3. 如果某个要点没有直接证据支持，将其标记为 [UNVERIFIED] 并从最终答案中删除或明确标注为"无法确认"

=== 禁止行为 ===
- MUST NOT 基于函数名推测实现
- MUST NOT 将无关函数强行解释为答案
- MUST NOT 使用"推测"、"可能"来替代证据

用户问题: {question}

检索证据:
{evidence}

请在 <evidence_audit>...</evidence_audit> 之后，用中文输出清晰、结构化的最终答案。
"""
```

---

## 3. LLM Judge 提示词 (`binary_judge`)

**用途**：评估生成答案的正确性。

```python
BINARY_JUDGE_PROMPT = """你是一个对抗性评审员。你的任务不是"确认答案不错"，而是"尽力找出答案中的错误"。

=== 评审策略 ===
1. 先独立回答问题（基于你自己的知识），形成标准答案
2. 逐句检查生成答案，寻找以下错误类型：
   - 事实错误：提到的函数、文件、Issue 不存在或属性错误
   - 归因错误：将 A 的功能/归属说成 B 的
   - 推测冒充事实：用确定的语气陈述没有证据支持的内容
   - 遗漏关键信息：回避了问题的核心
   - 答非所问：回答的是相关问题，但不是用户实际问的问题

=== 你会想找的借口 — 不要上当 ===
- "生成答案和参考答案方向一致" → 方向一致不代表事实正确
- "虽然细节有小错，但核心是对的" → 如果核心论断的证据错误，整体就是错误
- "参考答案可能不完整" → 你的任务是评生成答案，不是维护参考答案

=== 输出格式 ===
在 <analysis> 标签中列出你发现的每个问题点（即使最后判定为 CORRECT，也要列出你检查过的维度）。

首行必须输出：结果: CORRECT 或 结果: INCORRECT
第二行起：基于上述分析给出最终结论和关键证据。

【问题】
{question}

【参考答案】
{reference}

【生成答案】
{generated}
"""
```

---

## 4. 实体提取提示词 (`extract_entities`)

**用途**：从用户问题中提取关键代码实体。

```python
EXTRACT_ENTITIES_PROMPT = """从以下问题中提取关键代码实体（函数名、类名、变量名、文件名等）。

问题: {question}

要求:
1. 只提取具体的标识符名称
2. 如果问题问的是"函数xxx"，提取xxx
3. 如果问的是"模块yyy"，提取yyy
4. 如果包含文件路径（如 ggml-alloc.c），提取文件名
5. 最多返回3个最相关的实体

返回JSON格式:
{{"entities": ["entity1", "entity2", "entity3"]}}

只输出JSON，不要解释。
"""
```

---

## 5. 路由决策提示词 (`llm_route_decision`) — 降级版

**实验发现**：纯 LLM 路由（P1）是负优化，增加了延迟但准确率下降。
**建议**：不要用这个 prompt 做每道题的路由。改用**快速关键词规则路由**（见 tool-selection-guide.md）。

如果确实需要 LLM 辅助决策（如 hybrid 融合时），使用以下精简版：

```python
LLM_ROUTE_PROMPT = """分析问题特征，辅助选择检索策略。

问题: {question}

只需回答以下 JSON：
{{
    "entities": ["提取的函数名/类名/变量名，最多3个"],
    "has_call_chain_intent": true/false,
    "has_file_structure_intent": true/false,
    "has_bug_issue_intent": true/false
}}

只输出JSON:"""
```

---

## Prompt 工程关键洞察

1. **约束比建议有效**："不要编造"太温和，"STRICTLY PROHIBITED from..."才有效。
2. **负面示例能显著减少模型作弊**：提前列出模型常用的借口并封死。
3. **XML 结构化思维链** (`<analysis>` / `<decision>`) 比纯 JSON 更稳定，且 `<analysis>` 可以被 strip 不污染上下文。
4. **Judge 要从"温和评审"改为"对抗性挑刺"**，才能捕获脑补和归因错误。
