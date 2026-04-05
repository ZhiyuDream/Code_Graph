# Graph-Agent 失败案例深度分析

**评测日期**: 2026-04-03
**数据集**: QAv2 (360 题)
**Graph-Agent 结果**: `results/graph_agent_20260403_102930.json`（annotation bug 修复后）

---

## 一、整体结果

修复 `annotation_json IS NOT NULL` bug 后：

| 指标 | 修复前 | 修复后 |
|------|--------|--------|
| Graph-Agent LLM-Judge | 0.3984 | **0.4808** (+0.08) |
| Delta vs RAG | -0.2534 | **-0.1450** (+0.11) |
| Graph-Agent 胜 | 57 题 | **88 题** |
| RAG 胜 | 218 题 | **180 题** |

仍有 **180 题** RAG 优胜，下面逐类分析根因。

---

## 二、失败分类概览

修复后的 180 例 RAG 优胜可分为四大类：

| 失败类型 | 数量 | 占比 | 核心问题 |
|---------|------|------|---------|
| **搜到了但放弃** | 92 | 51.1% | 关键词与 Neo4j 实体名称不匹配，搜索返回空后直接放弃 |
| **多步但仍输** | 57 | 31.7% | 工具找到了信息，但回答质量不如 RAG |
| **过早放弃** | 20 | 11.1% | 仅用 1-2 个工具就给答案 |
| **搜到了但答案错** | 11 | 6.1% | 工具返回了有用信息，但 Agent 理解和总结能力不足 |

---

## 三、失败类型详解

### 类型 1：搜到了但放弃（92 例，占 51.1%）

**定义**: Agent 调用了工具，工具返回"未找到"，Agent 直接放弃。但实际上 RAG 检索到了相关信息（说明相关内容存在于代码库中，只是 Agent 的搜索方式没有匹配到）。

#### 1.1 部分名称变体（43 例，最主要）

**模式**: 搜索关键词与 Neo4j 中的实际名称存在局部差异，导致 `CONTAINS` 匹配失败。

**典型案例**:

**Case 156 — `iq2_entry_t` 类型成员搜索**

| | 内容 |
|--|------|
| **问题** | `iq2_entry_t 被设计为当前的抽象层级或实体，它在整体系统架构中的核心作用和目标是什么？` |
| **Agent 工具轨迹** | `search_attributes('iq2_entry_t')` → 返回空 → `search_variables('iq2_entry_t')` → 返回空 → `search_functions('iq2_entry_t')` → 返回空 → 放弃 |
| **Agent 答案** | "未找到名称包含 'iq2_entry_t' 的 Class 成员" |
| **RAG 答案** | "代码库中提及多个与 IQ2 量化相关的函数，它们涉及量化初始化、索引映射、内存释放等" |
| **根因** | `iq2_entry_t` 是 C 语言 `struct` 的类型名，在 Neo4j 中不存在 `iq2_entry_t` 类型的 Attribute 节点，但相关 IQ2 量化函数存在于代码库中。RAG 的 embedding 检索能通过相关函数找到，Agent 只做精确名称匹配就错过了 |

**Case 99 — `GGML_FA_TILE_Q` 宏搜索**

| | 内容 |
|--|------|
| **问题** | `GGML_FA_TILE_Q 的定义、解析和应用流程是如何设计和实现的？` |
| **Agent 工具轨迹** | `search_variables('GGML_FA_TILE_Q')` → 返回空 → 放弃 |
| **Agent 答案** | "没有找到名为 GGML_FA_TILE_Q 的变量" |
| **RAG 答案** | "虽然没有直接涉及 GGML_FA_TILE_Q 这个宏的具体定义，但可以结合相关函数（如 ggml_new_graph）..." |
| **根因** | `GGML_FA_TILE_Q` 是 `#define` 预处理器宏，在 Neo4j 中不是 Variable 节点，但 RAG embedding 检索能覆盖包含该宏的相关代码上下文 |

**Case 82 — `GGML_DEFAULT_GRAPH_SIZE` 宏搜索**

| | 内容 |
|--|------|
| **问题** | `为什么在系统设计中选择使用 GGML_DEFAULT_GRAPH_SIZE 来实现特定功能或控制逻辑？` |
| **Agent 工具轨迹** | `search_variables('GGML_DEFAULT_GRAPH_SIZE')` → 返回空 → 放弃 |
| **RAG 答案** | "虽然没有直接提到 GGML_DEFAULT_GRAPH_SIZE 的具体设计细节，但可以结合相关函数..." |
| **根因** | 同上，预处理器宏不在 Neo4j Variable 节点中 |

#### 1.2 类型名不在 Attribute 节点中（21 例）

**模式**: 以 `_t` 结尾的类型名（如 `ggml_backend_metal_reg_t`、`ggml_gallocr_t`）被当作 Class 成员（Attribute）搜索，但这些类型是 `struct` 定义，不是 Neo4j 中的 Attribute 节点。

**典型案例**:

**Case 157 / 159 — `ggml_backend_metal_reg_t`**

| | 内容 |
|--|------|
| **问题** | `为什么 ggml_backend_metal_reg_t 的设计选择了当前的抽象层次和成员分布？` |
| **Agent 工具轨迹** | `search_attributes('ggml_backend_metal_reg_t')` × 2 → 均返回空 → 放弃 |
| **RAG 答案** | "参考信息中并未涉及 ggml_backend_metal_reg_t 的具体结构..."（RAG 同样没直接找到，但给了更合理的分析） |
| **根因** | `ggml_backend_metal_reg_t` 是 `struct` 类型定义，不是 Attribute 节点。Agent 搜索 Attribute 就返回空，而 RAG 的 embedding 检索能发现相关函数并给出上下文 |

**Case 164 — `ggml_gallocr_t`**

| | 内容 |
|--|------|
| **问题** | `在哪里可以找到对 ggml_gallocr_t 的成员变量进行赋值或修改的代码位置？` |
| **Agent 工具轨迹** | `search_attributes('ggml_gallocr_t')` → 返回空 → `search_variables('ggml_gallocr_t')` → 返回空 → 放弃 |
| **根因** | 同上。`ggml_gallocr_t` 是结构体类型，不是 Neo4j 中的 Variable 或 Attribute |

#### 1.3 模块路径命名变体（4 例）

**模式**: 问题中的模块名含连字符（`-`），但 Neo4j 中目录路径用斜杠（`/`）。

**典型案例**:

**Case 0 — `ggml-blas`**

| | 内容 |
|--|------|
| **问题** | `这个 ggml-blas 主要包含哪些核心功能或组件，它们之间是如何组织和协作的？` |
| **Agent 工具轨迹** | `find_module_by_keyword('ggml-blas')` → "未找到与 'ggml-blas' 相关的目录或文件" → 放弃 |
| **RAG 答案** | "ggml-blas 主要核心功能集中在矩阵乘法相关的高性能计算。具体包括 gemm_bloc..." |
| **根因** | 实际目录路径是 `ggml/src/ggml-blas/`（不含前缀），`find_module_by_keyword` 的 `CONTAINS 'ggml-blas'` 无法匹配到路径中的目录名 |

**Case 28 — `ggml-cann`**

| | 内容 |
|--|------|
| **Agent 工具轨迹** | `find_module_by_keyword('ggml-cann')` → 返回空 → 放弃 |
| **根因** | 同上，实际路径应为含 `ggml-cann` 的子路径 |

#### 1.4 Issue/PR 关键词过长（3 例）

**模式**: Issue 搜索用完整问句作为关键词，过于具体导致匹配失败。

**典型案例**:

**Case 15 — `speculative design architecture performance`**

| | 内容 |
|--|------|
| **问题** | `为什么在设计 speculative 时选择了当前的架构或实现方式，这对系统的性能有哪些具体影响？` |
| **Agent 工具轨迹** | `search_issues('speculative design architecture performance')` → 返回空 → `search_issues('speculative')` → 返回空 → 放弃 |
| **RAG 答案** | "llama.cpp 中的 speculative（推测性解码）..."（RAG 检索到了相关 Issue/PR 的 embedding） |
| **根因** | Agent 第一次用完整问句搜索失败后缩短了关键词，但仍然搜不到（因为 speculative 是通用技术名称，Issue 可能用其他描述方式） |

---

### 类型 2：多步但仍输（57 例，占 31.7%）

**定义**: Agent 调用了 3 步以上工具，找到了相关函数或文件，但最终答案质量仍不如 RAG。

**核心根因**: **Agent 工具返回的信息缺乏语义描述**。工具只能返回函数名、文件路径、fan_in/out 等结构化数据，不能返回函数的自然语言描述（annotation）。而 RAG 的 embedding 检索能找到函数的摘要信息。

**典型案例**:

**Case 248 — `llama_sampler_typical_name`**

| | 内容 |
|--|------|
| **问题** | `如何通过具体步骤实现 llama_sampler_typical_name 中的核心算法逻辑？` |
| **Agent 工具轨迹** | `search_functions` → 找到函数 → `get_function_detail` → 获取详情 → `read_file_lines` × 4 → 读了 4 次源码 |
| **Agent 分数** | 0.25 |
| **RAG 分数** | 1.0 |
| **Agent 答案** | "llama_sampler_typical_name 所代表的采样核心算法逻辑并没有直接给出详细..." |
| **RAG 答案** | "`llama_sampler_typical_name` 函数的功能是返回一个固定的字符串名称，用于标识'typical'采样方法..." |
| **根因** | Agent 读了 `llama-sampler.cpp` 的多行源码，但没有提取出函数的核心功能描述。RAG embedding 检索到的 chunk 恰好包含了函数的摘要注释。`get_function_detail` 返回的 `annotation` 字段为 NULL（未 annotate），所以 Agent 只能读原始代码，但 LLM 从原始代码中提炼关键信息的质量不如 RAG 检索到的精炼 chunk |

**Case 251 — `ggml_backend_cpu_device_get_memory`**

| | 内容 |
|--|------|
| **问题** | `如何利用相关的API或框架支持来实现 ggml_backend_cpu_device_get_memory 的功能和处理流程？` |
| **Agent 工具轨迹** | `search_functions` → 找到 → `get_function_detail` → 获取 → `read_file_lines` × 4 → 读了 4 次 |
| **Agent 分数** | 0.30 |
| **RAG 分数** | 0.85 |
| **Agent 答案** | "函数定义在 ggml/src/ggml-cpu/ggml-cpu.cpp 中，但函数具体实现内容未在当前检索代码范围内展示" |
| **RAG 答案** | "`ggml_backend_cpu_device_get_memory` 的作用是'检索CPU设备的总物理内存和可用内存'..." |
| **根因** | 同样的问题：Agent 工具返回的 `annotation` 为 NULL，Agent 读了源码但不知道该提取什么信息来回答"如何实现"类问题 |

**Case 184 — `gguf_set_val_u32`**

| | 内容 |
|--|------|
| **问题** | `gguf_set_val_u32 的输入、输出和内部状态的作用机制是什么？` |
| **Agent 工具轨迹** | `search_functions` → 找到 → `get_function_detail` → → `read_file_lines` × 4 |
| **Agent 分数** | 0.30 |
| **RAG 分数** | 0.80 |
| **Agent 答案** | "结合 gguf.cpp 文件内容，分析总结..."（读了很多行但回答不聚焦） |
| **RAG 答案** | "函数位于 ggml/src/gguf.cpp，用于在 GGUF 上下文中设置一个 32 位无符号整数的 key-value 对" |
| **根因** | 同样，Agent 读了源码但总结能力有限，RAG 的 embedding chunk 恰好包含函数功能描述 |

---

### 类型 3：过早放弃（20 例，占 11.1%）

**定义**: Agent 仅用 1-2 个工具就给答案，没有充分利用多步探索。

**典型案例**:

**Case 14 — `llama-quant` 模块**

| | 内容 |
|--|------|
| **问题** | `为什么在系统设计中选择将特定功能归纳到 llama-quant？` |
| **Agent 工具轨迹** | `get_module_overview('llama-quant')` → 返回空 → 直接放弃 |
| **Agent 答案** | "llama.cpp 代码库中不存在名为 llama-quant 的模块" |
| **RAG 答案** | "将特定功能归纳到 llama-quant 模块，主要是为了将模型的量化相关逻辑集中管理..." |
| **根因** | `llama-quant` 不是目录名，但 RAG embedding 检索能找到相关函数。Agent 应该尝试 `find_module_by_keyword('quant')` 等替代策略 |

**Case 18 — `llama-graph` 模块**

| | 内容 |
|--|------|
| **问题** | `数据或控制流从哪些位置进入和离开 llama-graph？` |
| **Agent 工具轨迹** | `get_module_overview('llama-graph')` → 返回空 → 直接放弃 |
| **RAG 答案** | "llama-graph 的构建主要集中在函数 `llm_build_llama<true>::llm_build_llama`..." |
| **根因** | `llama-graph` 不是目录（实际是 `llama-graph.cpp` 源文件），Agent 没有尝试 `find_module_by_keyword` 或 `search_functions` |

---

### 类型 4：搜到了但答案错（11 例，占 6.1%）

**定义**: 工具返回了有用信息，但 Agent 的理解和总结出现了方向性错误。

**典型案例**:

**Case 13 — HTTP 设计问题**

| | 内容 |
|--|------|
| **问题** | `为什么选择以当前方式划分和设计 http？` |
| **Agent 工具轨迹** | `search_issues('http design goals')` × 2 → 均返回空 |
| **Agent 答案** | "llama.cpp 是一个轻量级开源项目，主要关注本地推理，并未涉及 HTTP 协议的设计" |
| **RAG 答案** | "参考信息主要涉及 cpp-httplib 中 HTTP 请求的处理与发送函数..." |
| **根因** | Agent 用"HTTP design goals"搜 Issue 失败后，直接根据"HTTP 不在 llama.cpp 核心功能中"推理，而没有尝试其他搜索方式（RAG 则找到了 `cpp-httplib` 相关函数） |

**Case 279 — `llama_grammar_element.type`**

| | 内容 |
|--|------|
| **问题** | `llama_grammar_element.type 在系统架构中的角色？` |
| **Agent 工具轨迹** | `search_attributes` → 返回空 |
| **Agent 答案** | "未直接找到名为 llama_grammar_element.type 的成员或变量定义" |
| **RAG 答案** | "llama_grammar_element.type 作为语法元素的类型字段，其生命周期管理设计..." |
| **根因** | Agent 搜不到就直接说"未找到"，但 RAG embedding 检索到了相关的上下文信息 |

---

## 四、失败模式全景图

```
180 例 RAG 优胜的根因分解：

关键词不匹配 (关键词与Neo4j实体名称不一致)
├── 部分名称变体（partial name） 43
│   例: "iq2_entry_t" → 不在 Attribute 节点
│   例: "ggml_backend_sycl_context::new_pool_for_host" → 方法名不够精确
├── 类型不在 Attribute 节点  21
│   例: "ggml_backend_metal_reg_t", "ggml_gallocr_t"
│   这些是 struct 类型，但 Agent 搜索 Attribute/Variable 节点
├── 模块路径变体  4
│   例: "ggml-blas" vs "ggml/src/ggml-blas/"
├── 宏/常量不在 Variable 节点  4
│   例: "GGML_DEFAULT_GRAPH_SIZE", "GGML_FA_TILE_Q", "SYCL_CHECK"
└── Issue 关键词过长  3
    例: "speculative design architecture performance"

工具信息不足 (工具找到了但回答质量差)
├── annotation=NULL，工具只返回函数名/文件  57
│   Agent 读源码但不知道该提炼什么
└── 工具组合策略不对
    例: 读了位置不对的源码行

过早放弃 (策略问题)
└── 1-2步就给答案  20
    搜索返回空后没有尝试替代策略

理解/总结能力问题
└── 搜到了但理解错  11
    工具返回了信息，但 Agent 总结方向错误
```

---

## 五、改进建议（按优先级）

### P0 — 最紧急

**1. 添加工具结果 fallback 机制（解决 43+21 例）**

当 `search_attributes` 返回空时，自动 fallback 到 `search_variables` 和 `search_functions`：
- `iq2_entry_t` 搜 Attribute 为空 → 自动搜 Variable → 再搜 Function
- `ggml_gallocr_t` 搜 Variable 为空 → 搜 `ggml_gallocr` 子串

**2. 类型名智能路由（解决 21 例）**

对于以 `_t` 结尾的名称，自动同时搜索 Function 和 Variable，不只搜 Attribute。

**3. 宏/常量名 fallback 到 Issue/函数搜索（解决 4+3 例）**

`GGML_DEFAULT_GRAPH_SIZE` 搜 Variable 为空 → 搜包含该宏名的函数或 Issue。

### P1 — 高优先级

**4. annotation 补录（解决 57 例的根本）**

当前所有函数的 annotation=NULL，这是"多步但仍输"57 例的根因。补录后 `get_function_detail` 就能返回函数描述，大幅提升 Agent 答案质量。

**5. Issue 关键词预处理（解决 3+? 例）**

`search_issues` 调用前，自动将超长关键词截取前 3-5 个实义词重试。

**6. 最小探索步数保障（解决 20 例）**

对于 Issue 类问题，强制至少 2 步（搜索 + 获取详情或替代搜索）；对于模块类问题，至少尝试 2 种搜索方式（`find_module_by_keyword` + `search_functions`）。

### P2 — 中期改进

**7. 相关概念检索**

当精确名称搜索失败时，自动触发语义相关搜索。例如 `iq2_entry_t` 搜不到 → 自动搜 `iq2` 相关的函数名 → 用找到的函数回答问题。

**8. RAG + Agent 混合架构**

用 RAG 检索结果作为 Agent 的初始上下文，弥补 Neo4j 覆盖不足的问题。

---

## 附录：评测数据

- Graph-Agent 结果: `results/graph_agent_20260403_102930.json`
- Classic-RAG 结果: `results/classic_rag_20260402_191755.json`
- Judge 结果: `results/judge_20260403_102930.json`
- 评测集: `results/qav2_20260403_102930.csv`
