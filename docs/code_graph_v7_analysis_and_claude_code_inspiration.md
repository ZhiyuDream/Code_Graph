# Code_Graph v7 现状分析与 Claude Code 架构启发报告

> 分析日期：2026-04-16  
> 分析对象：Code_Graph v7 实现 + `/data/yulin/RUC/extracted_source`（Claude Code 源码）

---

## 一、执行摘要

**Code_Graph v7 目前处于"效率优化瓶颈期"**。经过 P0（混合策略+智能停止）、P1（LLM 路由）、V7 Minimal（精简扩展）三轮迭代，团队已经用大量实验数据证明了一个核心结论：

> **简单混合策略（P0）准确率最高（~74%），LLM 路由（P1）未能超越，而盲目移除低效工具（V7 Minimal）导致准确率暴跌 8.5%。**

当前最大的矛盾是：** callers/callees 扩展平均效率极低（0.5 新函数/次，80% 空手而归），但完全移除后准确率显著下降**。这说明扩展在"关键路径"上有隐性价值，只是当前触发机制过于粗放。

通过对 `extracted_source`（即 Anthropic Claude Code 的 TypeScript 源码）的深入研究，我们发现它是一个**工程化程度极高、设计模式非常先进的 Agent 辅助编程框架**。其核心优势在于：统一的递归查询循环、工具并发执行引擎、Fail-Closed 的安全默认、以及多层上下文压缩机制。这些设计对 Code_Graph 从"脚本原型"向"工程化 Agent 框架"升级具有重要参考价值。

---

## 二、Code_Graph v7 现状深度分析

### 2.1 版本演进与性能全景

| 版本 | 准确率 | 0-分 | 平均延迟 | 平均步数 | 核心策略 |
|------|--------|------|----------|----------|----------|
| **V7 Base** | 71.7% | 9 | 61.5s | 2.9 | Embedding + Neo4j ReAct |
| **P0（最佳）** | **73.9%** | **1** | 55.3s | **2.2** | + Grep Fallback + 智能熔断 |
| P0 重跑 | 71.1% | 1 | **31.1s** | 3.2 | 增加了工具调用记录 |
| P1 | 71.1% | 1 | 42.5s | 2.2 | LLM 智能路由 |
| **V7 Minimal** | **65.6%** | 4 | 28.0s | 3.9 | 移除 callers/callees |

**关键洞察：**
1. **P0 是当前的帕累托最优**：准确率最高、0-分最少、步数最少。
2. **P1 的 LLM 路由是负优化**：增加了 ~11s 延迟，准确率下降 3%，说明"用 LLM 做路由"在这个场景下成本收益不成正比。
3. **V7 Minimal 证明了一件事**：callers/callees 虽然低效，但**不能简单砍掉**。

### 2.2 核心架构拆解

v7 的实现分布在两个平行的代码路径上：

#### A. 完整 Agent（`tools/agent_qa.py`）—— 被"雪藏"的老版本
- 使用 OpenAI Function Calling API
- 注册了 15+ 个工具：模块定位、函数搜索、变量/属性搜索、文件读取、Issue 搜索、语义搜索
- 有 `read_file_lines`、`search_variables`、`search_attributes` 等**高精度工具**
- **但 v7 的 benchmark 脚本并没有直接用它**，原因是历史优化过程中觉得 Function Calling 的延迟和 token 成本高

#### B. v7 自定义脚本（`scripts/run_qa_v7_*.py`）—— 当前主战场
- **手搓 JSON 解析 ReAct**：不用 Function Calling，而是直接把 tool 描述写进 prompt，让 LLM 输出 JSON 格式的 `{"thought": ..., "action": ..., "target": ...}`，然后正则提取
- 工具集极度精简：只有 `semantic_search`、`grep_fallback`、`expand_callers/callees`、`issue_search`、`explore_file`
- 核心流程：
  ```
  Step 1: 初始检索（semantic + issue，P0 加 grep fallback，P1 加路由）
  Step 2-5: ReAct 决策 → expand_callers/callees 或停止
  Final: 基于收集的上下文生成答案
  ```

**架构问题：**
- **两个代码路径割裂**：完整 agent 里有很多好工具（如 `read_file_lines`），但 v7 脚本无法使用
- **ReAct 是硬编码的**：只能做 callers/callees 扩展，模型不能自由选择 "读取文件"、"搜索变量" 等更精细的操作
- **没有真正的工具并发**：虽然 P1 的 hybrid 模式用了 `ThreadPoolExecutor`，但 ReAct 步骤本身是串行的

### 2.3 关键问题诊断（基于实验数据）

#### 问题 1： callers/callees 扩展的"悖论"
- **效率极差**：占 57.5% 的工具调用，平均仅发现 0.47 个新函数，80% 的调用空手而归
- **但价值极高**：完全移除后准确率从 74.1% → 65.6%（-8.5%）
- **根因**：扩展在"调用链问题"和"文件结构问题"的关键路径上提供了不可替代的上下文，但当前**每道题都盲目扩展**，造成大量无效调用

#### 问题 2：初始检索的覆盖不足
- 37-48% 的错误题目检索到的函数 ≤ 3 个
- 对于"ggml-alloc.c 中包含哪些主要代码结构"这类问题，embedding 只能召回 1-3 个函数
- 模型在信息不足时会"脑补"，导致准确性下降

#### 问题 3：智能停止过于激进
- P1 中 83.7% 的错误题目在 Step 2 就停止了
- 系统过早判断"信息充足"，但实际上检索到的上下文远远不够回答问题
- V7 Minimal 虽然走到了最大步数（96.8%），但那些步骤是"空转"（skipped_expansion），毫无意义

#### 问题 4：LLM 路由的误判
- P1 的 LLM 路由对架构/模块类问题的误判率高达 26.9%
- 最优策略 `grep` 准确率 76.7%，但只被使用了 11.9%
- 最优组合 `semantic+graph` 平均召回 7.7 个函数，但只被使用了 2.5%

#### 问题 5：缺乏对非函数符号的检索
- v7 脚本完全没有使用 `search_variables`、`search_attributes`、`read_file_lines`
- 对于宏定义（如 `GGML_TENSOR_LOCALS_2`）、结构体字段、常量枚举等问题，完全束手无策

---

## 三、extracted_source（Claude Code）框架研究

### 3.1 项目定位

`/data/yulin/RUC/extracted_source/parent_dir/src` 是 **Anthropic Claude Code** 的核心 TypeScript 源码。这是一个生产级的 AI 编程助手 CLI，其架构成熟度远超一般研究原型。它的核心使命是：**让 LLM 通过工具调用安全、高效、可扩展地操作代码库**。

### 3.2 核心架构亮点

#### 1. 统一的递归查询循环（`query.ts`）

Claude Code 的精髓在于：**无论主线程、同步子 Agent、异步子 Agent、还是压缩 Agent，都跑在同一个 `query()` 函数里**。

```typescript
// query.ts — 约 1700 行的 AsyncGenerator
while (true) {
  // 1. 上下文预处理（snip / microcompact / autocompact / collapse）
  // 2. 调用模型（callModel）
  // 3. 流式解析工具调用
  // 4. 执行工具（runTools / StreamingToolExecutor）
  // 5. 将结果注入消息历史，递归下一轮
}
```

**对 Code_Graph 的启示：**
- Code_Graph v7 的 ReAct 是**硬编码步骤**（Step 1 检索 → Step 2-5 扩展），而 Claude Code 的 ReAct 是**通用消息循环**（LLM 自主决定调用哪些工具）。
- 如果 Code_Graph 也能让 LLM 自主决定"读取文件"、"搜索变量"、"扩展调用链"，而不是限制在固定的 `expand_callers/callees` 里，灵活性和准确性都会大幅提升。

#### 2. 工具定义的工程化标准（`Tool.ts` + `buildTool`）

Claude Code 的工具接口是我见过最全面的：

```typescript
type Tool = {
  name: string
  call(args, context, canUseTool, parentMessage, onProgress?): Promise<ToolResult>
  isConcurrencySafe(input): boolean   // 是否能并行
  isReadOnly(input): boolean          // 是否只读
  isDestructive?(input): boolean      // 是否危险操作
  interruptBehavior?(): 'cancel' | 'block'
  validateInput?(input, context): Promise<ValidationResult>
  checkPermissions(input, context): Promise<PermissionResult>
  // ... 还有 20+ 个用于 UI、渲染、搜索索引、权限匹配的方法
}
```

最精彩的是 `buildTool` 的 **Fail-Closed 默认策略**：
- `isConcurrencySafe` 默认 `false`（必须显式声明安全才能并行）
- `isReadOnly` 默认 `false`（必须显式声明只读）
- `isDestructive` 默认 `false`

**对 Code_Graph 的启示：**
- Code_Graph 目前的工具是"裸函数"，没有统一的接口抽象。
- 引入 `Tool` 接口后，可以清晰地标记哪些检索工具可以并发执行（如 `semantic_search` + `grep_search` + `issue_search` 可以并行），哪些需要串行（如 `read_file_lines` 应该在定位到文件后执行）。

#### 3. StreamingToolExecutor — 并发工具执行引擎

Claude Code 不是等 LLM 把整段话写完再执行工具，而是**流式解析**：一旦检测到 `<tool_use>` 块，立即启动 `StreamingToolExecutor`。

并发规则：
- **Read-only + concurrency-safe** 的工具批量并行执行（默认最多 10 个并发）
- **Non-safe** 的工具独占执行
- 只有 **Bash 错误**会取消同级并行任务，其他工具（如 Read、WebFetch）失败不影响同伴

**对 Code_Graph 的启示：**
- P1 的 hybrid 模式已经尝试了并行，但只限于初始检索。
- 如果 Code_Graph 采用 Claude Code 的并发模型，**每一轮 ReAct 都可以同时发起多个检索请求**（例如：同时搜索 embedding、grep、issue、甚至多个文件），将多步串行变为一步并行，显著降低延迟。

#### 4. AgentTool — 递归子 Agent 即工具

Claude Code 把"启动子 Agent"也封装成了一个工具 `AgentTool`。子 Agent 调用 `runAgent()`，而 `runAgent()` 本质上还是调用 `query()`。

关键优化：**Fork Subagent Prompt Cache Sharing**
- 子 Agent 继承父 Agent 的 `renderedSystemPrompt`（冻结的系统 prompt 字节）
- 使用完全相同的 tools 数组
- 保证 API 请求的前缀字节完全一致 → **命中 prompt cache，降低 token 成本**

**对 Code_Graph 的启示：**
- Code_Graph 可以尝试把" callers/callees 深度扩展"拆成子 Agent 任务：主 Agent 负责初始检索和路由判断，子 Agent 负责对特定函数进行深度调用链分析。这样主循环不会被低效扩展拖慢。
- 如果未来要做多 Repo 分析或多文件并行探索，子 Agent 模式是天然的可扩展架构。

#### 5. 多层上下文压缩（Context Compaction Stack）

Claude Code 有四层上下文管理机制叠加使用：

| 层级 | 机制 | 作用 |
|------|------|------|
| L1 | **Snip** | 删除历史消息中旧的保护尾部内容 |
| L2 | **Microcompact** | 缓存式编辑工具结果 |
| L3 | **Autocompact** | token 超限后用 LLM 总结历史 |
| L4 | **Context Collapse** | 读取时投影，将细粒度消息归档为摘要 |

**对 Code_Graph 的启示：**
- Code_Graph v7 的 ReAct 循环目前只处理很少的上下文（几个函数名 + Issue 标题），还没有遇到长上下文问题。
- 但如果未来让 LLM 自主调用 `read_file_lines` 读取大量代码，上下文会迅速膨胀。提前设计上下文压缩机制（如：将超过 800 token 的工具结果自动摘要）是必要的。

#### 6. Hook 架构（PreToolUse / PostToolUse / StopHooks）

工具执行前后有一整套 hook 管道：
- `PreToolUseHook`：可以重写输入、自动批准权限、注入额外上下文、或阻止执行
- `PostToolUseHook`：观察结果、记录日志
- `StopHook`：评估 assistant 消息，可以在循环继续前注入阻止信息

**对 Code_Graph 的启示：**
- Code_Graph 目前的"智能停止"（熔断机制）是硬编码在脚本里的。
- 如果改用 hook 架构，"停止决策"可以变成一个独立的 `StopHook`，根据当前检索到的函数数量、分数分布、问题类型等动态判断是否停止，而不需要耦合在主循环里。

---

## 四、对 Code_Graph 的具体优化启发

基于以上分析，我们将启发分为四个层面：**架构层、工具层、检索层、执行层**。

### 4.1 架构层：从"硬编码 ReAct 脚本"升级为"通用 Agent 循环"

**现状问题：**
- v7 脚本的 ReAct 循环是手搓的：Step 1 固定做初始检索，Step 2-5 只能做 callers/callees 扩展
- 完整 agent（`tools/agent_qa.py`）里的好工具（`read_file_lines`、`search_variables`）被弃用

**借鉴 Claude Code 的做法：**

1. **统一入口：使用 Function Calling 或结构化输出**
   - 让 LLM 自主决定调用哪个工具，而不是限制在固定的 `expand_callers/callees`
   - 工具列表应包括：
     - `semantic_search`（语义搜索）
     - `grep_search`（精确搜索）
     - `get_callers` / `get_callees`（调用链扩展）
     - `get_file_functions`（文件内函数列表）
     - `read_file_lines`（读取具体代码）
     - `search_variables` / `search_attributes`（变量/成员搜索）
     - `search_issues`（Issue 搜索）
     - `sufficient`（信息充足，停止检索）

2. **引入统一的 `AgentLoop`**
   - 参考 `query.ts`，设计一个 Python 版本的通用循环：
     ```python
     async def agent_loop(question: str, tools: list[Tool], max_turns: int = 5):
         messages = [system_prompt, user_message]
         for turn in range(max_turns):
             response = await llm.chat.completions.create(
                 messages=messages,
                 tools=tools,
             )
             tool_calls = response.tool_calls
             if not tool_calls:
                 return response.content  # 直接回答
             
             # 并发执行工具
             results = await execute_tools_concurrently(tool_calls, tools)
             messages.extend(format_tool_results(results))
         
         # 最终生成答案
         return await generate_answer(messages)
     ```
   - 这样做的好处：任何新工具（如 `search_macros`）加进去后，LLM 立刻就能使用，不需要改 ReAct 脚本

### 4.2 工具层：建立工具注册表与并发调度

**现状问题：**
- 工具是散落在各处的裸函数，没有统一接口
- P1 的 hybrid 并行是临时写的 `ThreadPoolExecutor`，没有通用的并发调度逻辑

**借鉴 Claude Code 的做法：**

1. **定义统一的 `Tool` 接口（Python 版）**
   ```python
   @dataclass
   class Tool:
       name: str
       description: str
       parameters: dict  # JSON Schema
       is_read_only: bool = True
       is_concurrency_safe: bool = True
       call: Callable[..., ToolResult]
   ```

2. **工具并发调度器**
   - 将 Read-only 工具（所有检索类工具）标记为 `concurrency_safe=True`
   - 每一轮 LLM 可以同时输出多个 tool_calls，调度器自动并行执行
   - 例如：一轮内同时调用 `semantic_search`、`grep_search`、`search_issues`，而不是分三步走
   - 这可以将 P0 的 2.2 步/题进一步压缩到 ~1.5 步/题（大部分检索在第一步并行完成）

3. **Fail-Closed 安全默认**
   - 任何新工具默认 `is_concurrency_safe=False`，必须显式声明才能并行
   - 写操作（如果未来有）默认 `is_read_only=False`

### 4.3 检索层：条件触发 + 文件级探索 + 非函数符号

这是最直接能解决 v7 当前痛点的层面。

#### 改进 1：条件触发 callers/callees（最关键）

不要用 LLM 路由每道题，而是用**快速规则**判断是否需要调用链扩展：

```python
CALL_CHAIN_KEYWORDS = {
    '调用', 'caller', 'callee', '链', 'chain', 
    '流程', 'flow', '执行', '过程', 'process', 
    '哪里调用', '依赖', 'depend', '关系'
}

def needs_call_chain_expansion(question: str) -> bool:
    q = question.lower()
    return any(kw in q for kw in CALL_CHAIN_KEYWORDS)
```

- **只有命中关键词时**，才把 `get_callers` / `get_callees` 放进可用工具列表
- 其他题目直接跳过扩展，保留 P0 的准确率同时消除 80% 的无效调用
- 这比 P1 的 LLM 路由快得多（0ms vs ~2s LLM 延迟），且更准确

#### 改进 2：文件级问题的定向处理

对于"文件 X 中有哪些函数/结构"类问题，**绕过 embedding，直接用 `get_file_functions` 或 `read_file_lines`**：

```python
FILE_STRUCTURE_KEYWORDS = {
    '包含哪些', '有哪些函数', '文件中', '代码结构', 
    '定义了哪些', 'inside', 'in file', 'contains'
}

def is_file_structure_question(question: str) -> bool:
    return any(kw in question.lower() for kw in FILE_STRUCTURE_KEYWORDS)
```

- 先用简单正则从问题中提取文件名
- 直接调用 `tool_get_file_functions(driver, file_path)` 获取完整函数列表
- 这比 embedding 搜索的召回率高得多

#### 改进 3：把 `read_file_lines` 重新纳入 ReAct 循环

`tools/agent_qa.py` 里已经有 `read_file_lines`，但 v7 脚本没有使用。应该让它回归：

- 当 LLM 需要查看结构体定义、宏定义、变量类型时，可以直接调用 `read_file_lines`
- 特别对于 `search_attributes` 找到的结构体字段，后续**必须**用 `read_file_lines` 读取源码来确认类型和注释

#### 改进 4：宏定义和常量搜索

llama.cpp 中大量功能由宏驱动（如 `GGML_TENSOR_LOCALS_2`），但当前图里宏不是节点。建议：
- 新增 `search_macros` 工具：内部用 `rg` 搜索 `#define XXX` 模式
- 或让 `grep_search` 增加宏模式匹配逻辑

### 4.4 执行层：更智能的停止策略与错误恢复

**现状问题：**
- P0 的熔断：连续 2 轮信息增益 ≤ 1 时停止 → 太激进
- V7 Minimal：走到最大步数但空转 → 太浪费

**改进建议：**

1. **相关性感知的停止条件**
   ```python
   def should_stop(info_gain_history, current_functions, top_score):
       recent_gains = info_gain_history[-2:]
       # 条件 A：连续低增益 + 已有足够信息
       if all(g <= 1 for g in recent_gains) and len(current_functions) >= 5 and top_score >= 0.6:
           return True
       # 条件 B：连续 2 轮 0 增益，无论如何停止
       if all(g == 0 for g in recent_gains):
           return True
       # 条件 C：已经达到了合理的函数上限
       if len(current_functions) >= 12:
           return True
       return False
   ```

2. **从硬编码停止改为 Hook 模式**
   - 设计一个 `StopDecisionHook`，在每一轮工具执行后评估是否停止
   - Hook 可以访问：当前函数列表、Issue 列表、历史增益、问题类型
   - 这样停止逻辑可以独立演进，不影响主循环

3. **错误恢复机制**
   - 当前 `react_decide` 的 JSON 解析失败时，fallback 逻辑是硬编码的 `expand_callees`
   - 应该改为：如果 LLM 输出不可解析，向 messages 中注入一条 "请使用正确 JSON 格式" 的提示，然后重试，而不是盲目继续

---

## 五、具体改进路线图

### 短期（1-2 周）：基于现有脚本的最小改动优化

目标：**在现有 `run_qa_v7_optimized.py` 基础上，把准确率从 65.6%（V7 Minimal）或 71.1%（P1）拉回 P0 水平甚至超越**。

1. **恢复 callers/callees，但改为条件触发**
   - 用 `_should_use_call_chain(question)` 规则判断
   - 预计可减少 60-70% 的无效扩展调用，同时保留关键路径上的准确性

2. **修复文件结构类问题的检索**
   - 在 `react_search` 的 Step 1 增加问题类型检测
   - 如果是文件结构问题，直接调用 `tool_get_file_functions` 而非 embedding

3. **调优停止条件**
   - 把 P0 的 `连续2轮增益≤1` 改为 `连续2轮增益≤1 且 函数数≥5 且 top_score≥0.6`
   - 避免在检索不足时过早停止

4. **P1 路由的降级/移除**
   - 实验数据表明 P1 的 LLM 路由是负优化
   - 改为固定混合策略（Semantic + Grep Fallback + Issue），或仅用快速关键词路由

### 中期（1 个月）：引入通用 Agent 循环

1. **重构 `tools/agent_qa.py` 的完整 agent**
   - 让它支持 OpenAI Function Calling（或国产模型的等价功能）
   - 把 v7 脚本里验证过的好策略（Grep Fallback、条件扩展、智能停止）迁移到完整 agent 中

2. **统一工具注册表**
   - 设计 Python 版 `ToolRegistry`，支持并发调度
   - 所有工具（包括 `read_file_lines`、`search_variables`）统一注册

3. **工具并发优化**
   - 初始检索阶段：并行执行 `semantic_search` + `grep_search` + `search_issues`
   - 用 asyncio 实现，预计可将延迟再降 20-30%

4. **集成 `read_file_lines` 和 `search_variables`**
   - 在 ReAct 循环中释放这些工具的调用权限
   - 特别关注 struct/macro/variable 类问题

### 长期（2-3 个月）：向 Claude Code 级别的工程化迈进

1. **子 Agent 任务拆分**
   - 主 Agent：负责问题理解和初始检索
   - 子 Agent 1（CallGraphAgent）：深度分析调用链
   - 子 Agent 2（FileInspectorAgent）：深度分析特定文件的代码结构
   - 子任务可以后台运行，主 Agent 汇总结果

2. **上下文压缩与长上下文支持**
   - 当 `read_file_lines` 读取大量代码后，设计自动摘要机制
   - 学习 Claude Code 的 microcompact / autocompact 思路

3. **Prompt Cache 优化**
   - 冻结系统 prompt 和工具描述的渲染结果，在多道题之间复用（如果 benchmark 是批量跑的）
   - 这可以显著降低 token 成本

4. **更精细的评估与 A/B 测试框架**
   - 建立每道题的"工具调用轨迹"记录（已经部分实现）
   - 开发自动化回归测试：任何改动都要与 P0 做 diff 分析

---

## 六、结论

**Code_Graph v7 已经通过大量实验找到了正确的方向，但被困在了"脚本级优化"的天花板上。**

当前最核心的任务不是继续调 P1 的路由参数，而是：

> **1. 恢复 callers/callees 并用规则化条件触发替代盲目调用；**  
> **2. 把被弃用的高精度工具（`read_file_lines`、`search_variables`）重新纳入循环；**  
> **3. 逐步从硬编码 ReAct 脚本迁移到通用 Agent 循环，借鉴 Claude Code 的并发执行、工具注册表、Hook 架构等工程化设计。**

Claude Code 的源码向我们展示了一个**生产级 Agent 框架**应该有的样子：统一的递归循环、流式并发执行、Fail-Closed 的安全设计、以及可扩展的 Hook 管道。Code_Graph 如果能吸收这些架构思想，将有机会突破 74% 的准确率瓶颈，向 80%+ 迈进。
