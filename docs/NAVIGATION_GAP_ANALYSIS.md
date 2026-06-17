# 当前系统 vs 导航范式：差距分析与改进路线图

> 本文档对照你提出的 **Cognition-Navigation-Evidence 三层架构**，逐行分析现有代码的差距，并给出可落地的改进方案。

---

## 一、一句话结论

当前系统与用户期望的 Navigation Paradigm 差距主要在 **三个缺失**：

| 缺失 | 说明 | 后果 |
|------|------|------|
| **缺失 Cognition Layer** | QA Pipeline 完全没有 README/文档/架构信息的处理 | 用户问"支持哪些后端"，系统去搜代码而不是看 README |
| **缺失 Navigation Action Space** | ReAct 只有检索/扩展 action，没有 zoom-in/路径规划 action | LLM 只能不断 grep/expand，无法像人一样"先定位模块，再定位文件" |
| **缺失 Route Planning** | 所有问题一视同仁走同一 pipeline | 72% 召回噪音，信噪比极低 |

**但好消息是：图结构里已经有导航所需的大部分基础设施，只是 QA 系统没利用。**

---

## 二、当前系统已具备但未利用的基础

### 2.1 图结构已有层级 CONTAINS 关系

```
Repository → Directory → File → Function/Class/Variable
```

代码位置：`src/ingestion/graph_builder.py` 第 171-268 行

- `Repository` 节点（第 210-215 行）
- `Directory` 节点 + 层级 `CONTAINS` 边（第 218-230 行）
- `File` 节点 + `CONTAINS` 边（第 233-251 行）
- `Function`/`Class`/`Variable` 的 `CONTAINS` 边（第 254-268 行）

### 2.2 图结构已有 Module 节点（社区发现）

代码位置：`src/ingestion/graph_builder.py` 第 433-550 行

- 使用 **Louvain 算法** 对函数调用图进行社区发现
- 生成 `Module` 节点 + `BELONGS_TO` 边 + `MODULE_CALLS` 边
- 自动推断模块名称（基于共同目录，第 553-578 行）

### 2.3 已有渐进式代码展开

代码位置：`src/qa/expansion.py` + `src/qa/models.py` 第 10-15 行

```python
class ExpandLevel(Enum):
    SIGNATURE = auto()      # 仅函数签名
    BODY = auto()           # 完整函数实现
    CLASS = auto()          # 完整类
    FULL_FILE = auto()      # 整个文件
```

### 2.4 关键问题

以上基础设施花了大量代码构建（`graph_builder.py` 共 578 行），但 **QA Pipeline 只用了其中的 Function 节点和 CALLS 边**。

`Directory`、`File`、`Module`、`Repository` 节点在检索阶段**完全不被查询**。

---

## 三、具体差距分析（精确到代码行）

### 差距 1：Cognition Layer 完全缺失

**现状：**

```python
# src/qa/pipeline.py 第 64-66 行
tracer.start_phase()
functions, issues = self._initial_search(question)   # ← 直接开始检索
```

`QAPipeline.run()` 的入口就是 `Initial Search`，没有任何前置的认知层。

**缺失：**
- 没有 `READMERetriever`
- 没有 `RepoSummary` 的生成与缓存
- 没有判断"这个问题该用 README 回答还是代码回答"的逻辑

**后果（STATUS.md 中提到的典型 bad case）：**

用户问："这个项目支持哪些后端？"

系统行为：
```
grep "backend" → 找到 20+ 个单行匹配
embedding "supported backends" → 找到几个函数摘要
拼进 prompt → DeepSeek 从代码中推断后端列表
```

理想行为：
```
先看 README → "Supported backends: CPU, CUDA, Metal, Vulkan..."
直接回答
```

### 差距 2：Navigation Action Space 不匹配

**现状：**

```python
# src/qa/agent_loop.py 第 20-28 行
_ACTIONS = {
    "grep_search":     "用新的关键词进行 grep 代码搜索",
    "semantic_search": "用新的查询进行 embedding 语义搜索",
    "expand_callers":  "扩展目标函数的调用者（上游）",
    "expand_callees":  "扩展目标函数的被调用者（下游）",
    "read_class":      "读取目标函数所在类/文件的完整实现",
    "sufficient":      "信息已足够，可以生成答案",
}
```

所有 action 本质都是 **Retrieval**（检索）或 **Expansion**（扩展）。

没有 **Navigation**（导航）action：
- 没有 `zoom_module`："让我看看 backend 模块有哪些函数"
- 没有 `zoom_file`："让我看看 ggml-backend.cpp 里有哪些关键函数"
- 没有 `zoom_directory`："让我看看 ggml/ 目录结构"
- 没有 `get_repo_summary`："让我先看看仓库整体架构"

**后果：**

ReAct Loop 的决策空间被限制在"搜索更多代码"或"扩展调用链"，无法做出"我应该先去 backend 模块看看"的战略决策。

这导致 STATUS.md 中描述的：
> "DeepSeek 只能基于函数名'瞎猜'哪些值得扩展"

因为 LLM 看不到模块/目录结构，它甚至不知道"backend 模块"的存在。

### 差距 3：Retriever 不利用图层级

**现状：**

```python
# src/qa/retrievers/graph.py 第 82-94 行
def _fetch_by_keyword(self, keyword: str, limit: int) -> list[dict]:
    rows = self._run("""
        MATCH (f:Function)                        # ← 只查 Function 节点
        WHERE toLower(f.name) CONTAINS $kw
           OR toLower(f.file_path) CONTAINS $kw
        RETURN f.name AS name, ...
    """, {"kw": kw, "limit": limit})
    return rows
```

`GraphRetriever` 只查询 `Function` 节点，完全不利用：
- `Directory` 节点（目录结构）
- `Module` 节点（模块划分）
- `File` 节点（文件级聚合）
- `Repository` 节点（仓库元信息）

**更深层的问题：**

`GraphRetriever._expand_calls()`（第 97-109 行）沿 `CALLS` 边扩展，这是 **图遍历**，但不是 **导航**。

导航应该是：
```
用户问 backend 相关问题
→ 找到 backend 模块（Module 节点）
→ 查看模块内核心文件（File 节点）
→ 查看文件内函数签名（Function 节点）
→ 选择关键函数展开 body
```

而当前的 `expand_callers/callees` 是：
```
找到一个函数
→ 沿 CALLS 边上下游扩展
→ 可能跨模块跳到无关代码
```

这正是 STATUS.md 中 72% 噪音的来源之一。

### 差距 4：缺少 Route Planning（路径规划）

**现状：**

```python
# src/qa/pipeline.py 第 55-108 行
def run(self, question: str) -> QAResult:
    result = QAResult(question=question)
    # ...
    # 1. Initial Search          ← 直接检索
    # 2. ReAct Loop (optional)   ← 局部搜索
    # 3. Expansion               ← 展开代码
    # 4. Generate Answer         ← 生成答案
```

流程是线性的，没有"先理解仓库 → 判断问题层级 → 规划访问路径"的步骤。

**缺失：**

没有 `NavigationPlanner` 组件来回答：
```text
"这个问题是 L0（架构）、L1（模块）还是 L2（函数实现）？"
"如果是 L1，应该先去哪个 Module？"
"如果是 L2，应该从哪个 File 开始看？"
```

### 差距 5：Information Funnel 缺 Summary 层

**现状：**

```python
# src/qa/models.py 第 10-15 行
class ExpandLevel(Enum):
    SIGNATURE = auto()      # 仅函数签名 (~50 tokens)
    BODY = auto()           # 完整函数实现
    CLASS = auto()          # 完整类
    FULL_FILE = auto()      # 整个文件
```

从 `SIGNATURE` 直接跳到 `BODY`，中间缺少：
- **Function Summary/Annotation**：这个函数是干什么的（docstring/注释摘要）
- **Module Summary**：这个模块是干什么的
- **File Summary**：这个文件是干什么的

你提到的 RepoMaster 的 coarse-to-fine 思想：
```
Signature → Summary → Body
```

当前系统有 Signature 和 Body，但 **缺少 Summary**。

### 差距 6：ReAct Prompt 没有仓库结构上下文

**现状：**

```
# prompts/react_decide.txt
【已收集函数】(共{function_count}个，按相似度排序):
{function_list}

【相关Issue】(共{issue_count}个):
{issue_list}
```

LLM 在决策时只能看到：
- 函数名列表
- Issue 列表
- 已扩展的调用链

**看不到：**
- 仓库整体架构（README 摘要）
- 模块划分（Module 列表）
- 目录结构（Directory 层级）
- 当前所在"位置"（是在 backend 模块？还是 sampling 模块？）

**后果：** STATUS.md 明确指出
> "决策 prompt 中每个函数只显示：函数名 + 文件路径 + score + source，签名（180 字符以内），没有 body preview"

LLM 没有上下文来做"先去 backend 模块看看"的决策，因为它甚至不知道模块存在。

---

## 四、改进方案

### 阶段一：最小改动（1-2 周可落地）

目标：**在现有 Pipeline 前叠加 Navigation Layer，扩展 ReAct Action Space**。

#### 改动 1：新增 `RepoCognizer`（认知层）

```python
# src/qa/cognition.py
class RepoCognizer:
    """仓库认知层：生成并利用 Repository Summary"""
    
    def __init__(self, repo_root: str, neo4j_driver=None):
        self.repo_root = Path(repo_root)
        self.summary_path = self.repo_root / ".code_graph" / "repo_summary.json"
        self.summary = self._load_or_generate()
    
    def _generate(self) -> dict:
        """预计算仓库认知地图"""
        summary = {
            "readme_summary": self._summarize_readme(),
            "modules": self._extract_modules_from_graph(),  # 从 Neo4j Module 节点
            "top_directories": self._get_top_dirs(),
            "architecture": self._infer_architecture(),
        }
        return summary
    
    def get_relevant_summary(self, question: str) -> str:
        """根据问题返回相关认知信息"""
        # 如果问题涉及架构/概念，返回 README + Module 摘要
        # 如果问题涉及具体函数，返回空（让 Navigation Layer 处理）
        pass
```

**预计算内容（一次生成，多次使用）：**

```json
{
  "readme_summary": "llama.cpp 是一个纯 C/C++ 的 LLM 推理框架，支持多种后端...",
  "modules": [
    {"name": "mod_ggml", "description": "张量计算库，backend 抽象层", "files": ["ggml.c", "ggml-backend.c"]},
    {"name": "mod_llama", "description": "模型加载与推理主逻辑", "files": ["llama.cpp"]},
    {"name": "mod_sampling", "description": "采样策略", "files": ["sampling.cpp"]}
  ],
  "architecture": {
    "layers": ["model loading", "inference", "kv cache", "backend", "quantization", "sampling"]
  }
}
```

#### 改动 2：扩展 ReAct Action Space

```python
# src/qa/agent_loop.py
_ACTIONS = {
    # --- 原有检索类 action ---
    "grep_search":     "用新的关键词进行 grep 代码搜索",
    "semantic_search": "用新的查询进行 embedding 语义搜索",
    
    # --- 原有扩展类 action ---
    "expand_callers":  "扩展目标函数的调用者（上游）",
    "expand_callees":  "扩展目标函数的被调用者（下游）",
    "read_class":      "读取目标函数所在类/文件的完整实现",
    
    # --- 新增导航类 action ---
    "get_repo_summary": "查看仓库整体架构摘要（README、模块划分、设计理念）",
    "zoom_module":     "查看指定模块的摘要和包含的函数列表",
    "zoom_file":       "查看指定文件内的函数签名列表",
    "zoom_directory":  "查看指定目录的结构和关键文件",
    
    "sufficient":      "信息已足够，可以生成答案",
}
```

#### 改动 3：改造 Pipeline 入口，增加 Cognition Check

```python
# src/qa/pipeline.py
class QAPipeline:
    def __init__(self, ..., cognizer: RepoCognizer | None = None):
        # ...
        self.cognizer = cognizer
    
    def run(self, question: str) -> QAResult:
        result = QAResult(question=question)
        
        # ===== 新增：Cognition Layer =====
        if self.cognizer:
            repo_summary = self.cognizer.get_relevant_summary(question)
            if self._is_architecture_question(question, repo_summary):
                # L0 问题：直接用认知层信息回答
                result.answer = self._answer_from_cognition(question, repo_summary)
                return result
        
        # ===== 新增：Navigation Planning =====
        if self.cognizer:
            navigation_plan = self._plan_navigation(question, repo_summary)
            # 将 plan 注入 ReAct Loop（作为初始上下文或建议路径）
        
        # ===== 原有流程 =====
        functions, issues = self._initial_search(question)
        # ... ReAct → Expansion → Generate
```

#### 改动 4：改造 ReAct Prompt，加入仓库结构上下文

```
# prompts/react_decide.txt（改造后）

问题: {question}

【仓库架构摘要】
{repo_summary}        ← 新增

【模块划分】
{module_list}         ← 新增：从 Neo4j Module 节点

【已收集函数】(共{function_count}个):
{function_list}

【当前所在位置】      ← 新增
{current_location}    # 例如："当前在 mod_ggml 模块，ggml-backend.c 文件"
...

你是代码导航专家。请先判断问题层级，再选择行动：
- L0（架构/概念）：优先使用 get_repo_summary，或 zoom_module 定位模块
- L1（模块/文件）：使用 zoom_module / zoom_file 缩小范围
- L2（函数实现）：使用 expand_callers / expand_callees / read_class 深入细节
```

#### 改动 5：新增 Navigation Retrievers

```python
# src/qa/retrievers/repo_summary.py
class RepoSummaryRetriever(BaseRetriever):
    """检索 README/文档/架构信息"""
    def retrieve(self, question, top_k=3):
        # 返回 README 摘要、Module 摘要
        pass

# src/qa/retrievers/module.py  
class ModuleRetriever(BaseRetriever):
    """基于 Module 节点的检索"""
    def retrieve(self, question, top_k=3):
        # 查询 Neo4j 的 Module 节点，返回模块信息
        pass

# src/qa/retrievers/directory.py
class DirectoryRetriever(BaseRetriever):
    """基于目录结构的检索"""
    def retrieve(self, question, top_k=3):
        # 查询 Neo4j 的 Directory 层级
        pass
```

### 阶段二：架构升级（1 个月）

#### 目标：显式 Cognition-Navigation-Evidence 三层架构

```
┌─────────────────────────────────────────────────────────────┐
│                    COGNITION LAYER                           │
│  ┌──────────────┐  ┌─────────────────┐  ┌────────────────┐  │
│  │ RepoCognizer │  │QuestionClassifier│  │SummaryIndexer  │  │
│  │(仓库认知地图) │  │ (L0/L1/L2 判断)  │  │(预计算模块摘要) │  │
│  └──────────────┘  └─────────────────┘  └────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│                   NAVIGATION LAYER                           │
│  ┌─────────────────┐  ┌──────────────┐  ┌────────────────┐  │
│  │NavigationPlanner│  │ModuleNavigator│  │FileNavigator   │  │
│  │   (路径规划)     │  │  (模块级zoom) │  │ (文件级zoom)   │  │
│  └─────────────────┘  └──────────────┘  └────────────────┘  │
│  ┌─────────────────┐  ┌──────────────┐                       │
│  │DirectoryNavigator│  │RouteMemory   │                       │
│  │  (目录级zoom)    │  │(导航历史)    │                       │
│  └─────────────────┘  └──────────────┘                       │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│                    EVIDENCE LAYER                            │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────────────┐ │
│  │Signature     │  │Summary       │  │Body/CallChain      │ │
│  │Expander      │  │Expander(新增) │  │Expander(现有)      │ │
│  └──────────────┘  └──────────────┘  └────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

#### 信息漏斗升级（对应你提出的六层）

```
L0 README / Docs / Architecture     ← Cognition Layer
L1 Module Summary                   ← Navigation Layer  
L2 File Summary                     ← Navigation Layer
L3 Function Signature               ← Evidence Layer（现有）
L4 Function Docstring/Annotation    ← Evidence Layer（新增）
L5 Function Body                    ← Evidence Layer（现有）
```

**查询时按需加载：**
- L0 问题：只访问 L0
- L1 问题：访问 L0-L2
- L2 问题：访问 L0-L3，需要时深入 L4-L5

#### 关键新增组件

**1. `QuestionClassifier`**

判断问题层级：
```python
def classify(question: str) -> str:
    # L0: "这个项目是什么？", "支持哪些后端？", "架构是怎样的？"
    # L1: "backend 模块是怎么工作的？", "sampling 有哪些策略？"
    # L2: "ggml_backend_free 为什么泄漏？", "这个函数的调用链是什么？"
```

**2. `NavigationPlanner`**

生成访问路径：
```python
def plan(question: str, repo_summary: dict) -> NavigationPlan:
    # 返回：
    # {
    #   "target_layer": "L1",
    #   "suggested_modules": ["mod_ggml", "mod_backend"],
    #   "suggested_files": ["ggml-backend.cpp"],
    #   "reasoning": "问题涉及后端实现，建议先查看 ggml 模块"
    # }
```

**3. `RouteMemory`**

记录导航历史，防止循环：
```python
class RouteMemory:
    def __init__(self):
        self.visited_modules = set()
        self.visited_files = set()
        self.current_location = None  # "mod_ggml/ggml-backend.cpp"
```

---

## 五、预期效果

### 对 STATUS.md 中已知问题的解决

| 已知问题 | 当前根因 | 导航架构如何解决 |
|----------|---------|----------------|
| **召回噪音过高（72%）** | Grep 召回大量无关单行匹配；没有模块过滤 | Navigation Planner 先定位模块，只在相关模块内检索 |
| **ReAct prompt 缺少 body preview** | prompt 只有函数名列表 | 加入 Module/File 摘要，LLM 能在更高抽象层做决策 |
| **信噪比 > Context 大小** | 36 个函数中只有 4-5 个相关 | 先导航到正确模块，再细化，目标从 36 个减到 5-10 个 |
| **DeepSeek 只能瞎猜** | 看不到仓库结构 | 加入 Repo Summary + Module List，LLM 有全局认知 |

### 对你提出的典型场景的解决

**场景："How does Exaone inference work?"**

| 步骤 | 当前系统行为 | 导航架构行为 |
|------|------------|-----------|
| 1 | grep "Exaone" → 可能找不到或找到零散代码 | QuestionClassifier → L0/L1 |
| 2 | embedding 搜到几个相关函数 | NavigationPlanner → 建议查看 "mod_llama" 模块 |
| 3 | 拼 36 个函数进 prompt | zoom_module("mod_llama") → 看模块摘要和关键文件 |
| 4 | DeepSeek 被淹没 | zoom_file("llama.cpp") → 看文件内函数签名 |
| 5 | — | 定位到 `llama_decode` → expand_callees → 深入调度器 → backend → kernel |

---

## 六、下一步行动建议

### 本周可做（最小 PoC）

1. **生成 `repo_summary.json`**
   - 读取 `llama.cpp/README.md`
   - 从 Neo4j 提取 Module 节点列表
   - 保存到 `data/repo_summary.json`

2. **新增 `get_repo_summary` action**
   - 在 `agent_loop.py` 中加一个新 action
   - 让 ReAct 可以请求仓库摘要
   - 观察 DeepSeek 是否会主动使用

3. **改造 `react_decide.txt`**
   - 在 prompt 中加入 Module 列表（从 Neo4j 查询）
   - 加入一行："如果问题是关于架构/概念，优先使用 get_repo_summary"
   - 跑 5-10 题对比效果

### 下周可做

4. **实现 `zoom_module` action**
   - 查询 Neo4j：给定模块名，返回模块内函数签名列表
   - 这是真正的"导航"而非"检索"

5. **实现 `QuestionClassifier`**
   - 用简单的规则/LLM 判断问题层级
   - L0 问题直接利用 repo_summary 回答，不走检索

### 长期（1 个月）

6. **预计算 Module Summary / File Summary**
   - 用 LLM 为每个 Module/File 生成一句话摘要
   - 存入 Neo4j 或本地索引
   - 这是 Information Funnel 的 L1-L2 层

7. **评估指标升级**
   - 当前：结论一致率
   - 新增：导航路径质量、模块命中率、噪音降低比例
