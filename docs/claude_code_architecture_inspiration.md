# Claude Code 架构调研：对代码问答系统的启发

**调研目标**：研究 Claude Code 源码/架构，提炼对 Code_Graph 代码问答系统的设计启发
**数据来源**：
- 官方文档：`code.claude.com/docs`
- Agent SDK 文档
- **实际源码**：`/tmp/cc/extracted_source/parent_dir/src/`（从 cc.zip 解压）
  - `query.ts`（1730 行，主体循环）
  - `memdir.ts`（507 行，记忆系统）
  - `findRelevantMemories.ts`（141 行，记忆检索）
  - `memoryScan.ts`（95 行，记忆文件扫描）
  - `autoCompact.ts`（352 行，自动压缩）
  - `sideQuery.ts`（222 行，侧边查询工具）

**说明**：Claude Code 的核心实现是闭源的（`anthropics/claude-code` 仓库只是安装/分发插件）。以下结论来自实际源码阅读 + 官方文档的公开架构描述。

---

## 1. 核心发现：Claude Code 没有预建索引

这是最重要的一点：**Claude Code 完全不依赖预建的 embedding 索引、AST 解析或图数据库**。

它回答代码问题的机制是：

1. 用户提问（"认证逻辑在哪里？"）
2. LLM 主动发出搜索工具调用（`Grep` 搜索 `authenticate`，`Glob` 找 `**/*auth*`）
3. `Read` 读取相关文件
4. 如果装了 LSP 插件，还能用 `LSP` 工具做定义跳转、引用查找、调用链追踪
5. LLM 在上下文中综合所有读取的文件内容给出答案

**本质是"按需读取 RAG"而非"索引检索 RAG"**——检索策略由 LLM 的推理引导，而不是由 embedding 相似度决定。

---

## 2. 架构概览

Claude Code 是一个**基于语言模型的 agentic harness**，三层结构：

```
Layer 1: Claude 语言模型（推理引擎）
Layer 2: Agent Loop（执行循环：收集上下文 → 执行动作 → 验证结果）
Layer 3: Tool Layer（工具层：Bash/Read/Edit/Grep/LSP/Agent...）
```

关键机制：
- 每个循环周期称为一个 **turn**：Claude 输出 tool calls → harness 执行 → 结果反馈 → 重复
- Tool call 的执行结果作为 `UserMessage` 回传给 Claude，累积到对话历史中
- **Read-only 工具（Read/Glob/Grep）可以并发执行**，Edit/Write/Bash 等写操作串行执行

---

## 3. 代码理解机制

### 3.1 LLM 本身（主要方式）

Claude 模型依靠预训练知识理解代码——能直接阅读并理解任意语言的代码，理解 API 模式、数据结构、算法逻辑。不需要预先解析 AST 或建索引。

### 3.2 LSP 集成（结构化智能）

Claude Code 支持通过插件安装 Language Server Protocol（clangd/pyright/gopls/rust-analyzer 等）。启用后获得：

| LSP 能力 | 说明 |
|---------|------|
| Jump to definition | 跳转到符号定义位置 |
| Find all references | 查找符号的所有引用 |
| Type info at position | 光标位置的类型信息 |
| List symbols in file | 列出文件中的所有符号 |
| Call hierarchy | 函数调用层级追踪 |
| Diagnostics | 每次编辑后自动报错（类型错误、缺失 import 等） |

**关键**：LSP 提供的是实时、准确的代码结构信息，不依赖 embedding 或预解析。

### 3.3 按需文件导航

Claude 通过工具调用主动探索代码库，不依赖预建索引：
- `Glob`：按模式找文件（`**/*.ts`, `src/**/*.py`）
- `Grep`：正则搜索文件内容
- `Read`：读取指定文件（或指定行范围）

LLM 根据任务推理决定探索路径，而不是盲目穷举。

---

## 4. 上下文管理

### 4.1 会话启动时加载的内容（按顺序）

1. System prompt（~4,200 tokens）
2. `MEMORY.md` 前 200 行 / 25KB（项目记忆）
3. 环境信息（~280 tokens）
4. `CLAUDE.md` 文件（项目根目录、用户级 `~/.claude/CLAUDE.md`、组织级）
5. Tool definitions（所有工具定义）
6. Skill 描述（仅摘要；实际内容在调用时加载）

### 4.2 上下文累积与压缩（实际源码）

`query.ts` 是主体循环（1730 行），核心是 `async function* query()` 异步生成器模式：

```typescript
// 关键状态
interface QueryState {
  messages: Message[]
  autoCompactTracking: AutoCompactTrackingState
  maxOutputTokensRecoveryCount: number
  turnCount: number
  pendingToolUseSummary: ToolUseSummaryMessage | undefined
}
```

压缩策略按触发顺序分多级（`autoCompact.ts` 源码）：

```typescript
// 预留 20K tokens 用于压缩输出
const MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20_000
// 触发阈值：effectiveContextWindow - 13,000
const AUTOCOMPACT_BUFFER_TOKENS = 13_000
// 连续失败熔断：3 次后停止
const MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3
```

**关键机制**：
- 上下文窗口在会话内**不重置**，持续累积
- 当窗口接近上限时自动压缩：总结旧对话历史，保留近期交互和关键决策
- `CLAUDE.md` 在压缩后**重新从磁盘读取注入**（不依赖对话历史）
- 支持手动压缩：`/compact`（可指定重点：`/compact focus on API changes`）
- **熔断机制**：连续 3 次压缩失败后停止尝试（避免无限重试浪费 API 调用）

### 4.3 子 Agent 隔离

复杂任务可 spawn 子 Agent：
- 每个子 Agent 有**独立的新鲜上下文窗口**
- 不继承父 agent 的对话历史
- 只返回**最终结果摘要**给父 agent，不返回完整 transcript

这解决了长时间会话中上下文膨胀的问题。

### 4.4 ToolSearch：按需工具加载（实际源码）

`sideQuery.ts`（222 行）是侧边查询工具，用于主循环之外的独立 LLM 调用：

```typescript
export async function sideQuery(opts: SideQueryOptions): Promise<BetaMessage>
```

当可用工具数量很大（50+）时，将所有工具定义全部加载会消耗 10-20K tokens。Claude Code 的解决方式：
- 工具定义默认**不加载**到上下文
- Claude 收到可用工具的摘要描述
- 需要时用 `ToolSearch` 工具语义搜索最相关的 3-5 个工具
- 搜索到的工具定义才加载进上下文

这个模式本质上是**对工具定义的 RAG**，对代码文档/代码片段同样适用：不需要一次性把所有代码 chunk 都加载进上下文，用语义搜索按需加载最相关的部分。

---

## 5. 记忆系统：MEMORY.md（实际源码分析）

### 5.1 核心常量

`memdir.ts` 第 34-38 行：

```typescript
export const ENTRYPOINT_NAME = 'MEMORY.md'
export const MAX_ENTRYPOINT_LINES = 200
// ~125 chars/line at 200 lines。p97 实测；捕获超出行数限制的长行（p100 观测到：197KB/200行内）
export const MAX_ENTRYPOINT_BYTES = 25_000
```

### 5.2 四种 Memory Type（eval 验证）

`memoryTypes.ts` 定义了 4 种类型，每种都有 **when_to_save**、**how_to_use**、**body_structure** 和 **examples**：

| Type | Description |
|------|-------------|
| `user` | 用户角色、目标、偏好。帮助定制回答方式（如 senior eng vs.新手）。|
| `feedback` | 用户给出的修正或确认。包括 *why* 和 *how to apply*，用于同类场景的判断依据。|
| `project` | 项目状态、目标、决策及动机（deadline、stakeholder ask）。**Why** 帮助判断记忆是否还适用。|
| `reference` | 外部系统指针（Linear 项目、Grafana dashboard）。知道去哪里找信息。|

### 5.3 WHAT_NOT_TO_SAVE（eval 验证）

`memoryTypes.ts` 第 183-195 行的 `WHAT_NOT_TO_SAVE_SECTION`：

> "Code patterns, conventions, architecture, file paths, or project structure — these can be derived from the current project state."
> "Git history, recent changes, or who-changed-what — `git log` / `git blame` are authoritative."
> "Debugging solutions or fix recipes — the fix is in the code; the commit message has the context."
> "Anything already documented in CLAUDE.md files."
> "Ephemeral task details: in-progress work, temporary state, current conversation context."

关键设计：**即使用户明确要求保存某些内容（如"帮我记住这个 PR 列表"），也会引导用户问"有什么 surprising 的？"**——只有 surprising/non-obvious 的部分才值得保存。

### 5.4 TRUSTING_RECALL（最精妙）

`memoryTypes.ts` 第 240-256 行的 `TRUSTING_RECALL_SECTION` 针对一个实际失败模式：memory 说 X 存在，但 X 已经被重命名/删除了。

> "Before recommending from memory: A memory that names a specific function, file, or flag is a claim that it existed *when the memory was written*. It may have been renamed, removed, or never merged. Before recommending it:"
> - If the memory names a file path: **check the file exists.**
> - If the memory names a function or flag: **grep for it.**
> - If the user is about to act on your recommendation: **verify first.**

注释说明：这段是通过 A/B test 验证的——header 用 "Before recommending from memory"（动作触发器）比用 "Trusting what you recall"（抽象概念）效果好得多（3/3 vs 0/3）。

### 5.5 两档记忆架构

`memdir.ts` 中 `buildMemoryLines()` 定义了记忆的 two-step 保存流程：

**Step 1**：写独立文件（`user_role.md`、`feedback_testing.md` 等），使用 frontmatter 格式：
```markdown
---
name: {{memory name}}
description: {{one-line description}}
type: {{user, feedback, project, reference}}
---

{{memory content}}
```

**Step 2**：在 `MEMORY.md` 中加一条指针（index entry）：
```markdown
- [Title](file.md) — one-line hook
```

### 5.3 按需加载：findRelevantMemories（实际源码）

`findRelevantMemories.ts`（141 行）的核心逻辑：

```typescript
// 1. 扫描所有 memory 文件的 frontmatter
const memories = await scanMemoryFiles(memoryDir, signal)

// 2. 用单独的 Sonnet sideQuery 从 manifest 中选最相关的 5 个
const selectedFilenames = await selectRelevantMemories(query, memories, signal, recentTools)

// 3. 返回绝对路径 + mtime，供调用方决定是否重新读取
return selected.map(m => ({ path: m.filePath, mtimeMs: m.mtimeMs }))
```

**选择 prompt**（`findRelevantMemories.ts` 第 18-24 行）：

> "You are selecting memories that will be useful to Claude Code as it processes a user's query... Return a list of filenames for the memories that will clearly be useful... (up to 5). Only include memories that you are certain will be helpful... If you are unsure if a memory will be useful, then do not include it."

**关键设计**：`alreadySurfaced` 参数防止重复选择同一文件，使选择器把 5 个 slot 都用在新的候选上。

### 5.4 memoryScan：轻量索引

`memoryScan.ts`（95 行）实现了 memory 文件的轻量扫描：

```typescript
// 扫描所有 .md 文件（排除 MEMORY.md 自身）
// 只读前 30 行 frontmatter
const { frontmatter } = parseFrontmatter(content, filePath)
// 返回: filename, filePath, mtimeMs, description, type
export type MemoryHeader = {
  filename: string
  filePath: string
  mtimeMs: number
  description: string | null
  type: MemoryType | undefined
}
```

**排序**：按 mtime 新到旧，最多 200 个文件。

### 5.5 映射到代码问答

这是一个**经过验证的两档记忆模式**：
- 快速摘要层（MEMORY.md index）—— 提供全局概览，最多 200 行/25KB
- 详细 topic 层（按需加载）—— 通过 Sonnet sideQuery 选择最相关的 5 个文件

Claude Code 的 `findRelevantMemories` 用**语义选择**而不是简单的关键词匹配。这对 Code_Graph 的 Issue 检索有直接启发：不需要每次把所有 Issue body 都加载进上下文，用一个轻量索引做预筛选，每次只把最相关的 3-5 个 Issue 的完整内容加载进来。

---

## 6. Claude Code 对代码问答系统的启发

### 启发 1：LLM 引导的主动检索优于纯 embedding 检索

Claude Code 的"按需 RAG"模式证明了：当 LLM 能主动搜索时，embedding 索引不是必需品。LLM 的推理能力可以规划检索策略（先找哪个文件、再找哪个函数），特别适合复杂多跳问题（"找到所有调用这个函数的地方，检查它们的错误处理"）。

**对 Code_Graph 的启发**：Code_Graph 的 Neo4j 图索引 + 预设检索策略（Type A/B/C）是好的，但可以补充一个"LLM 主动探索模式"：当预设策略无法回答时，允许 LLM 自己发 `Grep`/`Glob` 探索代码库，结合图结构信息综合回答。

### 启发 2：LSP 是结构化代码理解的黄金标准

LSP 提供的定义跳转、引用查找、调用链追踪比 regex grep 精确得多，而且实时同步（每次编辑后自动更新）。

**对 Code_Graph 的启发**：
- clangd 的 `textDocument/documentSymbol` 可以获取结构信息（之前 `struct_field_parsing_analysis.md` 分析过这个方向）
- 可以考虑让 LLM 在回答问题时通过 LSP 实时查询代码结构，而不只依赖预建的图索引
- 但 LSP 需要本地安装 language server，当前环境可能不支持

### 启发 3：并行化独立检索操作

Claude Code 的 Read-only 工具（Glob/Grep/Read）并发执行，显著加速大范围代码探索。

**对 Code_Graph 的启发**：当前 QA 流程中，如果需要同时查询多个模块的信息，可以并发执行多个 Neo4j 查询，而不是串行等待每个结果。

### 启发 4：两档记忆模式（已验证）

MEMORY.md（快速摘要）+ topic files（按需加载）是**经过验证的生产级架构**。

**对 Code_Graph 的启发**：
- `Issue` 节点入库时可以同时写一个摘要到索引表（issue number、title、score、标签、关键信号）
- 检索时先用索引表粗筛，再读取详细 body，而不是每次都读完整 body

### 启发 5：子 Agent 并行探索子系统

对于需要探索多个独立子系统的复杂问题，spawn 多个子 agent 并行工作，各自探索后汇总。

**对 Code_Graph 的启发**：当问题涉及多个模块时（如"解释 llama-server 的路由机制和 chat template 的交互"），可以并行：
- Agent A：从 Neo4j 查询 server 路由相关函数
- Agent B：从 Neo4j 查询 chat template 相关函数
- 主 agent 综合两边的结果回答

### 启发 6：ToolSearch 模式对代码检索的类比

ToolSearch 的核心思想是：大量工具定义不一次性加载，用语义搜索按需获取。

**对 Code_Graph 的启发**：代码 chunk / Issue 数据不需要全部加载到 LLM 上下文。用一个轻量索引（如 issue 摘要表、函数签名表）做预筛选，每次只把最相关的 3-5 个 issue 的完整内容加载进来。

### 启发 7：压缩后重新注入持久化知识

Claude Code 的 CLAUDE.md 在上下文压缩后重新从磁盘读取，不依赖对话历史中的记忆。

**对 Code_Graph 的启发**：如果做长会话代码问答（如连续多轮对话），关键上下文（已确认的代码结构、相关 issue 列表）应该持久化到外部存储，在后续 turn 中重新注入，而不是依赖 LLM 的对话记忆。

### 启发 8：sideQuery 模式（源码验证）

`sideQuery.ts`（222 行）是 Claude Code 处理**主循环之外的 LLM 调用**的标准模式：
- 用于 memory 选择（`findRelevantMemories`）、权限解释、session 搜索、模型验证等
- 与主循环共享 API client 和 attribution 逻辑，但有独立上下文
- 支持 structured output、thinking budget、temperature 等参数

**对 Code_Graph 的启发**：Code_Graph 的 QA 流程中，Issue 摘要生成、问题意图分类、多候选结果的重排序等子任务，都可以用类似 `sideQuery` 的轻量 wrapper 调用 LLM，而不是都塞进主问答循环。

### 启发 9：熔断机制

`autoCompact.ts` 第 68-70 行：
```typescript
// 连续 autocompact 失败后停止重试
// 1,279 个 session 在单次会话中连续失败 50+ 次（最高 3,272 次），每天浪费约 250K 次 API 调用
const MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3
```

**对 Code_Graph 的启发**：在执行多轮检索策略时（如连续 3 次尝试都返回低质量答案后），应该停止重试并向用户返回"无法回答"的结论，而不是无限循环浪费资源。

---

## 7. Claude Code 没有做什么（刻意为之）

| 未采用的技术 | 原因 |
|------------|------|
| 预建 embedding 索引 | LLM + 搜索工具的动态检索效果更好，且避免索引陈旧问题 |
| AST 预解析 | 依赖 LSP 实时查询，不维护离线 AST |
| 图数据库 | 代码结构通过 LSP 动态访问 |
| 代码摘要预生成 | 代码由 LLM 直接理解，不需要预先摘要 |

Claude Code 的设计理念是：**让 LLM 主动探索比预建索引更灵活**，代价是每次探索都需要消耗 token 和时间。适合交互式开发场景，但不适合超大规模代码库的纯检索场景。

---

## 8. 核心机制详解（源码深度）

### 8.1 工具结果持久化：per-message 聚合预算

Claude Code 不仅有 per-tool 的结果大小限制，还有一个**per-message 聚合预算**（`toolResultStorage.ts`，1000+ 行）。

核心问题：一个大文件 Read 可能只有 50KB，但连续 3 个并行工具结果加起来可能超过 200K。简单截断会破坏 prompt cache 稳定性（每次截断产生的 preview 长度可能不同）。

**解决方案**：
- 追踪每个 `tool_use_id` 的处理结果（seenIds + replacements Map）
- 超过 per-message 限制时，**把最大的 fresh 结果持久化到磁盘**，替换成 2KB preview
- frozen 结果（已发送给 model 但未超过限制）不再替换，保证 prompt cache 稳定
- mustReapply 结果从 Map 直接查，不走 I/O

```typescript
// 状态不可变保证：一旦决定处理某 tool_use_id，其命运就冻结
type ContentReplacementState = {
  seenIds: Set<string>        // 已见过的 tool_use_id
  replacements: Map<string, string>  // 被替换的 → exact preview string
}
```

**对 Code_Graph 的启发**：当从 Neo4j 返回多个函数的详细信息时，可能总大小超过 LLM 上下文窗口。不应该逐个截断，而应该用类似的"持久化到磁盘 + 替换 preview"机制，保证每次发给 LLM 的内容长度稳定。

### 8.2 Token Budget 与递减回报检测

`tokenBudget.ts`（94 行）的 budget 检查机制：

```typescript
const DIMINISHING_THRESHOLD = 500
const COMPLETION_THRESHOLD = 0.9  // 90% of budget

// 连续 3 次继续且每次增长 <500 tokens → 递减回报，停止
const isDiminishing =
  tracker.continuationCount >= 3 &&
  deltaSinceLastCheck < DIMINISHING_THRESHOLD &&
  tracker.lastDeltaTokens < DIMINISHING_THRESHOLD
```

**对 Code_Graph 的启发**：多轮检索时，如果连续 N 轮返回的有效信息都在递减（如每次都只多找到 1 个相关函数），应该停止检索而不是继续浪费 API 调用。

### 8.3 压缩管线（4 层按序执行）

`query.ts` 第 369-467 行展示了每次 API 调用前的压缩管线：

```
messagesForQuery
  → applyToolResultBudget()         # 工具结果聚合裁剪
  → snipCompactIfNeeded()           # 历史裁剪（保留最后一条 assistant）
  → microcompact()                  # 微压缩
  → contextCollapse.applyCollapsesIfNeeded()  # 折叠（90%/95% 分级）
  → autocompact()                   # 自动压缩（effectiveWindow - 13K）
  → callModel()                      # 真正的 API 调用
```

每层职责单一，组合起来确保每次 API 调用都在 context window 内。

**对 Code_Graph 的启发**：QA 流程中的多阶段检索优化，可以借鉴这种分层策略：
- Layer 1: 向量相似度粗筛（快速）
- Layer 2: 图结构二次筛选（精确）
- Layer 3: LLM 主动探索（兜底）
各层按需调用，而不是每次都走完整流程。

### 8.4 async generator 流式循环

`query.ts` 的主体是 `async function* query()`——每次 yield 一个 `StreamEvent`，而不是等整个 query 完成。这使得：
- 工具执行结果可以边执行边 yield（streaming tool execution）
- 前端可以实时显示进度
- 某个工具失败不会导致整个 query 失败

```typescript
for await (const update of toolUpdates) {
  if (update.message) {
    yield update.message  // 边执行边返回
  }
}
```

**对 Code_Graph 的启发**：如果做实时展示的 QA 界面（streaming 答案），可以用类似模式，边检索边 yield 部分结果给用户。

---

## 9. 对 Code_Graph 当前实现的建议

基于以上分析，Code_Graph 当前基于 Neo4j 图 + 预设检索策略的方案是合理的，特别是对大规模代码库的检索场景。以下几个方向可以借鉴：

1. **补充 LLM 主动探索模式**：当预设策略（Type A/B/C）无法给出满意答案时，允许 LLM 用 `Grep`/`Glob` 工具探索代码库，结合图结构信息综合回答

2. **Issue 轻量索引表**：每个 Issue 入库时同时生成一个摘要行（number、score、标签、关键文件、commit SHA），检索时先用索引粗筛，再读详细 body

3. **并行化 Neo4j 查询**：当问题涉及多个不相关的代码区域时，并发执行多个查询

4. **LSP 集成探索**（长期）：如果能解决 clangd v14 的环境问题，可以用 LSP 做更精确的函数/变量导航

5. **工具结果的磁盘持久化**：当单次返回的 Neo4j 结果总量过大时，持久化到磁盘而不是简单截断，用 preview + 文件引用替代，保证长度稳定

6. **递减回报检测**：连续 N 次检索结果增量很小时停止，而不是无限重试

---

## 10. Eval-Driven Design：代码即证据

Claude Code 的源码中大量注释写明 `eval-validated` 或 `// Tested: X works better than Y`，说明他们用 A/B test 迭代 prompt 设计：

```typescript
// Header wording matters: "Before recommending" (action cue at the decision
// point) tested better than "Trusting what you recall" (abstract). The
// abstract header went 0/3; the action-header went 3/3 in eval.
```

**对 Code_Graph 的启发**：
- Issue quality scoring prompt 可以用类似方法迭代：先生成 v1 prompt，跑一批 issue 看评分分布，调 prompt，再跑，直到满意
- Memory prompt 的 what-not-to-save 和 trusting-recall 规则都是 eval 验证过的，可以直接借鉴到 Code_Graph 的答案生成质量规范

---

## 11. 其他精妙细节

### 11.1 memoryScan 的单遍优化

`memoryScan.ts` 第 30-33 行注释说明了扫描策略：

> "Single-pass: readFileInRange stats internally and returns mtimeMs, so we read-then-sort rather than stat-sort-read. For the common case (N ≤ 200) this halves syscalls."

**对 Code_Graph 的启发**：扫描大量文件时，优先读内容再 sort，而不是先 stat 再读，可以减少 syscall。

### 11.2 空 tool result 的 sentinel

`toolResultStorage.ts` 第 287-294 行对空结果注入 sentinel marker：

```typescript
// inc-4586: Empty tool_result content at the prompt tail causes some models
// (notably capybara) to emit the \n\nHuman: stop sequence and end their turn
// with zero output. Inject a short marker so the model always has something.
if (isToolResultContentEmpty(content)) {
  return { ...toolResultBlock, content: `(${toolName} completed with no output)` }
}
```

### 11.3 compact 运行在 fork agent 中

`compact.ts` 中的 `compactConversation` 运行在一个 forked agent（`runForkedAgent`）里，独立于主 query loop。这意味着压缩过程不阻塞主循环，压缩结果通过 channel 传回。

**对 Code_Graph 的启发**：对于耗时较长的 Issue 分析（如批量分析 100 个 issue），应该 fork 到后台执行，不阻塞前端响应。

---

## 12. 参考资料

- [How Claude Code works](https://code.claude.com/docs/en/how-claude-code-works)
- [Agent SDK - Agent Loop](https://code.claude.com/docs/en/agent-sdk/agent-loop.md)
- [Tools Reference](https://code.claude.com/docs/en/tools-reference.md)
- [Context Window Management](https://code.claude.com/docs/en/context-window.md)
- [Memory: How Claude remembers your project](https://code.claude.com/docs/en/memory.md)
- [Tool Search](https://code.claude.com/docs/en/agent-sdk/tool-search.md)
- [LSP Plugins](https://code.claude.com/docs/en/discover-plugins.md)
