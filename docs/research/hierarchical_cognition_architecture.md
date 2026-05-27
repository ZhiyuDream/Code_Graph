# 双源层级认知体系：代码仓认知金字塔

**日期**: 2026-05-12 | **版本**: v2（基于导师反馈升级）

---

## 一、核心认知跃迁：从"单层检索"到"认知金字塔"

### 1.1 导师反馈的关键洞察

> **"README 是外部先验（top-down prior），不是从代码结构生长出来的认知。两者必须共存，但角色不同。"**

| 维度 | Top-down（README/docs） | Bottom-up（代码结构） |
|------|------------------------|---------------------|
| 来源 | 人类编写的外部文档 | 从代码自动抽象 |
| 特点 | 有全局目标感，但可能过时/不完整 | 精确反映真实结构，但缺乏"为什么" |
| 角色 | **校正（alignment）** | **基础认知层** |
| 用途 | 给检索提供全局导航 prior | 提供精确的调用关系和流程 |

**关键结论**：
- ❌ README **不能替代** bottom-up 摘要
- ❌ Bottom-up **不能替代** README 的全局视角
- ✅ 正确做法：**双源融合**——bottom-up 生成认知，README 做对齐校正

---

## 二、六层架构：代码仓认知金字塔

```
Layer 6: Navigation & Retrieval Layer（导航检索层）
    ↑ 查询路由：宽 query → Module → File → Function
    ↑              具体 query → Function / Grep
    ↑
Layer 5: Repository Cognitive Layer（仓库认知层）
    ↑ "这是 transformer inference engine，核心流程是 tokenize → schedule → execute"
    ↑
Layer 4: Module Semantic Layer（模块认知层）
    ↑ "backend 模块负责多后端调度，上游是 scheduler，下游是 device drivers"
    ↑
Layer 3: File Semantic Layer（文件认知层）
    ↑ "backend_scheduler.cpp 负责请求分发和负载均衡"
    ↑
Layer 2: Function Semantic Layer（函数认知层）
    ↑ "schedule_graph() 是调度入口，被 inference loop 调用，负责将计算图分配到各后端"
    ↑
Layer 1: Structural Graph Layer（结构层/ Ground Truth）
    ↑ Function / Class / File / Directory / CALLS / CONTAINS / MENTIONS
    ↑
Layer 0: Raw Repository Layer（原始输入）
    Code + README + docs + Issues + PRs
```

---

## 三、各层详细设计

### Layer 0: Raw Repository Layer

原始知识源，不做任何处理：
- Source code（C/C++/Python）
- README.md / docs/
- GitHub Issues / PRs
- Commit messages

### Layer 1: Structural Graph Layer（Ground Truth Layer）

**原则**：尽量 deterministic，不让 LLM 幻觉。

**节点**：
```
Repository → Directory → File → Class → Function → Variable
Document → Issue → PR
```

**边**：
```
CALLS, DEFINED_IN, BELONGS_TO, CONTAINS, IMPORTS, INHERITS
MENTIONS, REFERENCES, DESCRIBES
```

**当前状态**：✅ 基本完成（Neo4j 中已有 Function/CALLS/CONTAINS/MENTIONS）

---

### Layer 2: Function Semantic Layer（第一层抽象）

**这是所有 abstraction 的基础，必须先做。**

#### 2.1 选 Representative Functions

不做全部 15,143 个函数，只做**有代表性的**：

| 标准 | 理由 | 筛选方式 |
|------|------|----------|
| **高 PageRank** | 图中心节点，信息汇聚点 | Neo4j `gds.pageRank` 或 in-degree/out-degree 综合 |
| **入口函数** | 流程起点 | 无 CALLS 入边 或 caller 很少 |
| **Dispatch / Init / Handler** | 控制流关键节点 | 函数名包含 `dispatch`, `init`, `handle`, `register`, `forward` |
| **每个 Module 的代表** | 确保模块覆盖 | 每个 Module/Directory 取度数最高的 3-5 个函数 |

**预估数量**：~500-1000 个 representative functions（占总数的 3-7%）

#### 2.2 输入（不是只看代码）

```
Function body
+ Signature
+ Docstring
+ Top callers（前 5 个调用者）
+ Top callees（前 5 个被调用者）
+ 所在文件
+ 所在模块
+ Issue mentions
```

#### 2.3 输出（结构化 annotation，不是自由文本）

```json
{
  "purpose": "该函数的核心职责",
  "pipeline_role": "在整体流程中扮演什么角色（如：初始化、调度、执行、清理）",
  "upstream_dependencies": "依赖什么前置条件/数据",
  "downstream_effects": "产生什么副作用/输出",
  "usage_pattern": "通常在哪些场景被调用（如：inference loop, model init, error handling）",
  "importance": "critical / important / utility",
  "semantic_tags": ["scheduling", "memory", "backend", "tokenization"]
}
```

**为什么用结构化 JSON**：
- 后续 File/Module 层的聚合需要结构化信息
- 自由文本无法做逻辑推理（如"这个文件有多少调度相关函数"）

---

### Layer 3: File Semantic Layer

**Bottom-up 聚合**

#### 3.1 输入
```
Representative function annotations（Layer 2）
+ Import/include relationships
+ File-level call statistics（该文件的函数被谁调用、调用谁）
```

#### 3.2 输出
```json
{
  "file_role": "该文件在系统中的角色",
  "main_responsibility": "主要职责",
  "major_components": ["component1", "component2"],
  "pipeline_stage": "属于哪个流程阶段（如：tokenization / scheduling / execution）",
  "interacts_with": ["file1.cpp", "file2.cpp"]
}
```

---

### Layer 4: Module Semantic Layer

**逻辑模块，不等于 directory**

#### 4.1 模块定义

Module 应该是 **logical cluster**：
- 调用密度聚类（call-density clustering）
- 语义聚类（semantic clustering，基于 function annotation 的 tags）
- 社区发现（Louvain，但需人工校准）

#### 4.2 输入
```
File summaries（Layer 3）
+ Cross-file call graph
+ README fragments（对齐校正）
+ Issue discussions
```

#### 4.3 输出
```json
{
  "module_role": "该模块在系统中的角色",
  "system_position": "位于架构的哪个位置（如：core engine / backend adapter / utility）",
  "upstream_modules": ["module1", "module2"],
  "downstream_modules": ["module3"],
  "entry_points": ["func1", "func2"],
  "key_flows": ["flow1: init → schedule → execute"]
}
```

---

### Layer 5: Repository Cognitive Layer

**相当于"AI 读完 README 后的全局理解"**

#### 5.1 输入
```
README
+ docs/
+ Module summaries（Layer 4）
+ Top-level graph topology
```

#### 5.2 输出
```json
{
  "repository_type": "transformer inference engine",
  "core_architecture": "tokenizer → model loader → scheduler → backend executor",
  "major_pipelines": [
    "inference: tokenize → context build → decode → output",
    "quantization: load fp16 → quantize → save gguf"
  ],
  "execution_flow": ["main() → llama_init() → llama_decode() → output"],
  "main_entrypoints": ["main", "llama_server_main", "llama_cli_main"],
  "critical_subsystems": ["ggml backend", "KV cache", "tokenizer"]
}
```

#### 5.3 README 的角色：对齐校正

**不是用 README 直接生成 Module 认知，而是用 README 校正 bottom-up 生成的认知。**

例如：
- Bottom-up 认为 "ggml-scheduler" 是一个独立模块
- README 说 "scheduling 是 backend 的一部分"
- **校正**：合并 scheduler 到 backend 模块

---

### Layer 6: Navigation & Retrieval Layer

**真正做 retrieval 的层，不是 flat 检索，而是层级导航。**

#### 6.1 不同 query 类型的导航路径

**宽泛 query**（如 "How does inference work?"）
```
Repo Layer → Module Layer → File Layer → Function Layer
```

**符号 query**（如 "Where is MAX_FUSED_ADDS defined?"）
```
Grep → Function Layer（直接定位）
```

**流程 query**（如 "How does data enter backend execution?"）
```
Module Layer → Entry Point → Call Chain Expansion
```

#### 6.2 和传统 RAG 的本质区别

| | 传统 RAG | Repository Navigation |
|--|---------|----------------------|
| 检索单元 | 扁平 chunk | 层级节点（Repo/Module/File/Func）|
| 扩展策略 | 沿边扩展（CALLS）| 沿层级 zoom in / zoom out |
| LLM 角色 | 读代码 + 回答 | **导航决策者** + 读代码 + 回答 |
| 信息增益 | 递减（多跳后噪声累积）| 递增（每层更聚焦）|

---

## 四、构建顺序（导师建议）

### Step 1（现在最重要）：Function Semantic Layer

**理由**：这是所有 abstraction 的基础。

**具体工作**：
1. 从 Neo4j 选 representative functions（~500-1000 个）
2. 设计 structured annotation prompt
3. 用 LLM 批量生成 annotation
4. 存储到 Neo4j（`Function.annotation` JSON 字段）

### Step 2：File Semantic Layer

**具体工作**：
1. 聚合每个文件内的 representative function annotations
2. 生成 file-level summary
3. 存储到 Neo4j（`File.summary` JSON 字段）

### Step 3：Module Semantic Layer

**具体工作**：
1. 基于 call-density + semantic clustering 定义 logical modules
2. 聚合 file summaries 生成 module-level summary
3. 存储到 Neo4j（`Module.summary` JSON 字段）

### Step 4：Repository Cognitive Layer + README 对齐

**具体工作**：
1. 解析 README/docs，提取章节结构
2. 生成 repository-level summary
3. **用 README 校正 bottom-up 生成的 module summaries**
4. 建立 `Document → Module/Function` 关系

### Step 5：Navigation & Retrieval Layer

**具体工作**：
1. 实现层级导航 Agent
2. AB 对比实验：flat RAG vs 层级导航

---

## 五、和 ZoomRAG 的关联

ZoomRAG 核心：**"全局 → 局部 → 更局部"**

Repository Navigation 核心：
```
Repo → Module → File → Function → Statement
```

**天然匹配**。

ZoomRAG 的"全局摘要" = Layer 5（Repository Cognitive）+ Layer 4（Module Semantic）
ZoomRAG 的"局部细节" = Layer 2（Function Semantic）+ Layer 1（Structural Graph）

---

## 六、论文叙事升级

### 旧叙事（已验证为工程优化）

> "本文提出 GraphRAG，通过构建代码调用图提升检索质量..."

### 新叙事（范式创新）

> "传统代码 RAG 采用扁平化检索，而代码仓天然具备层级结构，且人类工程师依赖'README → 目录 → 模块 → 入口 → 细节'的认知路径。本文提出**双源层级认知体系**：
> 1. **Bottom-up structural abstraction**：从代码结构自底向上聚合 Function → File → Module 的语义摘要；
> 2. **Top-down semantic prior**：利用 README/docs 提供全局认知先验；
> 3. **Hierarchical Navigation**：让 LLM 像人类工程师一样逐层缩小搜索空间，而非全仓扁平召回。
> 实验表明，该体系在 xxx 代码库上显著降低了检索噪声，QA 准确率从 yy% 提升到 zz%。"

---

## 七、总结

| 问题 | 旧思路 | 新思路 |
|------|--------|--------|
| 摘要来源 | 直接看函数名生成 Directory summary | **Function → File → Module 自底向上聚合** |
| Function summary | 只看代码 | **代码 + graph neighborhood + 流程角色** |
| 高层抽象 | 硬猜 | **README 给 prior，代码给 structure，双源融合** |
| README 角色 | 直接生成模块认知 | **校正（alignment）bottom-up 认知** |
| Module 定义 | = Directory | **= Logical cluster（调用密度 + 语义聚类）** |
| 检索范式 | Flat chunk retrieval | **Hierarchical navigation（zoom in / zoom out）** |

**真正的升级**：从"如何让图更全"到"**如何让 AI 像工程师一样理解代码仓**"。

---

*文档生成时间: 2026-05-12*
