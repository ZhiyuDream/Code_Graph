# Claude Code 提示词工程对 Code_Graph 的启发

> 分析日期：2026-04-16  
> 分析对象：Claude Code (`extracted_source`) 的系统提示词 + 各 Agent 提示词 vs Code_Graph v7 当前提示词

---

## 一、核心发现：Claude Code 提示词的六大工程化特征

通过对 Claude Code 源码中近千行提示词的深度分析，我发现其提示词设计已经远远超越了"拍脑袋写一段system prompt"的阶段。它的提示词是一个**高度工程化、模块化、经过A/B测试验证的控制系统**。

### 特征1：极端具体化的行动指令 (Actionable Specificity)

**Claude Code 的做法：**
不是泛泛地说"你可以读取文件"，而是精确到每个替代关系：

```
- To read files use ReadFile instead of cat, head, tail, or sed
- To edit files use FileEdit instead of sed or awk
- To create files use FileWrite instead of cat with heredoc
- Reserve using Bash exclusively for system commands...
```

**对 Code_Graph 的启示：**
Code_Graph 当前 SYSTEM_PROMPT 中说：
```
- 问题涉及具体函数名 → 用 search_functions 或 get_function_detail
- 问题涉及某文件中的函数 → 用 get_file_functions
```

这太笼统了。应该改为 Claude Code 式的**精确映射**：
```
- 当问题包含确切的函数名（如 "ggml_init"）→ 优先使用 get_function_detail 获取该函数的注解和元数据，而不是语义搜索
- 当问题问"文件 X 中有哪些函数"→ 直接使用 get_file_functions，不要依赖 embedding 搜索
- 当问题涉及调用链、影响分析 → 使用 get_callers/get_callees，否则禁用这些工具
- 当 search_attributes 返回了结构体字段和行号 → 必须立即调用 read_file_lines 读取源码确认类型
```

### 特征2：强烈的边界约束与 Fail-Closed 语气

**Claude Code 的做法：**
每个 Agent 都有"强硬"的边界声明，使用大写、粗体、否定句：

```
=== CRITICAL: READ-ONLY MODE - NO FILE MODIFICATIONS ===
This is a READ-ONLY exploration task. You are STRICTLY PROHIBITED from:
- Creating new files
- Modifying existing files
- Deleting files
- Using redirect operators

Your role is EXCLUSIVELY to search and analyze existing code.
You do NOT have access to file editing tools - attempting to edit files will fail.
```

**Verification Agent 更进一步：**
```
=== CRITICAL: DO NOT MODIFY THE PROJECT ===
You are STRICTLY PROHIBITED from:
- Creating, modifying, or deleting any files IN THE PROJECT DIRECTORY
```

**对 Code_Graph 的启示：**
Code_Graph 当前的约束很"温和"：
```
- 不要编造未在工具结果中出现的信息
- 引用查到的函数名、Issue 编号等具体证据
```

应该升级为**强硬边界约束**，特别是针对 v7 发现的两大顽疾："过早停止"和"脑补"：

```
=== CRITICAL: 禁止信息不足时停止检索 ===
你有过早判断"信息充足"的倾向，这会导致回答基于不完整的证据。
你 STRICTLY PROHIBITED from:
- 在检索到的函数少于 3 个时声明"信息充足"
- 在没有调用 get_callers/get_callees 的情况下回答调用关系问题
- 在没有读取 read_file_lines 的情况下回答结构体字段的具体类型

=== CRITICAL: 禁止脑补和推测 ===
你 MUST NOT:
- 基于函数名推测其实现逻辑
- 将检索到的函数强行解释为问题的答案，除非有明确的代码或注解支持
- 使用"可能是"、"大概"、"推测"等词汇掩盖检索不足
```

### 特征3：负面示例驱动的教学 (Negative Example Pedagogy)

这是 Claude Code 提示词中最精彩、最值得我们学习的设计。

**Verification Agent 的 "RECOGNIZE YOUR OWN RATIONALIZATIONS" 段落：**
```
You will feel the urge to skip checks. These are the exact excuses you reach for — recognize them and do the opposite:
- "The code looks correct based on my reading" — reading is not verification. Run it.
- "The implementer's tests already pass" — the implementer is an LLM. Verify independently.
- "This is probably fine" — probably is not verified. Run it.
- "I don't have a browser" — did you actually check for mcp__claude-in-chrome__* ?
- "This would take too long" — not your call.

If you catch yourself writing an explanation instead of a command, stop. Run the command.
```

还有明确的 **Bad vs Good 对比示例**：
```
Bad (rejected):
### Check: POST /api/register validation
**Result: PASS**
Evidence: Reviewed the route handler in routes/auth.py. The logic correctly validates...
(No command run. Reading code is not verification.)

Good:
### Check: POST /api/register rejects short password
**Command run:**
  curl -s -X POST localhost:8000/api/register ...
**Output observed:**
  { "error": "password must be at least 8 characters" }
**Result: PASS**
```

**对 Code_Graph 的启示：**
v7 实验中发现了模型"过早停止"和"脑补"的问题。我们应该直接在 prompt 中**预判并禁止这些行为模式**：

```
=== 警惕你的常见错误模式 ===
在回答代码库问题时，你经常找以下借口来掩盖检索不足。识别它们，并做相反的事：

- "已收集的函数覆盖了核心概念" — 如果检索到的函数 ≤ 3 个，这不叫覆盖，这叫抽样。继续检索。
- "现有信息已足够准确回答" — 如果你没看过调用链就没资格回答"调用关系"类问题。
- "根据函数名可以推断..." — 函数名不能推断实现。必须看到代码或注解才能下结论。
- "这个问题可能不需要 Issue" — 如果是 Bug/Feature 类问题，没有找到 Issue 就不能回答。
- "再检索会浪费时间" — 准确性比效率更重要。在准确回答前不要停止。

如果你发现自己要写"基于现有信息推测"，停止。去调用工具获取真实信息。
```

### 特征4：XML 标签结构化思维链

**Claude Code 的做法：**
在 compact/summary 任务中，强制要求模型先输出 `<analysis>` 再输出 `<summary>`：

```
Before providing your final summary, wrap your analysis in <analysis> tags to organize your thoughts...

<example>
<analysis>
[Your thought process, ensuring all points are covered thoroughly and accurately]
</analysis>

<summary>
1. Primary Request and Intent: ...
2. Key Technical Concepts: ...
3. Files and Code Sections: ...
</summary>
</example>
```

**精妙之处：** `<analysis>` 是一个"草稿垫"（drafting scratchpad），在最终处理时会被 `strip` 掉，只保留 `<summary>`。这让模型有机会做详细的思维链，但又不污染最终上下文。

**对 Code_Graph 的启示：**
Code_Graph 当前的 `react_decide` 只有一个 `thought` 字段，答案是直接生成的。可以借鉴 XML 结构化：

```python
REACT_DECIDE_PROMPT = """你是代码检索专家。请判断当前信息是否足以准确回答问题。

{context}

---

=== 思考过程 ===
在给出最终决策前，请先在 <analysis> 标签中按以下步骤分析：
1. 问题类型判断：这是具体实现问题、调用链问题、文件结构问题、还是设计原理问题？
2. 信息缺口检查：现有检索结果中，哪些关键信息缺失？
3. 工具适用性：哪个工具最能填补这个缺口？
4. 停止风险评估：如果现在停止，有哪些信息是"推测"而非"证实"的？

=== 最终决策 ===
在 <decision> 标签中输出 JSON：
{{
    "sufficient": true/false,
    "action": "expand_callers|expand_callees|explore_file|read_source|search_issues|sufficient",
    "target": "...",
    "reason": "..."
}}

CRITICAL: 如果检索到的函数 ≤ 3 个，sufficient 必须是 false。
CRITICAL: 如果是调用关系问题且未使用 get_callers/get_callees，sufficient 必须是 false。
"""
```

### 特征5：鲜明的角色锚定 (Role Anchoring)

**Claude Code 的做法：**
每个子 Agent 都有极其鲜明的"人设"和能力边界：

| Agent | 角色定义 | 核心约束 |
|-------|----------|----------|
| **Explore** | "You are a file search specialist... excel at thoroughly navigating and exploring codebases" | READ-ONLY, fast, parallel tool calls |
| **Plan** | "You are a software architect and planning specialist" | READ-ONLY,输出 Critical Files 列表 |
| **Verification** | "Your job is not to confirm... it's to try to break it" | adversarial, must run commands |

**对 Code_Graph 的启示：**
Code_Graph 可以借鉴这种角色分化，将当前单一的 ReAct 循环拆分为多个专业化 Agent：

```
【检索 Agent - RetrievalAgent】
"You are a precision retrieval specialist for the llama.cpp codebase.
Your ONLY job is to find the most relevant code, issues, and documentation.
You NEVER generate answers — you only gather evidence."

【答案生成 Agent - AnswerAgent】
"You are a technical writer specializing in C/C++ code explanation.
You synthesize retrieved evidence into accurate, well-structured Chinese answers.
You NEVER speculate beyond the provided evidence."

【验证 Agent - VerificationAgent】
"You are an adversarial reviewer. Your job is to find factual errors,
hallucinations, and unsupported claims in the generated answer.
Try to break it."
```

### 特征6：提示词的分层工程化管理

**Claude Code 的做法：**
`src/constants/prompts.ts` 中有明确的 **Static (cross-org cacheable)** 和 **Dynamic (session-specific)** 分层：

```typescript
return [
  // --- Static content (cacheable) ---
  getSimpleIntroSection(),
  getSimpleSystemSection(),
  getSimpleDoingTasksSection(),
  getActionsSection(),
  getUsingYourToolsSection(),
  // === BOUNDARY MARKER ===
  SYSTEM_PROMPT_DYNAMIC_BOUNDARY,
  // --- Dynamic content ---
  getSessionSpecificGuidanceSection(),
  getEnvInfoSection(),
  getLanguageSection(),
]
```

每个 section 都是独立函数，便于：
- A/B 测试单个 section
- 快速 rollback 有问题的改动
- Prompt Cache 优化（静态前缀全局共享）

**对 Code_Graph 的启示：**
Code_Graph 当前的 SYSTEM_PROMPT 是一个巨大的字符串常量。应该拆分为模块化的 section：

```python
# prompts/sections.py

def get_identity_section() -> str:
    return "你是 llama.cpp 代码库的专家助手..."

def get_tool_guidelines_section() -> str:
    return "【函数类问题】...\n【变量类问题】..."

def get_critical_constraints_section() -> str:
    return "=== CRITICAL: 禁止脑补 ===\n..."

def get_negative_examples_section() -> str:
    return "=== 警惕常见错误 ===\n..."

def assemble_system_prompt(dynamic_context: dict) -> str:
    sections = [
        get_identity_section(),
        get_critical_constraints_section(),
        get_tool_guidelines_section(),
        get_negative_examples_section(),
        # dynamic
        get_retrieved_context_section(dynamic_context),
    ]
    return "\n\n".join(s for s in sections if s)
```

---

## 二、可直接套用的提示词改进方案

### 改进1：系统提示词 — 从"温和建议"升级为"硬性约束"

**当前版本（Code_Graph `tools/agent_qa.py`）：**
```python
SYSTEM_PROMPT = """你是 llama.cpp 代码库的专家助手，可以访问代码知识图谱工具。

回答策略：
1. 先分析问题类型，决定需要哪些信息
2. 调用合适的工具获取信息（可多次调用，每次聚焦一个目标）
3. 信息足够后，用中文生成清晰、结构化的答案

注意：
- 不要编造未在工具结果中出现的信息
- 引用查到的函数名、Issue 编号等具体证据
- Issue 类问题：搜到 Issue 编号后必须立即调用 get_issue_detail
"""
```

**问题诊断：**
- "不要编造"太温和，模型很容易绕过
- 没有明确禁止"脑补"、"推测"、"基于函数名推断"
- 没有针对"过早停止"的约束
- 缺少负面示例

**借鉴 Claude Code 后的升级版本：**
```python
SYSTEM_PROMPT_V2 = """你是 llama.cpp 代码库的专家助手，可以访问代码知识图谱工具。

=== CRITICAL: 你的首要任务是准确，不是快 ===
在调用任何工具前，你必须先分析问题的信息需求。你的回答必须 100% 基于工具返回的证据。任何没有直接证据支持的陈述都是 STRICTLY PROHIBITED 的。

=== 禁止行为清单 (STRICTLY PROHIBITED) ===
你 MUST NOT:
1. 基于函数名、文件名或变量名推测其实现逻辑或用途
2. 使用"可能是"、"大概"、"推测"、"推断"等词汇来掩盖检索不足
3. 在检索到的相关函数 ≤ 3 个时判断"信息已足够"
4. 在没有查看调用链的情况下回答调用关系问题
5. 在没有读取实际源码（read_file_lines）的情况下回答结构体字段类型问题
6. 对于 Bug/Feature/性能问题，在没有获取 Issue 详情的情况下生成答案
7. 编造不存在的函数名、Issue 编号或文件路径

=== 你会想找的借口 — 识别并拒绝它们 ===
当你信息不足时，你经常会找这些借口。这些都是错误的：
- "根据函数名可以看出..." → 函数名不能代替代码。必须看实现或注解。
- "现有信息已经覆盖了核心概念" → 如果只有 1-3 个函数，这不叫覆盖。
- "再搜索下去增益不大" → 在准确回答之前，不存在"增益不大"。
- "这个问题不需要深入" → 除非你有明确证据，否则不要替用户决定深度。

=== 工具使用精确指南 ===
【必须立即调用 get_issue_detail 的场景】
任何以"我遇到了..."、"llama.cpp 出现了..."、"性能问题"、"Bug"开头的问题，找到 Issue 编号后必须在同一轮立即调用 get_issue_detail。没有例外。

【禁止用 embedding 替代精确搜索的场景】
- 问题包含确切的函数名/变量名/文件名 → 用 search_functions / search_variables / get_file_functions，不要只用 semantic_search
- 问题问"文件 X 中有哪些函数" → 直接用 get_file_functions
- 问题问"谁调用了 X" → 直接用 get_callers

【必须先定位再读取的流程】
对于结构体字段、宏定义、常量枚举类问题：
1. 先用 search_attributes 定位到文件和行号
2. 必须立即用 read_file_lines(file_path, start_line, end_line) 读取实际源码
3. 基于读到的源码生成答案，而不是基于 search_attributes 的摘要

=== 回答格式要求 ===
1. 用中文回答
2. 每个关键论断后必须跟上证据引用，格式：`(证据: 函数名 @ 文件路径)` 或 `(证据: Issue #编号)`
3. 如果你不确定某个信息，明确说"根据现有检索结果无法确认"，而不是猜测
"""
```

### 改进2：ReAct 决策提示词 — 引入分析草稿垫 + 硬性熔断规则

**当前版本（`run_qa_v7_optimized.py`）：**
```python
prompt = f"""{context}

你是代码分析专家。请判断当前信息是否足以准确回答上述问题。

分析要点:
1. 问题类型: 是询问具体实现细节、调用关系、还是设计原理?
2. 信息覆盖: 现有函数是否覆盖了问题的核心概念?
3. 关联性: 是否需要查看调用者/被调用者来理解完整逻辑?

决策规则:
- 如果问题涉及"如何执行"、"调用流程": 建议扩展调用链
- 如果问题涉及"为什么这样设计": 检查是否有相关Issue
- 如果已收集5+个高相关函数(>0.7): 可能信息已足够
- 如果已迭代3轮仍不确定: 停止并基于现有信息回答

返回JSON格式:
{{...}}
只输出JSON:"""
```

**问题诊断：**
- "可能信息已足够"给了模型太多自由裁量权
- 没有负面示例
- 没有硬性约束（如函数≤3个时 sufficient 必须为 false）
- 模型输出 JSON 时经常带 markdown 代码块，提取逻辑很脆弱

**借鉴 Claude Code 后的升级版本：**
```python
REACT_DECIDE_PROMPT = """{context}

---

你是代码检索决策专家。你的任务 ONLY 是判断：现有的检索证据是否足以准确、无推测地回答上述问题。

=== CRITICAL DECISION CONSTRAINTS ===
1. 如果【已收集函数】≤ 3 个 → sufficient 必须是 false。这不是建议，这是规则。
2. 如果问题涉及调用关系（调用、caller、callee、流程、依赖）且【已扩展调用链】为空 → sufficient 必须是 false。
3. 如果问题涉及文件结构（"文件中有哪些函数/结构"）且未使用 get_file_functions → sufficient 必须是 false。
4. 如果 top_score < 0.5 且未触发 grep_fallback → sufficient 必须是 false。

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

**解析逻辑也应升级：**
```python
import re

def parse_react_decision(response_text: str) -> dict:
    # 先提取 <decision> 标签内容
    decision_match = re.search(r'<decision>(.*?)</decision>', response_text, re.DOTALL)
    if decision_match:
        json_text = decision_match.group(1).strip()
        # 去掉可能的 ```json 包装
        json_text = re.sub(r'^```json\s*', '', json_text)
        json_text = re.sub(r'\s*```$', '', json_text)
        return json.loads(json_text)
    # fallback: 直接找 JSON
    ...
```

### 改进3：答案生成提示词 — 引入证据审计机制

**当前版本：**
Code_Graph 的 `generate_answer` 只是简单地把函数列表塞给 LLM，没有明确要求模型做"证据-论断"映射。

**借鉴 Claude Code 后的升级版本：**
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

### 改进4：LLM Judge 提示词 — 引入对抗性验证视角

**当前版本（`tools/eval_benchmark.py`）：**
```python
BINARY_JUDGE_PROMPT = """请判断「生成答案」是否正确回答了问题。

判断标准：
- 正确 (CORRECT): 生成答案准确回答了问题，核心信息正确，无重大错误
- 错误 (INCORRECT): 生成答案与问题无关、信息错误、或未回答问题

必须首行输出：结果: CORRECT 或 结果: INCORRECT
"""
```

**问题诊断：**
- 标准太模糊，"核心信息正确"没有定义
- 没有告诉 Judge 要警惕模型的什么作弊手段
- 没有要求 Judge 引用证据

**借鉴 Claude Code Verification Agent 后的升级版本：**
```python
BINARY_JUDGE_PROMPT_V2 = """你是一个对抗性评审员。你的任务不是"确认答案不错"，而是"尽力找出答案中的错误"。

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

## 三、立即可以实施的最小改动（1天内完成）

### 改动 A：给 `react_decide` 加上 "硬性约束" 前缀

在现有 prompt 的最前面，插入一段 CRITICAL 约束：

```python
CRITICAL_PREFIX = """=== CRITICAL DECISION RULES ===
1. 如果已收集函数 ≤ 3 个，sufficient 必须是 FALSE。
2. 如果问题包含"调用"、"caller"、"callee"、"流程"且未扩展调用链，sufficient 必须是 FALSE。
3. 禁止以"信息可能已足够"作为停止理由，除非函数数 ≥ 5 且 top_score ≥ 0.6。

=== 常见错误 — 禁止出现 ===
- "现有函数覆盖了核心概念"（当函数 ≤ 3 时这是错误的）
- "基于函数名可以推断..."（必须看代码或注解才能下结论）

"""

prompt = CRITICAL_PREFIX + f"""{context}
...原有prompt内容..."""
```

**预期效果：** 直接针对 v7 实验中 37-48% 错误题检索不足的问题，用规则强制模型继续检索。

### 改动 B：给 `generate_answer` 加上 "证据审计" 要求

在答案生成 prompt 中加入：

```python
EVIDENCE_PREFIX = """在给出最终答案前，你必须先在 <analysis> 中检查：
1. 你计划陈述的每个要点是否有检索证据直接支持？
2. 如果没有直接证据，必须标注为 [UNVERIFIED] 并在最终答案中省略。
3. 禁止基于函数名推测实现逻辑。

最终答案必须基于证据，禁止脑补。

"""
```

**预期效果：** 减少"脑补"导致的错误，特别是针对"apertus 包含哪些子模块"这类信息不足时模型会编造详细结构的案例。

### 改动 C：优化 Judge Prompt 的对抗性

将现有 `BINARY_JUDGE_PROMPT` 的判断标准从：
```
- 正确 (CORRECT): 生成答案准确回答了问题，核心信息正确，无重大错误
```

改为：
```
- 正确 (CORRECT): 生成答案的每个关键论断都有确凿证据支持，没有事实错误、没有推测冒充事实、没有归因错误
- 错误 (INCORRECT): 生成答案包含任何事实错误、未经证实的推测、或将无关信息强行关联为答案
```

**预期效果：** 让 Judge 更严格，减少因"方向差不多"而误判为 CORRECT 的情况。

---

## 四、中期工程化目标（1-2周）

1. **建立 `prompts/` 目录**
   - `sections.py`：存放所有可复用的 prompt section（identity, constraints, examples, guidelines）
   - `react_decide.py`：ReAct 决策 prompt
   - `answer_generation.py`：答案生成 prompt
   - `judge.py`：LLM Judge prompt

2. **引入 A/B 测试框架**
   - 每个 prompt 改动都要能标注版本号（如 `react_decide_v2`）
   - 跑 360 题 benchmark 时记录使用的 prompt 版本
   - 便于对比哪个 prompt 版本提升了哪些类型的问题

3. **建立 Prompt 版本日志**
   - 记录每次改动的假设（如"加入 CRITICAL 约束以解决过早停止问题"）
   - 记录实验结果是否验证了假设
   - 避免反复在同一个问题上调来调去

---

## 五、总结

Claude Code 的提示词给我们的最大启示是：**提示词不是"写一段好听的描述"，而是"设计一个精确的行为控制系统"**。

它通过以下手段实现了这一点：
1. **具体化**：每个场景精确映射到工具，没有模糊地带
2. **强约束**：用大写、否定句、CRITICAL 标签划定不可逾越的红线
3. **负面示例**：预判模型的作弊借口并提前封死
4. **结构化**：用 XML 标签引导模型的思维链，同时保留过滤草稿的能力
5. **模块化**：把 prompt 拆成可独立迭代的 section，支持工程化管理

Code_Graph 当前最大的 prompt 问题不是"写得不好"，而是**约束力不够**——模型有太多的自由裁量空间来"过早停止"和"脑补"。借鉴 Claude Code 的强硬约束风格，是在不增加任何工具或架构改动的情况下，**最有可能快速提升准确率**的方向。
