# Repository Cognition and Evidence-Guided Navigation for Codebase Question Answering

> **分支**: `feat/navigation-architecture`  
> **核心主张**: *Answering is not retrieval. Answwering is investigation.*  
> **范式升级**: 从 Flat Chunk Retrieval → Repository Cognition → Investigation-Driven Navigation → Evidence-Guided Answer  

---

## 一、核心主张：为什么 Retrieval 范式到天花板了

### 1.1 当前系统的天花板

`docs/QA_SYSTEM_STATUS.md` 已经用数据证明：

| 指标 | 现状 | 根因 |
|------|------|------|
| 召回噪音 | **72%** | 搜索空间是"全仓库所有函数"，没有分层过滤 |
| DeepSeek 分析质量 | 被淹没 | 36 个函数中只有 4-5 个真正相关 |
| CALLS 边收益 | **+82.9% 边数，准确率几乎不变** | 起点错了，扩展越多噪音越大 |
| Module 节点收益 | **-0.9pp** | 物理目录 ≠ 认知单元，Louvain 聚类无语义 |

**结论**：继续补 CALLS 边、优化 embedding、清洗节点 = **无效投入**。天花板不在"图的质量"，而在**检索范式本身**。

### 1.2 为什么 Flat Retrieval 不适合代码仓

代码仓 ≠ 文档库。代码天然具备：
- **层级结构**：Repo → Directory → File → Class → Function
- **调用关系**：CALLS 边构成执行流
- **依赖关系**：IMPORTS / CONTAINS 构成组织流
- **抽象层级**：README（概念）→ Topic（主题）→ Function（实现）

Flat Retrieval 的假设是：
```text
问题 → 找到最相似的 chunk → 拼上下文 → 回答
```

但这个假设隐含了一个致命问题：**它把仓库当成一袋无序的代码片段**。LLM 不知道"这个仓库是什么"，也不知道"我该去哪找"。

### 1.3 人类工程师怎么做（不是检索，是调查）

老工程师面对 "Why is avatar loading failing?"：

```text
先猜：可能是网络问题 / 配置问题 / 缓存问题
  ↓
选最可能的假设：先看网络层
  ↓
导航到网络相关代码
  ↓
收集证据（代码 + 日志 + Issue）
  ↓
发现证据不支持 → 修正假设 → 转看缓存层
  ↓
找到证据支持 → 确认根因
```

这是：
```text
Investigation Planning → Evidence Collection → Plan Revision → Conclusion
```

不是：
```text
Search → Search → Search → Answer
```

### 1.4 我们的核心思想

> **Code QA should not retrieve code. Code QA should investigate repositories.**

更准确地说：

```text
传统范式:  Question → Retrieval → Answer
我们的范式: Question → Repository Cognition → Investigation Planning 
                                          → Navigation → Evidence Collection
                                          → Evidence Verification → Answer
```

关键差异：

| | Flat Retrieval | Repository Investigation |
|--|----------------|--------------------------|
| 起点 | 全仓所有 chunk | 仓库认知地图 |
| 策略 | 语义相似度排序 | **假设驱动**的层级导航 |
| LLM 角色 | 读代码 + 回答 | **调查员**：生成假设、导航取证、验证修正 |
| 信息增益 | 递减（多跳后噪音累积）| 递增（每层验证或排除假设）|
| 可解释性 | 黑盒（为什么选这些函数？）| **白盒**（假设链 + 证据链 + 导航路径）|
| 适用场景 | 局部代码问答 | 架构理解、根因分析、审计、**governance** |

---

## 二、方法概述：三层架构

```
┌──────────────────────────────────────────────────────────────────────────┐
│                     REPOSITORY COGNITION                                  │
│  ┌──────────────┐  ┌─────────────────────┐  ┌────────────────────────┐  │
│  │ Document     │  │ Repository Topic    │  │ Cognitive Map          │  │
│  │ Graph        │  │ Grounding           │  │ (Multi-Source Cache)   │  │
│  │(README/docs) │  │(文档主题，先验指导)   │  │                        │  │
│  └──────────────┘  └─────────────────────┘  └────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌──────────────────────────────────────────────────────────────────────────┐
│                  HYPOTHESIS-DRIVEN NAVIGATION                             │
│  ┌─────────────────┐  ┌──────────────┐  ┌─────────────────────────────┐  │
│  │ Investigation   │  │ Zoom-In      │  │ Zoom-Out                    │  │
│  │ Planner         │  │ (Topic→Func) │  │ (Func→Topic)                │  │
│  │(计划+目标+范围)  │  │              │  │                             │  │
│  └─────────────────┘  └──────────────┘  └─────────────────────────────┘  │
│  ┌─────────────────────────────────────────────────────────────────────┐  │
│  │ Investigation Agent (ReAct with Plan + Evidence + Revision actions)  │  │
│  └─────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌──────────────────────────────────────────────────────────────────────────┐
│                     EVIDENCE-GUIDED ANSWER                                │
│  ┌──────────────┐  ┌──────────────┐  ┌─────────────────────────────────┐ │
│  │ Evidence     │  │ Plan         │  │ Completeness                    │ │
│  │ Chain        │  │ Verification │  │ Verification                    │ │
│  │(计划+导航=    │  │(证据是否支持  │  │(是否遗漏其它计划)               │ │
│  │  证据链)     │  │  计划)       │  │                                 │ │
│  └──────────────┘  └──────────────┘  └─────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 三、Repository Cognition（认知地图）

这是整个系统的基础。不是"一个 JSON 文件"，而是一个**多源融合的认知图**。

### 3.1 Document Graph（文档图）

**核心洞察**：README 和 docs/ 不是单一文档，而是**多层级知识结构**。

```
README.md                    ← Repository Level: "这是什么？"
  ├── docs/backend.md        ← Topic Level: "Backend Architecture"
  ├── docs/gguf.md           ← Topic Level: "Model Format"
  ├── docs/server.md         ← Topic Level: "Serving Pipeline"
  └── docs/development.md    ← Topic Level: "Build & Dev"
```

**当前问题**：`RepoCognizer` 只读 `README.md` 前 2000 字，丢失了文档的层级结构。

**正确做法**：构建 Document Graph。

```
Document 节点:
  - README.md (level=REPO)
  - docs/backend.md (level=TOPIC, topic="backend")
  - docs/gguf.md (level=TOPIC, topic="model_format")
  ...

边:
  - CONTAINS: README → docs/*.md
  - DESCRIBES: docs/backend.md → Topic("backend")
  - MENTIONS: docs/backend.md → Function("ggml_backend_sched_graph_compute")
```

**这样 query "backend 怎么工作" 时**：
- 先命中 `docs/backend.md`（Topic Level）
- 再沿 DESCRIBES 边找到 Topic("backend")
- 再沿 MENTIONS 边找到具体函数

**而不是**：在全仓函数里 embedding 搜索 "backend"。

### 3.2 Repository Cognition：多源融合（Multi-Source Cognition）

**这是最关键的设计修正。**

**之前的问题**：我们曾考虑用 "Semantic Module Discovery"，即通过聚类算法"发现"逻辑模块。但这面临根本困难：代码仓天然是 overlapping community，硬聚类不成立。

**修正后的设计**：Repository Cognition **不是单一来源**，而是多源融合的：

```
Repository Cognition Sources
├── Document Topics（README/docs/Issue 中的主题）
├── Directory Structure（物理目录层级）
├── Call Graph Hubs（调用图中心节点）
├── File Summaries（文件级摘要）
└── Issue Discussions（问题讨论中的关联）
```

**Topic 只是认知来源之一，不是唯一入口，更不是硬约束。**

#### 为什么 Topic 只是来源之一

很多仓库文档质量参差不齐：
- 公司私有仓库：README 只有 "make && ./run"
- 学生项目：文档缺失
- 老项目：文档已过时

如果只依赖 Topic，这些仓库的认知地图会严重残缺。

**正确的做法是**：
- 文档丰富时，Topic 是主要认知骨架
- 文档缺失时，Directory Structure + Call Graph Hubs 提供 fallback
- 所有来源融合成统一的 Cognitive Map，Topic 是其中一层

#### 3.2.1 Topic Grounding：从已有文档中提取主题

**我们不是"发现"Topic，我们是"锚定"（Ground）已有 Topic。**

仓库本身就提供了 Topic，我们只需要提取和结构化：

| 对比 | Module（硬聚类） | Topic（已有文档锚定） |
|------|----------------|----------------------|
| 来源 | 算法"发明" | **README/docs/Issue 中本来就写着的** |
| 与函数关系 | 一对多（硬分配）| **多对多**（自然关联）|
| 人类可读性 | 需要人工校准 | 工程师本来就这么说（"这是 backend 的问题"）|
| 稳定性 | 聚类参数敏感 | 文档结构稳定 |
| 可解释性 | "模块是什么？"没有标准答案 | "Topic 来自 README 第三章节" |
| 边界 | 硬性切割 | 自然重叠 |

**但注意**：Topic 只是认知来源之一。对于没有好文档的仓库，Directory Structure 和 Call Graph Hubs 是重要 fallback。

#### 3.2.2 Topic Extraction：从多源文档中提取主题

**从三个来源提取，不需要聚类算法：**

**来源 1：文档结构（最稳定）**
```text
README.md
  ├── "Supported backends"      → Topic: "backend"
  ├── "Quantization"            → Topic: "quantization"
  ├── "Server mode"             → Topic: "serving"
  └── "Build instructions"      → Topic: "build"

docs/backend.md
  ├── "Scheduler"               → Topic: "backend_scheduler"
  └── "Device abstraction"      → Topic: "backend_device"
```

**来源 2：Issue/PR 标签（最反映实际问题）**
```text
Issue #1234: "backend crash on CUDA"    → Topic: "backend", "cuda"
Issue #5678: "sampling temperature bug" → Topic: "sampling"
```

**来源 3：函数签名/注释的关键词（最细粒度）**
```text
ggml_backend_sched_graph_compute  → Topic: "backend", "scheduler"
llama_sample_top_p                → Topic: "sampling"
```

#### 3.2.3 Topic Graph Construction（主题图构建）

**输入**：提取的 Topics + 代码实体（Function/File）  
**输出**：Topic Graph（多对多关联图）

**构建方法**（不是聚类，是对齐）：

```
Topic: "backend"
  ← DESCRIBES ── docs/backend.md
  ← MENTIONS ── Issue #1234
  ← CONTAINS ── Function: ggml_backend_sched_graph_compute
  ← CONTAINS ── Function: ggml_backend_cpu_init
  ← CONTAINS ── Function: llama_model_load (也关联 "model_loading")

Topic: "scheduler"
  ← CONTAINS ── Function: ggml_backend_sched_graph_compute (也关联 "backend")
  ← CONTAINS ── Function: ggml_backend_sched_alloc_splits
```

**关键**：函数与 Topic 是**加权多对多**，自然解决 overlapping community 问题。`llama_decode` 可以同时以 0.9 权重关联 "inference"，0.6 权重关联 "scheduler"，0.4 权重关联 "kv_cache"。

#### 3.2.4 为什么这是研究点：Topic 只是 Cognition 的一层

| 现有方法 | 缺陷 | 我们的方法 |
|---------|------|-----------|
| Directory = Module | 物理路径 ≠ 认知单元 | **Multi-Source Cognition**：文档 + 目录 + 调用图 + Issue |
| Louvain 社区发现 | 硬聚类，假设 disjoint community | **Topic 作为软关联**：加权多对多，不强制分配 |
| Semantic Module Discovery | "模块"定义不清 | **Topic Grounding**：Topic 来自已有文档，有现实锚点 |
| 纯文档主题 | 文档缺失时失效 | **多源 fallback**：文档不足时用目录/调用图补充 |

**论文叙事**：
> "传统方法将目录结构或调用图聚类作为认知单元，但面临模块定义不清、overlapping community、文档缺失时失效等问题。本文提出 **Multi-Source Repository Cognition**：以文档主题为**主要锚点**，以目录结构和调用图中心性为**补充来源**，构建多源融合的认知地图。主题与代码实体建立**加权多对多关联**（Topic-Function Alignment），通过文本匹配、结构继承和语义相似度的多信号融合确定关联强度，而非硬聚类分配。"

**真正的技术难点不是 Topic 提取，而是 Topic-Function Alignment**：
- 如何给 `process_graph` 这类命名模糊的函数关联正确的 Topic？
- 如何融合文档提及、函数名关键词、文件级继承、结构中心性等多源信号？
- 如何给关联赋予可解释的置信度？

这才是值得在论文中展开的技术贡献。

### 3.3 Cognitive Map（认知地图缓存）

预计算并缓存以下内容（一次生成，多次使用）：

```json
{
  "repository": {
    "name": "llama.cpp",
    "description": "纯 C/C++ LLM 推理框架",
    "architecture": "tokenizer → model loader → scheduler → backend executor → sampler"
  },
  "documents": [
    {"path": "README.md", "level": "REPO", "summary": "...", "topics": ["inference", "backend"]},
    {"path": "docs/backend.md", "level": "TOPIC", "topic": "backend", "summary": "..."}
  ],
  "topics": [
    {
      "id": "topic_backend",
      "name": "Backend",
      "description": "多后端调度与执行",
      "source_documents": ["docs/backend.md"],
      "related_issues": ["#1234", "#5678"],
      "entry_functions": ["ggml_backend_sched_graph_compute", "ggml_backend_cpu_init"],
      "related_functions": [
        {"name": "ggml_backend_sched_graph_compute", "relevance": 1.0},
        {"name": "ggml_backend_cpu_init", "relevance": 0.9},
        {"name": "llama_model_load", "relevance": 0.3}
      ]
    },
    {
      "id": "topic_inference",
      "name": "Inference",
      "description": "模型推理主流程",
      "source_documents": ["README.md"],
      "entry_functions": ["llama_decode", "llama_tokenize"],
      "related_functions": [
        {"name": "llama_decode", "relevance": 1.0},
        {"name": "ggml_backend_sched_graph_compute", "relevance": 0.4}
      ]
    }
  ],
  "file_summaries": {
    "llama.cpp": "模型加载、上下文管理、解码循环",
    "ggml-backend.cpp": "后端抽象、调度器实现"
  }
}
```

---

## 四、Investigation-Driven Navigation（调查驱动导航）

### 4.1 核心修正：Navigation 不是浏览器，是调查

**之前的问题**：把 Navigation 设计成像浏览器一样的 Zoom-In/Zoom-Out，虽然比 flat retrieval 好，但还不够。

**真正的人类工程师行为**：
```text
看到问题
  ↓
生成假设："可能是 A / 可能是 B / 可能是 C"
  ↓
选择最可能的假设，导航到相关 Topic
  ↓
收集证据
  ↓
证据支持假设？→ 深入 / 证据不支持？→ 换假设
```

这是 **Investigation**，不是 **Browsing**。

### 4.2 Investigation Planning（调查计划生成）

**核心设计**：面对问题，生成**调查计划**（Investigation Plan），而非自由浮动的假设。

**为什么不用"假设"（Hypothesis）这个词**：
- "假设生成"容易被 reviewer 质疑："为什么一定要生成假设？直接导航不行吗？"
- "调查计划"更工程化：计划中包含 **调查目标**、**导航方向**、**预期证据**，是行动导向的

**计划 vs 假设的区别**：
```text
假设（Hypothesis）:
"backend 调度器未正确释放资源"
→ 太抽象，reviewer 会问"凭什么这么认为？"

计划（Investigation Plan）:
"调查 backend Topic 下的资源释放路径，重点检查：
 1. scheduler 入口函数的资源分配/释放对
 2. 异常返回路径是否跳过释放
 3. RPC 服务端 cleanup 逻辑"
→ 具体、可执行、可追溯
```

```python
# src/qa/investigation.py
from dataclasses import dataclass
from typing import Literal

@dataclass
class InvestigationPlan:
    id: str
    description: str                    # "调查 backend 资源释放路径"
    # Topic 是先验指导，不是硬约束
    related_topics: list[tuple[str, float]]  # [("topic_backend", 0.8), ("topic_rpc", 0.4)]
    target_functions: list[str]         # 优先检查的函数
    search_scope: str                   # "backend" / "global"
    verification_plan: str              # 如何验证
    confidence: float                   # 初始置信度
    status: Literal["pending", "verified", "rejected", "unclear"]

class InvestigationPlanner:
    """调查计划生成器。
    
    关键设计：
    1. Topic 是先验指导（prior），不是硬约束（constraint）
    2. 计划可以跨越多个 Topic
    3. 如果仓库没有相关 Topic，计划基于目录结构 / 调用图生成
    """
    
    def generate_plans(self, question: str, 
                       cognition: RepoCognition) -> list[InvestigationPlan]:
        # Topic 是参考，不是限制
        topics = cognition.get_topics_with_relevance(question)
        
        prompt = f"""
问题: {question}

【仓库认知上下文】
{cognition.get_summary()}

【相关 Topic 及关联度】（参考，不限定）
{topics}

【目录结构】（fallback 参考）
{cognition.get_directory_tree()}

【调用图入口】（fallback 参考）
{cognition.get_entrypoints()}

请生成 2-4 个调查计划：
1. 每个计划应优先关联高相关度 Topic，但**不限于单一 Topic**
2. 说明调查范围（哪些函数/文件/Topic）
3. 说明验证方法（检查什么证据）
4. 给出初始置信度（0-1）

注意：
- 跨 Topic 的计划是允许的（如 backend + scheduler + memory）
- 如果 Topic 关联度低，可以基于目录/调用图制定计划

返回 JSON 数组：
[
  {{
    "description": "调查 backend 资源释放路径",
    "related_topics": [["topic_backend", 0.8], ["topic_scheduler", 0.5]],
    "target_functions": ["ggml_backend_sched_graph_compute", "load_all_data"],
    "search_scope": "backend",
    "verification_plan": "检查所有失败返回路径是否跳过释放",
    "confidence": 0.8
  }}
]
"""
        return call_llm_json(prompt)
```

**为什么这样设计**：
- **Topic-guided, not Topic-constrained**：Topic 提供先验方向，但计划可以跨 Topic
- **防幻觉**：计划必须关联到 cognition 中的实体（Topic/目录/函数），不能凭空编造
- **鲁棒性**：文档缺失时，目录结构和调用图提供 fallback
- **可解释**：计划的每个部分都有认知来源

### 4.3 Topic-Centric Navigation（主题中心导航）

**核心流程**：
```text
Question
  ↓
Repository Cognition（多源融合：Topic + 目录 + 调用图 + Issue）
  ↓
Investigation Planning（Topic-guided, not Topic-constrained）
  ↓
选择计划 → 确定调查范围（可跨 Topic）
  ↓
Navigation Agent 在范围内导航
  ↓
收集 Evidence
  ↓
Plan Verification（证据是否支持计划）
  ↓
验证通过 → 深入 / 验证失败 → 换下一个计划
```

**关键**：每一步都有实体对应，没有自由浮动的推断。Topic 是先验，不是牢笼。

**对应 ReAct Actions**：

```python
# 调查计划管理
"generate_plans": "基于认知上下文生成调查计划"
"select_plan": "选择一个计划执行"
"reject_plan": "排除一个计划，说明原因"
"revise_plan": "根据新证据修正当前计划"

# Topic 导航（Zoom-In）
"zoom_topic": "查看某个 Topic 的摘要和相关函数"
"zoom_file": "查看某个文件内的函数签名列表"
"zoom_function": "查看某个函数的签名和注释"
"trace_callers": "沿调用链向上追踪"
"trace_callees": "沿调用链向下追踪"

# Topic 导航（Zoom-Out）
"zoom_out_file": "查看当前函数所属文件的整体结构"
"zoom_out_topic": "查看当前函数相关的 Topic 摘要"
"zoom_out_architecture": "查看整体架构摘要"

# 证据收集
"collect_evidence": "收集当前位置的代码证据"
"check_issue": "查看与当前 Topic/函数相关的 Issue"
```

### 4.4 改造后的 ReAct Prompt

```
你是代码调查专家。你正在调查以下问题：

问题: {question}

【当前调查计划】
{current_plan}

【仓库架构】
{repo_architecture}

【相关 Topic（参考，不限定）】
{relevant_topics}

【当前位置】
{current_location}

【已收集证据】
{evidence_chain}

【导航历史】
{navigation_path}

调查原则：
1. 先 zoom_topic 了解相关 Topic 的整体结构
2. 选择计划执行，范围可以跨多个 Topic
3. 收集支持或反对计划的证据
4. 如果证据不支持当前计划，reject_plan 并换下一个，或 revise_plan
5. 不要重复访问已检查过的位置
6. 证据充分时，可以生成答案

注意：计划不是牢笼。如果导航中发现了计划外的关键线索，可以修订计划。

返回JSON:
{
    "thought": "当前调查状态和下一步计划",
    "sufficient": false,
    "action": "zoom_topic",
    "target": "topic_backend",
    "plan_id": "p1"
}
```

---

## 五、Evidence-Guided Answer（证据导向回答）

### 5.1 Investigation Plan + Evidence Chain = 可审计的推理

传统 RAG 的问题是：LLM 给了一堆函数，但**不知道这些函数是怎么被选出来的**。

我们的优势：整个调查过程就是一条**可审计的推理链**。

```text
问题: "ggml_backend_free 为什么泄漏？"

推理链:
1. [计划生成] 基于 Cognition Map 生成 3 个调查计划
   - P1: 调查 backend 资源释放路径（关联 Topic: backend, scheduler）
   - P2: 调查各后端 cleanup 路径（关联 Topic: backend）
   - P3: 调查 RPC 异常路径（关联 Topic: backend, rpc）

2. [执行 P1] zoom_topic("topic_backend") → 了解 backend 整体结构

3. [执行 P1] zoom_function("ggml_backend_sched_graph_compute") → 检查调度逻辑

4. [证据收集] trace_callees("ggml_backend_sched_graph_compute") → 发现 load_all_data

5. [证据发现] 在 load_all_data 中发现 progress_callback 取消路径未释放资源
   → 支持 P1

6. [深入验证] 检查所有失败返回路径 → 同时发现 RPC accept 失败路径也泄漏
   → 支持 P3（计划外发现，修订计划）

7. [完整性检查] 是否还有其他入口函数未检查？
   → 已覆盖所有 backend 入口，无遗漏

8. [结论] 有两个泄漏点：
   - load_all_data 的 progress_callback 取消路径
   - RPC 服务端 accept 失败路径
```

**注意第 6 步**：调查中发现了计划外的证据（支持 P3），这是允许的——计划是先验指导，不是牢笼。发现新线索时可以修订计划。

这条链可以回答：
- **为什么找到这段代码？** → 因为沿 H1 → backend Topic → 调度器 → load_all_data 追踪
- **有没有遗漏？** → 检查了所有 backend 入口，还意外发现了 H3
- **证据可信度？** → 每一层都有明确的假设和验证逻辑

### 5.2 为什么对 Governance 场景重要

传统 RAG 回答：
> "这个函数有内存泄漏。"

追问：
> "你怎么知道的？"
> "还有其他地方泄漏吗？"
> "你检查了所有可能的路径吗？"

RAG 答不上来。

Investigation Agent 回答：
> "我先生成了 3 个调查计划：
> 1. 调查 backend 资源释放路径（关联 Topics: backend, scheduler）
> 2. 调查各后端 cleanup 路径（关联 Topic: backend）
> 3. 调查 RPC 异常路径（关联 Topics: backend, rpc）
>
> 然后我执行了计划 1：
> - 进入 Topic 'backend'，检查调度器入口函数
> - 沿调用链追踪到 load_all_data
> - 发现 progress_callback 取消时跳过了所有清理代码
>
> 深入验证时意外支持了计划 3：
> - RPC 服务端 accept 失败时直接 return，未释放 backends
> - 修订计划，补充 RPC 路径检查
>
> 完整性检查：已覆盖 backend 和 rpc 相关入口，无其他遗漏。"

这是 **auditable** 的。是 **governance** 的基础。

---

## 六、与现有系统的关系

### 6.1 不推翻，叠加

```
src/qa/
├── pipeline.py                  # 现有（保留作为基线）
├── investigation_pipeline.py    # 新增（假设驱动调查 Pipeline）
│
├── cognition.py                 # 新增（RepoCognition, DocumentGraph）
├── topic_discovery.py           # 新增（RepositoryTopicDiscovery）
├── investigation.py             # 新增（InvestigationPlanner, PlanVerifier）
├── navigation.py                # 新增（NavigationPlanner, RouteMemory）
│
├── agent_loop.py                # 修改（扩展 action space）
├── prompts.py                   # 修改（新 prompt 模板）
│
└── retrievers/
    ├── base.py                  # 现有
    ├── grep.py                  # 现有
    ├── embedding.py             # 现有
    ├── graph.py                 # 现有
    ├── topic.py                 # 新增（TopicRetriever）
    ├── document.py              # 新增（DocumentGraphRetriever）
    └── directory.py             # 新增（DirectoryRetriever）
```

### 6.2 渐进式实施，随时回退

每个 Phase 都保留新旧 Pipeline 的 A/B 对比：
```python
# 旧 Pipeline（基线）
from src.qa.pipeline import QAPipeline
baseline = QAPipeline(retrievers=[...])

# 新 Pipeline（实验）
from src.qa.investigation_pipeline import InvestigationQAPipeline
experiment = InvestigationQAPipeline(retrievers=[...], cognizer=cognizer)

# 跑同一套题，对比结果
```

---

## 七、实施路线图

### Phase 1: Repository Cognition（Week 1-2）

**目标**：构建 Document Graph + Repository Topic Discovery。

| 任务 | 说明 | 产出 |
|------|------|------|
| Document Graph 构建 | 解析 README + docs/，提取章节结构、主题 | `DocumentGraph` 类 |
| Repository Topic Discovery v0 | 从文档结构提取 Topic，建立 Topic-Function 多对多关联 | `Topic` 初版 |
| RepoCognition Cache | 预计算并缓存认知地图 | `data/cognitive_map.json` |

**验收标准**：
- Document Graph 能回答 "backend 相关文档有哪些"
- Topic 数量合理（5-20 个），每个有来源文档
- Topic-Function 关联中，一个函数可挂多个 Topic

### Phase 2: Investigation-Driven Navigation（Week 3-4）

**目标**：实现调查计划生成 + Topic 中心导航。

| 任务 | 说明 | 产出 |
|------|------|------|
| InvestigationPlanner | 基于 Cognition Map 生成调查计划 | `InvestigationPlanner` |
| Topic-Centric Navigation | zoom_topic, zoom_file, trace_callers | ReAct 扩展 |
| PlanVerifier | 收集证据，判断计划是否成立 | `PlanVerifier` |
| 限定范围搜索 | search_in_topic：在特定 Topic 内搜索 | 改造 Initial Search |

**验收标准**：
- ReAct 能完成一次完整调查：`generate_plans` → `zoom_topic` → `trace_callees` → `collect_evidence`
- Topic 限定搜索的噪音 < 30%

### Phase 3: Evidence Chain + Investigation Pipeline（Week 5-6）

**目标**：让调查过程成为可审计的推理链。

| 任务 | 说明 | 产出 |
|------|------|------|
| Evidence Chain 记录 | 每步决策记录到 QAResult | `EvidenceChain` |
| 答案生成增强 | 在答案中显式包含"假设链 + 证据链" | 改造 answer_generation prompt |
| Completeness Check | 自检是否遗漏其它假设 | `CompletenessChecker` |
| Investigation Pipeline | 端到端 Pipeline | `InvestigationQAPipeline` |

**验收标准**：
- 答案中包含清晰的假设链和证据链
- Completeness Check 能指出未验证的假设

### Phase 4: 评估与迭代（Week 7-8）

**目标**：量化验证 Investigation 范式优于 Retrieval 范式。

| 指标 | 说明 | 对比基线 |
|------|------|---------|
| **Topic Hit Rate** | 第一层导航选中正确 Topic 的比例 | — |
| **Noise Ratio** | 无关函数占召回总数的比例 | 72% → 目标 < 30% |
| **Plan Accuracy** | 生成的计划中包含正确根因的比例 | — |
| **QA Accuracy** | 最终答案准确率 | ~83% → 目标 +5% |
| **Evidence Completeness** | 审计场景下证据链完整性 | — |
| **Token Efficiency** | 每题消耗的 prompt token | 目标降低 30% |

---

## 八、预期研究贡献

### 8.1 范式创新

> **从 "Code Retrieval" 到 "Repository Investigation"**

不是提出一个新的 retrieval 算法，而是提出一种新的 codebase understanding 范式：
- **Repository Cognition**：AI 先建立仓库认知地图（Topic-Centric，Topic 来自已有文档）
- **Topic-Guided Investigation Planning**：在认知约束下生成可执行的调查计划（防幻觉）
- **Investigation-Driven Navigation**：像调查员一样导航取证、验证计划
- **Evidence-Guided Answer**：假设链 + 证据链 = 可审计的推理

### 8.2 技术贡献

1. **Document Graph**：将 README/docs 融入代码图的多层级知识结构
2. **Multi-Source Repository Cognition**：融合文档主题、目录结构、调用图、Issue 的多源认知地图
3. **Topic-Function Alignment**：多信号融合（文本+结构+语义）的加权关联，替代硬聚类
4. **Topic-Guided Investigation Planning**：Topic 为先验指导（非硬约束）的调查计划生成
5. **Investigation-Driven Navigation**：计划 → 导航 → 证据 → 修订的闭环调查机制
6. **Bidirectional Navigation**：Zoom-In + Zoom-Out 的双向层级导航

### 8.3 应用场景

不仅限于"问答准确率"，更扩展到：
- **代码审计**："这个 bug 的影响范围有多大？"
- **根因分析**："为什么头像加载失败？"
- **架构理解**："新特性该加在哪个 Topic 下？"
- **代码治理**："AI 生成的代码为什么这样设计？证据链是什么？"
- **新人 onboarding**："我想理解这个仓库，该从哪个 Topic 开始？"

---

## 九、总结

| | 旧思路 | 新思路 |
|--|--------|--------|
| **核心假设** | 代码仓 = 文档袋，搜最相似的 chunk | 代码仓 = 有主题的认知空间，需要调查 |
| **认知单元** | Directory / Module（硬聚类）| **Multi-Source**（Topic + 目录 + 调用图 + Issue）|
| **LLM 角色** | 读代码 + 回答 | **调查员**：在认知约束下生成计划、导航取证、验证修正 |
| **README 角色** | 可选的外部信息 | **Document Graph** 的核心层级 |
| **导航策略** | Zoom-In 浏览器 | **假设驱动调查**（假设→导航→验证→修正）|
| **可解释性** | 黑盒（为什么选这些函数？）| **白盒**（假设链 + 证据链 + 导航路径）|
| **研究定位** | 工程优化（更好的图/更好的检索）| **范式创新**（从 Retrieval 到 Investigation）|

> **最终目标**：不是让 LLM "搜到正确答案"，而是让 LLM "在仓库多源认知的约束下，像调查员一样制定计划、导航取证、验证修订、给出可审计的回答"。
