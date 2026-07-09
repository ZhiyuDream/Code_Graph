# Investigation & ReAct Prompts Review

This document collects all prompts used by the various investigation / ReAct / selection agents for review.
Generated: 2026-06-09

## react_decide.txt

```text
你是一个决策引擎。你的唯一任务是根据当前已收集的信息，选择下一步检索行动。不要分析代码，不要解释问题，只输出一个 JSON 决策。

【可用工具】
{actions}

【决策参考】
- 如果问题是关于"调用链/依赖关系/谁调用了谁"，expand_callers 或 expand_callees 通常是关键
- 如果当前信息明显不足（<3个相关函数），考虑 grep_search 或 semantic_search 重新检索
- 如果已收集到5+个相关函数且对其中多个进行了扩展，可以考虑 sufficient
- 不要重复扩展已标记[已扩展]的函数

【当前状态】
问题: {question}

已收集函数 ({function_count}个):
{function_list}

相关Issue ({issue_count}个):
{issue_list}

已扩展调用链 ({chain_count}条):
{chain_list}

已用过的搜索关键词: {used_queries}
（选 grep_search / semantic_search 时，请使用**不同**的关键词）

【输出格式】
返回严格的 JSON 对象，只输出 JSON，不要任何其他文字：
{{
    "thought": "选择该行动的理由（20字以内）",
    "sufficient": true_or_false,
    "action": "{action_choices}",
    "target": "目标函数名（如需）",
    "query": "搜索关键词（如需）"
}}

【示例】
搜索: {{"thought": "需要搜索backend代码", "sufficient": false, "action": "grep_search", "query": "ggml_backend_sched"}}
扩展调用者: {{"thought": "需要查看调用者", "sufficient": false, "action": "expand_callers", "target": "ggml_backend_sched_graph_compute"}}
结束: {{"thought": "信息已足够", "sufficient": true, "action": "sufficient"}}

只输出JSON:
```

## react_decide_v2.txt

```text
问题: {question}

【已收集函数】(共{function_count}个，按相似度排序):
{function_list}

【相关Issue】(共{issue_count}个):
{issue_list}

【已扩展调用链】(共{chain_count}条):
{chain_list}

---

你是代码检索专家。请根据当前已收集的信息和问题类型，选择最合适的下一步行动。

【可用工具】
{actions}

【决策参考】
- 如果问题是关于"调用链/依赖关系/谁调用了谁"，expand_callers 或 expand_callees 通常是关键，不要只盯着同文件扩展
- 如果问题是关于"模块组织结构/同文件内的协作"，expand_same_file 很有用
- 如果当前信息明显不足（<3个相关函数 或 高相关函数代码过短），可以考虑重新检索
- 如果已收集到5+个相关函数且对其中多个进行了扩展，可以考虑 sufficient
- 不要重复扩展已标记[已扩展]的函数

返回JSON:
{{
    "thought": "分析现有信息和下一步计划（30字以内）",
    "sufficient": true_or_false,
    "action": "{action_choices}",
    "target": "目标函数名（如需）",
    "query": "搜索关键词（如需）"
}}

只输出JSON:
```

## react_decide_flexible.txt

```text
问题: {question}

【已收集函数】(共{function_count}个，按相似度排序):
{function_list}

【相关Issue】(共{issue_count}个):
{issue_list}

【已扩展调用链】(共{chain_count}条):
{chain_list}

---

你是代码检索专家。请根据当前已收集的信息和问题类型，选择最合适的下一步行动。

【可用工具】
{actions}

【决策参考】
- 如果当前信息明显不足（<3个相关函数 或 高相关函数代码过短），可以考虑重新检索
- 如果你不确定该用什么精确关键词搜索，优先使用 semantic_search（语义搜索）而不是 grep_search
- semantic_search 时请用英文查询，尽量包含代码库中可能出现的英文函数名、模块名或技术术语（代码库是英文的，中文查询召回率低）
- 如果是调用链/依赖关系问题，expand_callers 或 expand_callees 通常有帮助
- 如果是模块组织结构问题，read_class 可以获取目标类/文件的完整上下文
- 如果已收集到5+个相关函数且对其中多个进行了扩展，可以考虑 sufficient
- 不要重复扩展已标记[已扩展]的函数

返回JSON:
{{
    "thought": "分析现有信息和下一步计划（30字以内）",
    "sufficient": false,
    "action": "{action_choices}",
    "target": "目标函数名（如需）"
}}

只输出JSON:
```

## react_decide_gpt54_style.txt

```text
问题: {question}

【已收集函数】(共{function_count}个，按相似度排序):
{function_list}

【相关Issue】(共{issue_count}个):
{issue_list}

【已扩展调用链】(共{chain_count}条):
{chain_list}

---

你是代码检索专家。请根据当前已收集的信息和问题类型，选择最合适的下一步行动。

【可用工具】
{actions}

【决策原则 - GPT-5.4 风格】
1. **优先策略（按优先级排序）**：
   - **expand_same_file** (最优先)：当需要理解某个函数所在的完整文件上下文、模块组织、或同文件内其他相关定义时，优先扩展同一文件内的函数。这能获取最完整的代码上下文。
   - **expand_callers** (次优先)：当问题是关于"谁调用了这个函数"、"依赖关系"、"模块间协作"时，向上追溯调用者。
   - **expand_callees** (最后考虑)：只有当需要深入某个函数的内部实现细节，且同文件和调用者信息已不足时，才向下扩展被调用者。

2. **必须继续扩展的情况（不要选sufficient）**：
   - 当前收集的函数数 <= 3 个
   - 高相关函数（相似度>0.7）的代码明显不足以回答问题
   - 问题涉及模块/架构设计但只收集到零散函数，未形成完整图景
   - 尚未对 top 3 高相关函数进行任何扩展

3. **可以停止的情况**：
   - 已收集至少 5 个相关函数且对其中 2+ 个进行了扩展
   - 同文件扩展已获得足够上下文
   - 调用链扩展已覆盖问题的核心范围

4. **重要约束**：
   - 不要重复扩展已标记[已扩展]的函数
   - action 必须是可用工具列表中的确切名称
   - target 必须是已收集函数列表中的确切函数名

返回JSON:
{{
    "thought": "分析现有信息和下一步计划（30字以内）",
    "sufficient": false,
    "action": "{action_choices}",
    "target": "目标函数名"
}}

只输出JSON:
```

## react_select_next_symbol.txt

```text
You are a senior code auditor investigating a repository to answer a question.

Question:
{question}

Candidate functions not yet investigated:
{candidates}

Investigated functions:
{visited}

Your task:
1. Choose ONE candidate function to investigate next.
2. After reading its code, decide if it is the EVIDENCE ANCHOR for the question.

An "evidence anchor" is the function that DIRECTLY IMPLEMENTS or CONTROLS the behavior asked about, and contains the key evidence needed to answer the question.

Rules:
- If the investigated function is the evidence anchor, finish and return it.
- If not, record negative evidence (why it is NOT the anchor) and choose another candidate.
- You have at most {max_steps} steps total. Current step: {current_step}

Output valid JSON:
{{
  "thought": "your reasoning",
  "action": "investigate|finish",
  "target": "function_name",
  "is_evidence_anchor": true or false,
  "reasoning": "why this function is or is not the evidence anchor"
}}

If action is "finish", target should be the function you believe is the evidence anchor (your best guess so far).
```

## dynamic_select_next.txt

```text
你正在调查代码仓库以回答问题。

【原始问题】
{question}

【当前搜索目标】
{next_search_target}

【已访问文件】
{visited_files}

【候选文件队列（frontier）】
{frontier_files}

【最新证据】
{latest_evidence}

---

【要求】
1. 总结你刚从文件中读到的新证据（new_evidence）。只陈述事实，不要猜测。
2. 说明这个证据如何改变了你下一步的搜索方向（decision_impact）。例如：
   - "发现 A 调用了 B，因此搜索方向从 A 转向 B"
   - "发现 B 的实现与调用方假设不一致，因此需要查看所有调用 B 的地方"
   - "发现没有新信息，因此返回去检查其他候选文件"
3. 明确下一个搜索目标（next_search_target），可以是一个函数名、一个模块、或一个调用路径。
4. 从候选队列中选择一个最相关的文件作为下一步动作（next_action），格式为 `read_file:path/to/file.cpp`。
5. 避免选择与已访问文件同类型且无新信息的冗余文件。

【输出格式】（必须是合法 JSON，不要有任何额外文字）
{{
  "new_evidence": "从刚读到的文件中发现的具体事实",
  "decision_impact": "这个事实如何改变了我的搜索方向",
  "next_search_target": "下一个要调查的目标（函数/模块/路径）",
  "next_action": "read_file:path/to/file.cpp"
}}
```

## symbol_follow_select_next.txt

```text
你正在调查代码仓库以回答问题。请基于当前证据，从以下候选符号中选择 1-2 个最值得继续追的具体符号。

【原始问题】
{question}

【当前候选符号】
{current_symbols}

【最新证据】
{latest_evidence}

---

【要求】
1. 只选择真实存在的函数名、类名或全局变量名。
2. 优先选择与问题核心直接相关的符号，而不是通用辅助函数。
3. 优先选择可能在多个文件中出现、能带你找到调用链的符号。
4. 解释为什么选择这些符号（decision_impact）。

【输出格式】（必须是合法 JSON）
{{
  "symbols": ["symbol1"],
  "decision_impact": "基于最新证据，我认为需要追这个符号，因为..."
}}
```

## worklist_select_next.txt

```text
你是一位代码审计专家。你已经初步扫描了一批候选文件，现在需要根据问题决定下一步行动，以找到所有可能相关的证据文件。

【原始问题】
{question}

【已扫描文件摘要】（每个文件前100行）
{file_summaries}

【已访问文件列表】
{visited_files}

【已扩展的符号】
{expanded_symbols}

---

【可用工具】
1. **read_full(file_path)** — 读取某个已扫描文件的完整内容（当你认为该文件高度相关、需要深入时）
2. **find_callers(symbol)** — 找到调用该函数/符号的所有位置
3. **find_callees(symbol)** — 找到该函数/符号调用的所有函数
4. **search_symbol(symbol)** — 在仓库中搜索某个符号（函数/变量/类）
5. **search_files(query)** — 用英文查询检索相关文件（当你需要扩展到新模块时）
6. **finish(reason)** — 认为已找到足够相关文件，结束调查

---

【决策规则】
- 优先通过 search_files 扩展问题中提到的核心模块/概念（如 sycl、context、backend、rpc 等）
- 当已扫描文件中出现关键函数/符号时，使用 find_callers/find_callees 追踪调用链
- 如果某个已扫描文件明显包含核心实现，使用 read_full 深入阅读
- 每次只选择一个最可能带来新相关文件的工具
- 当前第 {current_step} 步，最多 {max_steps} 步

【输出格式】（必须是合法 JSON，不要有任何额外文字）
{{
  "thought": "你的分析：已找到什么、还缺少什么、为什么选这个行动",
  "action": "read_full|find_callers|find_callees|search_symbol|search_files|finish",
  "action_input": {{"file_path": "..."}} 或 {{"symbol": "..."}} 或 {{"query": "..."}} 或 {{"reason": "..."}},
  "expected_gain": "这个行动预计能发现什么相关文件或证据"
}}
```

## init_suspicion.txt

```text
你是一位代码审计专家。给定审计问题和入口文件内容，请确定初始调查方向。

【问题】
{question}

【入口文件】
{file_path}

【文件中检测到的符号】
{candidate_symbols}

【文件内容摘要】
{content}

【要求】
1. 基于问题，列出 2-5 个最值得调查的符号。这些符号必须与问题的核心关切直接相关。
2. 优先从【文件中检测到的符号】中选择；如果问题明显需要某个符号但不在列表中，可以补充，但请谨慎。
3. 禁止选择通用辅助函数、宏、第三方库符号、纯数学/内存工具函数，除非它们与问题直接相关。
4. 返回的符号必须是真实存在的函数名/类名/全局变量名。
5. 用一句话描述当前调查焦点（current_question）。
6. suspicious_files 应该是根据当前证据最值得下一步调查的 2-4 个文件路径（相对路径，如 `ggml/src/...` 或 `src/...`）。

【输出格式】（必须是合法 JSON，不要有任何额外文字）
{{
  "current_question": "当前需要回答的子问题",
  "suspicious_symbols": [
    "symbol1",
    "symbol2"
  ],
  "suspicious_files": [
    "path/to/file1.cpp",
    "path/to/file2.cpp"
  ]
}}
```

## extract_evidence.txt

```text
你是一位代码审计专家。请从当前阅读的代码文件中提取与问题相关的关键证据，并指出这些证据让你对哪些符号/文件产生怀疑。

【问题】
{question}

【当前文件】
{file_path}

【当前搜索焦点】
{current_question}

【文件中检测到的候选符号】
{candidate_symbols}

【文件内容】
{content}

【要求】
1. 提取与问题直接相关的 2-4 个关键事实（fact）。
2. 基于这些事实，用一句话更新你的调查假设（new_hypothesis）。
3. **suspicious_symbols 必须且只能从上面的【候选符号】中选取**，数量严格限制在 2-5 个，按与问题的相关程度排序。
4. 禁止返回未在候选符号中出现的函数名、宏或变量名。
5. suspicious_files 应该是根据当前证据最值得下一步调查的 2-4 个文件路径（相对路径，如 `ggml/src/...` 或 `src/...`）。

【输出格式】（必须是合法 JSON，不要有任何额外文字）
{{
  "key_facts": [
    "事实1",
    "事实2"
  ],
  "new_hypothesis": "基于这些事实，我对问题的新判断",
  "suspicious_symbols": [
    "symbol1",
    "symbol2"
  ],
  "suspicious_files": [
    "path/to/file1.cpp",
    "path/to/file2.cpp"
  ]
}}
```

## generate_answer.txt

```text
你是一位代码审计专家。请基于调查过程中收集到的证据，回答原始问题。

【问题】
{question}

【调查过程中访问的文件及关键证据】
{evidence_log}

【已访问文件内容摘要】
{files_summary}

---

要求：
1. 直接回答原始问题
2. 引用你使用的代码证据（格式：`file.cpp:start-end`）
3. **答案末尾必须包含固定格式的"引用文件清单"**：

## 引用文件清单
- `file1.cpp`
- `file2.cpp`

4. 如果不确定，明确说明"无法确认"
```

## query_analysis.txt

```text
你是一个代码仓库查询分析器。你的任务是分析用户的代码查询，判断查询类型并提取关键信号。

## 查询类型定义

1. **symbol_centric**: 查询针对具体的函数、类、变量或文件的实现细节。
   - 特征：包含明确的函数名、类名或文件名
   - 示例：
     - "How does llama_decode work?"
     - "AI 帮我生成了 `ggml_sycl_set_device`，帮我审一下"
     - "分析 trim_whitespace 的实现"
     - "src/llama.cpp 里的 decode 逻辑"

2. **component_centric**: 查询针对某个组件、模块或子系统的工作原理。
   - 特征：包含组件名（backend, scheduler, tokenizer, sampler, RPC, KV cache 等），但没有具体到函数
   - 示例：
     - "backend scheduler 怎么工作"
     - "CUDA memory allocation 机制"
     - "tokenizer 的 vocabulary 管理"
     - "RPC 通信流程"

3. **architecture_centric**: 查询针对整体架构、流程或高层设计。
   - 特征：询问"流程"、"架构"、"整体"、"怎么工作的"、"设计原理"
   - 示例：
     - "llama.cpp 的推理流程"
     - "模型加载过程"
     - "server 架构"
     - "这个仓库的组织结构"

## 输出格式

返回 JSON：
{{
  "query_type": "symbol_centric|component_centric|architecture_centric|unknown",
  "symbols": ["提取的函数名/类名/文件名"],
  "components": ["提取的组件名/模块名"],
  "confidence": 0.0-1.0,
  "reasoning": "简要说明判断理由（中文）"
}}

## 规则

1. **symbols 中的项应该是代码中可能出现的标识符**（函数名、类名、文件名），不要包含普通英文单词
2. **函数名可能出现在**：反引号 `` ` `` 中、代码块中、或普通文本中，都要提取
3. **优先级**：如果一个查询同时包含 symbol 和 component，优先判断为 symbol_centric（因为更具体）
4. **confidence**：反映你对判断的确定程度。有明确函数名 → 高置信度；只有模糊描述 → 低置信度
5. **unknown**：当完全无法判断时使用
6. **不要编造**：如果查询中确实没有函数名，symbols 应为空列表

## 示例

Query: "AI 帮我生成了 `ggml_sycl_set_device` 这段实现，帮我审一下它的返回值、参数和副作用是否符合现有调用方的使用假设？"
{{
  "query_type": "symbol_centric",
  "symbols": ["ggml_sycl_set_device"],
  "components": [],
  "confidence": 0.98,
  "reasoning": "查询明确提到了函数名 ggml_sycl_set_device，并要求审计其实现细节"
}}

Query: "backend scheduler 是怎么调度计算图的？"
{{
  "query_type": "component_centric",
  "symbols": [],
  "components": ["backend", "scheduler"],
  "confidence": 0.85,
  "reasoning": "查询提到了 backend scheduler 组件，但没有具体到某个函数"
}}

Query: "llama.cpp 的整体推理流程是什么？"
{{
  "query_type": "architecture_centric",
  "symbols": [],
  "components": ["inference"],
  "confidence": 0.90,
  "reasoning": "查询询问整体推理流程，属于架构层面"
}}

Query: "How does llama_decode work?"
{{
  "query_type": "symbol_centric",
  "symbols": ["llama_decode"],
  "components": [],
  "confidence": 0.95,
  "reasoning": "查询明确提到了函数名 llama_decode"
}}

---

现在请分析以下查询：

Query: "{question}"
```

## expansion_decide.txt

```text
你是一个选择器。你的唯一任务是从函数列表中勾选与问题相关的函数名，不做任何分析，不做任何解释。

返回严格的 JSON 对象，只输出 JSON，不要任何其他文字：
{{
    "relevant_functions": ["函数名1", "函数名2"],
    "reason": "20字以内"
}}

【用户问题】
{question}

【候选函数】（只选名称，不分析代码）：
{function_list}

【规则】
1. 必须从上方列表中选，不要编造
2. 最多选 10 个，优先核心函数
3. 没有相关的就返回空数组 []

【示例】
多选: {{"relevant_functions": ["f1","f2"], "reason": "与backend调度直接相关"}}
单选: {{"relevant_functions": ["f1"], "reason": "问题核心函数"}}
无相关: {{"relevant_functions": [], "reason": "列表中无直接相关函数"}}

只输出JSON:
```

## select_best_entry.txt

```text
你正在调查代码库以回答下面的问题。以下文件是通过语义检索得到的候选入口文件。

【问题】
{question}

【候选入口文件】
{candidates}

【要求】
请从候选文件中选择最可能包含问题核心实现/定义、最适合作为调查起点的文件。
- 优先选择包含函数/类定义的文件（如 .cpp 实现文件、.h/.hpp 头文件），而非测试文件或示例文件。
- 如果问题涉及某个具体函数或类，优先选择该函数/类的定义文件。
- 只返回文件路径，不要任何解释。

最佳入口文件：
```

## select_multiple_symbols.txt

```text
You are a senior code auditor. A user asks a question about recent code changes in a repository.

Question:
{question}

We have retrieved the following function candidates using semantic search. Each candidate has an ID, file path, and function name.

{file_summaries}

Your task: Select ALL candidate functions that might be relevant to investigate this question. Do NOT limit yourself to one. A single starting point is often insufficient because the question may involve multiple related functions (callers, callees, or alternative implementations).

Important:
- Select between 1 and 5 candidate IDs.
- Return ONLY a JSON array of integers, e.g., [3, 7, 1].
- Order matters: put the most promising candidates first.
- Do not add explanations, function names, or markdown formatting outside the JSON array.
- If none of the candidates seem relevant, return an empty array [].
```

## select_top3.txt

```text
You are a senior code auditor. A user asks a question about a code repository.

Question:
{question}

Candidate functions (with signatures only):
{candidates}

Select the TOP {k} functions most likely to be the EVIDENCE ANCHOR — the function that directly implements or controls the behavior asked about.

Return a JSON array of exactly {k} candidate IDs, e.g., [1, 5, 9].
```

## stage1_select_entry.txt

```text
You are a senior code auditor. A user asks a question about recent code changes in a repository.

Question:
{question}

We have retrieved the following candidate files using semantic search. Each file is shown with an ID, its path, and a snippet of its content.

{file_summaries}

Your task: From these candidates, select the {select_top} file(s) that would be the best starting point to investigate the question.

Important:
- You MUST select from the candidates above. Even if none is perfect, choose the closest one.
- Do NOT return "NONE".
- Return ONLY the candidate ID(s), one per line (e.g., "1" or "3").
- If selecting multiple, order them from most to least relevant.
- Do not add explanations or file paths.
```

## stage1_select_function.txt

```text
You are a senior code auditor. A user asks a question about recent code changes in a repository.

Question:
{question}

We have retrieved the following function candidates using semantic search. Each candidate has an ID, file path, and function information.

{file_summaries}

Your task: Select the ONE candidate function that would be the best starting point to investigate the question.

Important:
- You MUST select one candidate from the list above.
- Return ONLY the candidate ID (a single number, e.g., "3").
- Do not add explanations, function names, or markdown formatting.
- Even if none of the candidates perfectly match, choose the closest one.
```

## stage1_select_function_anchor.txt

```text
You are a senior code auditor. A user asks a question about recent code changes in a repository.

Question:
{question}

We have retrieved the following function candidates using semantic search. Each candidate has an ID, file path, function signature, and function name.

{file_summaries}

Your task: Select the ONE candidate function that serves as the ROOT CAUSE or IMPLEMENTATION ANCHOR for the behavior asked about.

Important:
- This should be the function where the core logic is directly implemented, not a caller, wrapper, or utility.
- If the question asks about a specific behavior, choose the function that PRODUCES or CONTROLS that behavior.
- You MUST select one candidate from the list above.
- Return ONLY the candidate ID (a single number, e.g., "3").
- Do not add explanations, function names, or markdown formatting.
- Even if none of the candidates perfectly match, choose the closest one.
```

## stage1_select_function_evidence.txt

```text
You are a senior code auditor. A user asks a question about recent code changes in a repository.

Question:
{question}

We have retrieved the following function candidates using semantic search. Each candidate has an ID, file path, function signature, and function name.

{file_summaries}

Your task: Select the ONE candidate function that is MOST LIKELY TO CONTAIN THE EVIDENCE needed to answer the question.

Important:
- Prefer the function that IMPLEMENTS or ANCHORS the behavior, not one that merely describes or wraps it.
- Avoid generic API entry points, helper functions, or thin wrappers unless they themselves contain the key evidence.
- You MUST select one candidate from the list above.
- Return ONLY the candidate ID (a single number, e.g., "3").
- Do not add explanations, function names, or markdown formatting.
- Even if none of the candidates perfectly match, choose the closest one.
```

## stage1_select_function_signature.txt

```text
You are a senior code auditor. A user asks a question about recent code changes in a repository.

Question:
{question}

We have retrieved the following function candidates using semantic search. Each candidate has an ID, file path, function signature, and function name.

{file_summaries}

Your task: Select the ONE candidate function that would be the best starting point to investigate the question.

Important:
- You MUST select one candidate from the list above.
- Return ONLY the candidate ID (a single number, e.g., "3").
- Do not add explanations, function names, or markdown formatting.
- Even if none of the candidates perfectly match, choose the closest one.
```

## stage1_select_entry_ablation.txt

```text
You are a senior code auditor. A user asks a question about recent code changes in a repository.

Question:
{question}

We have retrieved the following candidate files using semantic search. Each candidate has an ID.

{file_summaries}

Your task: Select the ONE candidate file that would be the best starting point to investigate the question.

Important:
- You MUST select one candidate from the list above.
- Return ONLY the candidate ID (a single number, e.g., "3").
- Do not add explanations, file paths, or markdown formatting.
- Even if none of the candidates perfectly match, choose the closest one.
```
