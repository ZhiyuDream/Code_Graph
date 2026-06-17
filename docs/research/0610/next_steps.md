# 下一步：从 Retrieval 到 Evidence Navigation

## 背景

用户（导师）的系统分析指出：

> **代码仓问答未必缺检索，可能更缺导航。**

三个关键发现支撑这个判断：

1. **Topic Routing 已被证伪** — Document Routing File Hit Rate = 13.8%，Zero Hit = 68%。llama.cpp 的文档只描述"怎么用"，不映射"实现在哪"。Topic 应从 Routing Layer 降级为 Reasoning Layer（理解辅助，而非搜索入口）。

2. **最强路由信号已找到** — `grep symbol` File Hit Rate = 100%。对于 audit/bug/root cause 类任务，Question→Symbol→File 就够了，根本不需要 Topic。

3. **真正瓶颈不是 Retrieval** — Easy Benchmark 96% 检索全召回但只有 88-94% 引用全。Hard Benchmark 58% 检索全召回、92% Binary Judge 正确率但仅 42% 引用覆盖。**证据已经到了上下文里，但 Agent 不知道该看什么。**

## 核心问题重新定义

以前：
```
Question → 找不到证据 → 答错
```

现在：
```
Question → 证据找到了 → Agent 不会调查 → 答错
```

## 实验设计

### 实验目标

验证假设：**减少上下文量 + 引入文件级导航决策，可以比一次性塞入 Top20 获得更好的证据覆盖。**

### Baseline

```
Symbol Fast Path → Top N Retrieval → 一次性塞入 LLM → Answer
```

- Top N = 10-20（当前系统）
- LLM 从全部检索结果中 cherry-pick
- 问题：上下文太长，LLM 注意力分散，遗漏关键证据

### 实验组：Evidence Navigation

```
Symbol Fast Path → Top 3 初始文件
  → LLM 读文件 1
  → LLM 决定：继续深入 / 去读相关文件 / 停止
  → LLM 读文件 2
  → LLM 决定
  → ...
  → Answer
```

**关键设计**：

1. **每次只给一个文件**（或一个文件的特定行号范围）
2. **Agent 做导航决策**，不是搜索决策
3. **Action 空间**：
   - `read_more(file)` — 继续读这个文件的更多上下文
   - `read_related(file, relation)` — 读相关文件（callers/callees/imports）
   - `read_specific(file, line_range)` — 跳到特定行号精读
   - `mark_sufficient()` — 停止，已有足够证据
4. **停止条件**：
   - 主动标记 sufficient
   - 连续 2 步未获得新信息
   - 达到最大步数（如 10 步）

### 对比维度

| 指标 | Baseline | 实验组 |
|------|----------|--------|
| 平均访问文件数 | 10-20 | 3-8（预期） |
| Evidence 覆盖率 | ~71% (Hard) | 目标 >80% |
| 引用覆盖率 | ~42% (Hard) | 目标 >60% |
| Binary Judge 正确率 | 92% | 目标 ≥92% |
| 平均延迟 | 43s | 目标 <60s |
| 决策链可解释性 | 低（黑盒 cherry-pick） | 高（每步有明确决策理由） |

## 为什么这个实验有价值

### 1. 分离"检索问题"和"调查问题"

Hard Benchmark 目前同时存在两个问题：
- 检索层：找不到 gold 文件（29/50 检索不全）
- 调查层：找到了但不引用（29/50 搜到未引）

**关键分离实验**：
- 给定正确的初始文件（人工提供 Symbol Fast Path 的命中结果），Agent 能否通过导航找到所有 gold evidence？
  - 如果能：问题主要是检索层
  - 如果不能：问题主要是调查层

### 2. 验证"更少上下文 ≠ 更差答案"

当前 Agent 社区默认假设：上下文越多越好。但你们的数据暗示：
- audit_044：25 个文件（96% 噪声），LLM 精准引用了唯一的 gold
- audit_011：17 个文件（82% 噪声），LLM 被淹没，漏引了 speculative.cpp

这说明：**上下文质量比数量更重要，而质量取决于 Agent 的筛选能力。**

### 3. 接近工业界前沿

这个方向非常接近：
- **OpenHands** — 文件级操作 + 多轮决策
- **Claude Code** — 渐进式代码阅读 + 上下文管理
- **Cursor Agent** — 符号跳转 + 相关文件追踪
- **SWE-agent** — 文件系统导航 + 编辑操作

但你们的差异化价值在于：
- 有 Neo4j 代码图支撑（callers/callees/data flow）
- 有 benchmark 数据支撑（可以量化对比）
- 研究问题更聚焦：不是"怎么修 bug"，而是"怎么找到证据"

## 具体实现计划

### Phase 1：导航 Action 空间扩展（1-2 天）

1. 新增 Action：`read_file_lines(file_path, start_line, end_line)`
   - 直接读取源码，绕过 embedding 检索
   - 用于精读特定函数或代码段

2. 改造 `agent_loop.py`：
   - 维护一个 `visited_files` 集合（已读文件）
   - 维护一个 `candidate_files` 队列（待读文件，按优先级排序）
   - 每步 Decision 输出：下一步读哪个文件 + 为什么

3. 新增 Prompt：`navigation_decide.txt`
   - 告诉 LLM：你已经读了这些文件，发现了什么，接下来该读什么
   - 提供候选文件列表（来自 callers/callees/imports）

### Phase 2：Hard Benchmark 分离实验（2-3 天）

**实验 A：给定正确初始文件，测试导航能力**
- 人工为 Hard Benchmark 每道题选择 1-3 个正确的初始文件（覆盖部分 gold evidence）
- 运行 Navigation Agent，看能否通过 callers/callees 扩展到所有 gold evidence
- 记录每步决策链

**预期结果**：
- 如果 Navigation Agent 能找全大部分 gold → 调查策略有效
- 如果 Navigation Agent 仍然 cherry-pick → 需要改进 Decision Prompt

**实验 B：对比 Top20 vs Top3 + Navigation**
- Baseline：Symbol Fast Path 返回 Top 20，一次性塞入
- 实验组：Symbol Fast Path 返回 Top 3，Agent 通过导航逐步扩展
- 对比 Evidence 覆盖率和引用覆盖率

### Phase 3：决策策略优化（3-5 天）

基于实验 A/B 的结果，优化导航策略：

1. **如果 Agent 总是选错下一步**：
   - 引入文件重要性评分（embedding similarity / call frequency / 问题相关性）
   - 限制候选文件列表，只给 Top 5 最相关的

2. **如果 Agent 过早停止**：
   - 引入"覆盖率检查"：每步计算已读文件覆盖了多少 gold evidence
   - 强制要求 Agent 确认"已覆盖所有相关模块"才能停止

3. **如果 Agent 陷入循环**：
   - 引入回退机制：如果连续访问同一文件，强制切换方向
   - 引入"探索 vs 利用"平衡：初期多探索，后期聚焦

## 长期研究方向

如果 Phase 1-3 验证成功，可以扩展到：

1. **Multi-Agent Navigation**：一个 Agent 负责"检索"，一个 Agent 负责"调查"，一个 Agent 负责"验证"
2. **Learned Navigation Policy**：用 Hard Benchmark 的数据训练一个 Navigation Policy（如 RL 或模仿学习）
3. **Human-in-the-loop Investigation**：允许用户介入导航决策（如 Claude Code 的 Y/N 确认）

## 总结

> **Retrieval 找到的是入口，Investigation 决定的是证据链。**

你们现在已经证明了：
- Symbol Fast Path 能找到入口（Easy 96% 全召回）
- 但 Agent 不会沿着入口走进去（Hard 仅 42% 引用覆盖）

下一步不是继续优化入口，而是教会 Agent 怎么走进去、什么时候停下来、什么时候回头换条路。

这就是 **Evidence Navigation**。
