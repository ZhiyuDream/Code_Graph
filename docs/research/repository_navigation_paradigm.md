# 从 GraphRAG 到 Repository Navigation：认知跃迁与下一阶段方向

**日期**: 2026-05-12 | **作者**: Code_Graph Team

---

## 一、核心发现：图质量提升 ≠ QA 准确率提升

### 1.1 已完成的验证

| 改进项 | 投入 | DeepSeek 效果 | 结论 |
|--------|------|--------------|------|
| P0 数据清洗（去重、过滤 vendor） | 大 | +0.3pp | 几乎无感 |
| P1 调用解析增强（全局匹配、宏提取） | 大 | +0.3pp | CALLS 边 +82.9% 未转化 |
| P2 Module 节点（Louvain 社区发现） | 大 | -0.9pp | 引入噪声 |
| P2 + 关键词过滤 + 高频降权 | 中 | -0.3pp | 噪声修复无提升 |
| GPT-4.1-mini 全量验证 | 中 | 80.2% | 同样未受益 |

### 1.2 关键结论

> **DeepSeek 检索天花板确认为 ~83%，且该天花板与 CALLS 边数量、Module 质量、节点去重程度均无关。**

这意味着：
- ❌ 继续补 CALLS 边 = 无效投入
- ❌ 继续优化社区发现算法 = 无效投入
- ❌ 继续清洗/去重节点 = 边际收益递减

**真正的问题不是"图不够准"，而是"检索路径不对"。**

---

## 二、问题本质：从"局部片段检索"到"仓库级导航"

### 2.1 当前系统的检索范式

```
query
→ embedding / grep 找函数（扁平 top-k）
→ graph 扩展（CALLS、Module）
→ LLM 读（局部片段拼接）
```

这是 **"局部相关片段检索"（Flat Chunk Retrieval）**。

**问题**：
- 搜索空间是"全仓库所有函数"
- LLM 不知道该先看哪里
- benchmark 中的问题（如"exaone 的数据流向"）需要的是**流程理解**，不是**语义最相似的函数**

### 2.2 人类工程师的检索范式

```
query: "数据怎么进入 exaone"

↓

工程师不会：
  grep -r "exaone" | head -20

工程师会：
  1. README → 找到 backend 相关说明
  2. 目录 → 定位到 ggml-backend/
  3. 模块 → 找到 exaone 相关文件
  4. 入口 → 找 main() 或 register 函数
  5. 主流程 → 沿调用链追踪数据流
  6. 细节 → 定位具体实现函数
```

这是 **"分层缩小搜索空间"（Hierarchical Search）**。

**核心差异**：
| 维度 | Flat Retrieval | Hierarchical Navigation |
|------|---------------|------------------------|
| 起点 | 全仓库所有 chunk | 粗粒度摘要/目录/模块 |
| 策略 | 语义相似度排序 | 启发式逐层缩小 |
| 目标 | 找"最像"的函数 | 找"入口"和"流程" |
| 空间 | O(n) 搜索 | O(log n) 搜索 |
| 噪声 | 高（top-k 截断引入无关函数）| 低（每层过滤无关分支）|

---

## 三、为什么 CALLS 边没收益？

### 3.1 根本原因

```
embedding 找错了起点
→ 后面全错
→ CALLS 再多也没用
```

**具体表现**：
- 问题问的是"exaone 的数据流"
- embedding 召回的 top-5 可能是 `exaone_init`、`exaone_forward`、`ggml_exaone_xxx` 等孤立函数
- CALLS 扩展从这些错误起点展开，只会召回更多无关调用链
- Module 扩展召回同模块的 `ggml_malloc`、`max`、`min` 等噪声函数

**本质**：
> **不是"图不够全"，而是"起点不够对"。**

### 3.2 和导师方向的关联

导师此前提到：
> "要像人类工程师一样找代码"

现在验证了这个方向的正确性：
- 人类工程师不依赖"全仓 embedding top-k"
- 人类工程师依赖"仓库层级结构 + 启发式导航"
- 当前系统的瓶颈正是"缺乏层级导航能力"

---

## 四、下一阶段方向：Repository Navigation Agent

### 4.1 核心思想

> **ZoomRAG：先看粗粒度摘要，再逐步 zoom in 到细节**

代码仓天然具备层级结构：
```
Repository
├── Directory
│   ├── Module (Louvain 社区)
│   │   ├── File
│   │   │   ├── Class
│   │   │   │   ├── Function
│   │   │   │   │   ├── Statement
```

每一层都可以提供**摘要信息**，帮助 LLM 判断"是否相关"。

### 4.2 与传统 RAG 的区别

| 特性 | 传统 RAG / GraphRAG | Repository Navigation |
|------|---------------------|----------------------|
| 检索单元 | 扁平 chunk / 节点 | 层级摘要（Repo → Dir → File → Func）|
| 扩展策略 | 沿边扩展（CALLS、MENTIONS）| 沿层级 zoom in |
| 决策依据 | 相似度分数 | 启发式（目录名、摘要、入口点）|
| LLM 角色 | 读代码 + 回答问题 | **导航决策者** + 读代码 + 回答 |
| 信息增益 | 递减（多跳后噪声累积）| 递增（每层更聚焦）|

### 4.3 建议的检索流程

```
Phase 1: 仓库级粗定位（Coarse-grained Localization）
  query
  → README / 目录结构 / Module 摘要
  → LLM 决策："这个问题属于哪个模块/目录？"
  → 输出：相关目录列表（如 ["ggml/src/", "examples/"]）

Phase 2: 模块级入口识别（Entry Point Discovery）
  在相关目录内
  → 文件摘要 / 函数签名 / fan_in/fan_out
  → LLM 决策："入口函数可能是哪个？"
  → 输出：候选入口函数列表（如 ["main", "llm_build_exaone", "register_backend"]）

Phase 3: Zoom-in 调用链追踪（Fine-grained Call Chain Tracing）
  从入口函数
  → 沿 CALLS 扩展（可控深度）
  → 读取关键函数代码
  → 输出：完整调用链 + 代码片段

Phase 4: 答案生成
  → LLM 综合所有信息生成答案
```

### 4.4 为什么这更像"真正研究"

传统代码 RAG 的论文：
> "本文提出 GraphRAG，通过构建代码调用图提升检索质量..."

Repository Navigation 的论文：
> "传统 RAG 采用扁平化检索，而代码仓天然具备层级结构。本文提出 **coarse-to-fine 的 repository navigation 检索范式**，让 LLM 像人类工程师一样逐层缩小搜索空间..."

后者是**范式创新**，前者是**工程优化**。

---

## 五、具体实验方案

### 5.1 短期实验（1-2 周）

#### 实验 1：Module 摘要驱动检索

**假设**：先读 Module 摘要，再决定检索哪个模块，比直接全仓 embedding 更准确。

**方法**：
1. 为每个 Module 生成 LLM 摘要（已存在 `generate_module_summaries.py`）
2. 问题先过一遍 Module 摘要，LLM 选择 Top-3 相关 Module
3. 只在相关 Module 内做 embedding / graph 检索
4. 对比：全仓检索 vs Module 限定检索

**预期**：检索噪声降低，准确率 +2-5%

#### 实验 2：入口点识别（Entry Point Discovery）

**假设**：流程类问题的关键是找到"入口函数"，而非语义最相似的函数。

**方法**：
1. 从 Neo4j 中识别入口候选（无 CALLS 入边、或 fan_in 很低的函数）
2. 用 LLM 判断问题属于哪个"流程类型"
3. 匹配到对应入口函数
4. 从入口沿 CALLS 追踪

**预期**：流程类问题准确率 +5-10%

### 5.2 中期实验（2-4 周）

#### 实验 3：层级导航 Agent（Hierarchical Navigation Agent）

**目标**：实现完整的 Repository Navigation 流程

**架构**：
```python
def repository_navigation_agent(question: str) -> Answer:
    # Layer 1: Repo-level
    relevant_dirs = select_directories(question, repo_tree)
    
    # Layer 2: Module-level
    relevant_modules = select_modules(question, relevant_dirs)
    
    # Layer 3: Entry-level
    entry_points = discover_entry_points(question, relevant_modules)
    
    # Layer 4: Call-chain
    call_chain = trace_call_chain(entry_points, depth=3)
    
    # Layer 5: Answer
    return generate_answer(question, call_chain)
```

**对比基线**：
- 传统 Flat RAG（embedding top-k）
- GraphRAG（CALLS 扩展）
- **Repository Navigation**（本方法）

**评估指标**：
- 首次命中率（第一层就选中正确模块的比例）
- 检索 token 量（是否减少无关上下文）
- QA 准确率
- 答案中提到的函数是否真实存在于检索路径中

### 5.3 长期方向（1-2 个月）

#### 方向 1：自适应导航策略

不同问题类型需要不同导航路径：
- "函数 xxx 的核心逻辑" → 直接精确查找（LSP / Grep）
- "数据怎么进入 exaone" → 流程导航（入口 → 调用链）
- "为什么选择这种设计" → 设计决策检索（Issue / PR / 注释）

用 LLM 做**路由决策**，选择最佳导航路径。

#### 方向 2：多仓库迁移

验证 Repository Navigation 是否可迁移到其他代码仓：
- Linux Kernel（超大规模）
- PyTorch（Python/C++ 混合）
- Redis（C 项目）

---

## 六、和 ZoomRAG 的关联

ZoomRAG 的核心：
> "全局 → 局部 → 更局部"

Repository Navigation 的核心：
> "Repo → Dir → Module → File → Function → Statement"

**天然匹配**。

ZoomRAG 的"全局摘要"对应 Repository Navigation 的"Module 摘要"和"目录结构"。
ZoomRAG 的"局部细节"对应 Repository Navigation 的"入口函数"和"调用链"。

**下一步可以做的**：
1. 把 Module 摘要作为 ZoomRAG 的"全局层"
2. 把入口函数识别作为"局部层"
3. 把 CALLS 扩展作为"细节层"

---

## 七、总结

### 7.1 已完成的工作价值

| 发现 | 价值 |
|------|------|
| "CALLS 边 +82.9% 无收益" | 证明**边数量不是瓶颈** |
| "Module 扩展引入噪声" | 证明**扩展策略比图结构更重要** |
| "高频函数降权无效" | 证明**噪声不在高频函数，而在检索起点** |
| "GPT-4.1-mini 同样未受益" | 证明**问题与模型无关，与检索范式有关** |

### 7.2 下一阶段的真正目标

> **不是"让图更全"，而是"让 LLM 更会找"。**

从：
```
GraphRAG（图质量优化）
```

到：
```
Repository Navigation Agent（搜索策略优化）
```

这是从**工程优化**到**范式创新**的跃迁。

---

*文档生成时间: 2026-05-12*
