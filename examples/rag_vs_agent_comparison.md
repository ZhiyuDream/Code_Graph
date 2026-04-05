# Graph-Agent vs Classic-RAG 对比分析报告

**评测日期**: 2026-04-02
**数据集**: QAv2 (360 题)
**评测环境**: 20 并行 workers, LLM Judge 评分

---

## 一、整体评测结果

| 指标 | Graph-Agent | Classic-RAG | 说明 |
|------|-------------|-------------|------|
| **LLM Judge 均分** | 0.3984 | 0.6517 | RAG 显著领先 |
| **胜率 (delta>0.15)** | 57 题 | 218 题 | RAG 胜率 3.8x |
| **平局 (|delta|≤0.15)** | 68 题 | — | |
| **平均 Token 消耗** | 9,179 token/题 | 777 token/题 | Agent 多消耗 **11.8x** |
| **总 Token 消耗** | 3,304,582 | 279,554 | |
| **平均工具调用步数** | 3.51 步 | — | Graph-Agent 独有 |

**结论**: 在当前评测集上，Classic-RAG 明显优于 Graph-Agent（delta = -0.2534）。Graph-Agent 用 **12 倍** 的 token 消耗却获得了更低的评分，说明其工具调用策略存在严重效率问题。

---

## 二、Graph-Agent 失败根因分析

### 核心 Bug：`annotation_json IS NOT NULL` 过滤导致永远搜不到

**这是 Graph-Agent 失败的最主要原因**，直接导致 **48.2%（105/218）** 的 RAG 优胜案例。

#### 根因验证

Neo4j 中所有 6691 个函数的 `annotation_json` 字段值均为 `NULL`（annotation 步骤从未执行或失败），但 `search_functions` 工具的查询强制要求 `annotation_json IS NOT NULL`：

```cypher
-- search_functions 主查询
WHERE toLower(f.name) CONTAINS toLower($pat)
  AND f.annotation_json IS NOT NULL   -- ← 所有函数都被这个条件过滤掉！
```

主查询返回空后，工具直接返回 "未找到"，没有任何 fallback 查询。

| 节点类型 | 总数 | 有 annotation | 比例 |
|---------|------|--------------|------|
| Function | 6691 | 0 | 0% |
| Variable | 2263 | 0 | 0% |
| Class | 473 | 0 | 0% |
| File | 316 | 0 | 0% |
| Directory | 63 | 0 | 0% |

受影响工具：`search_functions`、`search_attributes`（同样有 annotation 过滤）、`semantic_search`（依赖 annotation）。

#### 失败类型分布（218 例 RAG 优胜）

| 失败类型 | 数量 | 占比 | 说明 |
|---------|------|------|------|
| **annotation 过滤导致搜不到** | 103 | 47.2% | 函数/属性搜索返回空，本应找到 |
| 工具用了但方向错 | 38 | 17.4% | 6 步全用完仍得 0 分 |
| 过早放弃（给了错误答案） | 33 | 15.1% | 1-2 步就返回错误答案 |
| 过早放弃（直接放弃） | 24 | 11.0% | 搜不到就直接放弃 |
| 多步但答案仍不完整 | 18 | 8.3% | 工具组合不对 |
| 模块搜索（含 annotation 过滤） | 2 | 0.9% | find_module_by_keyword 含 annotation 过滤 |

#### 典型案例：Case 305 (`ggml_vbuffer_alloc`)

**问题**: 为什么在函数设计中选择当前方式传递和处理 `ggml_vbuffer_alloc` 的参数 `buft`？

| | Graph-Agent | Classic-RAG |
|--|-------------|-------------|
| **分数** | 0.0 | 0.85 |
| **工具轨迹** | `search_functions('ggml_vbuffer_alloc')` → 返回"未找到" → 1 步放弃 | — |
| **Neo4j 验证** | 函数 **确实存在**于 `ggml/src/ggml-alloc.c`，但因 annotation=NULL 被主查询过滤 | — |

该函数在 Neo4j 中存在，`CONTAINS 'ggml_vbuffer_alloc'` 能精确匹配——只是被 annotation 条件错误过滤。如果去掉 annotation 过滤或添加 fallback 查询，这次搜索本应成功。

### 失败模式详解

#### 模式 1：annotation 过滤导致搜不到（103 例，占 47.2%）

典型问题如：`ggml_vbuffer_alloc`、`gguf_set_val_u32`、`llama_sampler_greedy_apply`、`ggml_backend_dev_supports_op` 等。这些函数在 Neo4j 中确实存在，但被强制过滤。例如 Case 305 调用了 `search_functions`，主查询返回空（annotation 条件），直接得到"未找到"答案。

#### 模式 2：过早放弃（57 例，占 26.1%）

**问题**: 搜索返回空后，Agent 不尝试任何替代策略，直接给出"未找到该函数/模块"的答案。

典型案例：
- Case 0 (`ggml-blas`): `find_module_by_keyword` → 1 步放弃
- Case 11 (`concat`): `search_functions` × 2 → 2 步放弃
- Case 19 (`llama-sampler`): `search_functions` → 1 步放弃

这些名称在 Neo4j 中确实存在变体差异（`ggml-blas` vs `ggml/src/ggml-blas/`），但 Agent 缺乏同义词/路径变体重试机制。

#### 模式 3：工具用了但方向错（38 例，占 17.4%）

**问题**: Agent 执行了 6 步工具调用，但方向系统性错误，6 步全部用于错误的搜索策略。

典型：Case 244（`ggml_backend_metal_event_wait`）——工具序列 `get_function_detail → search_functions × 4 → get_directory_tree`，所有搜索都指向不存在的实体，从未反思和调整策略。

---

## 三、Agent 优于 RAG 的典型案例（5 例）

### 案例 1 — Case 40: 文件内代码元素枚举
**问题**: `ggml.c` 中包含了哪些类型的代码元素，这些元素各自的作用和意义是什么？

| | Graph-Agent | Classic-RAG |
|--|-------------|-------------|
| **分数** | 0.85 | 0.25 |
| **工具轨迹** | 10 步: `search_functions` → `get_directory_tree` × 2 → `get_file_functions` → `get_function_detail` × 5 | — |
| **AG 策略** | 先定位 ggml.c 文件，再逐个获取核心函数注解，汇总分类 | 检索结果不直接覆盖 ggml.c 文件内的代码元素 |

**AG 胜因**: Graph-Agent 利用 `get_file_functions` 获取了 `ggml.c` 中所有核心函数，再结合 `get_function_detail` 获取每个函数的注解（annotation），系统性地对文件内容进行了分类描述。RAG 的 chunk 检索虽然相关但不够聚焦。

---

### 案例 2 — Case 223: 调用关系 + 源码交叉验证
**问题**: `llm_graph_input_pos_bucket::set_input` 依赖了哪些外部资源、变量或其他函数，这些依赖是如何相互关联的？

| | Graph-Agent | Classic-RAG |
|--|-------------|-------------|
| **分数** | 0.75 | 0.0 |
| **工具轨迹** | 6 步: `get_function_detail` → `get_callees` → `search_attributes` → `search_variables` × 2 → `read_file_lines` × 2 | — |
| **AG 策略** | 先查函数定义和调用关系，再查相关变量/属性，最后读取源码行验证 | 仅给出"参考信息中未具体列出依赖" |

**AG 胜因**: Graph-Agent 使用了调用链分析工具 `get_callees` 和 `get_function_detail`，再结合 `read_file_lines` 读取了 `src/llama-graph.cpp` 中的实际函数签名和变量引用，形成了一个完整的调用关系分析。这是 RAG 的 chunk 检索难以复现的结构化推理。

---

### 案例 3 — Case 307: 调用链可达性分析
**问题**: 函数 `ggml_mul_mat_p021_f16_f32_sycl` 的参数 `stream` 在程序的哪些函数调用路径中被传递和使用？

| | Graph-Agent | Classic-RAG |
|--|-------------|-------------|
| **分数** | 0.85 | 0.25 |
| **工具轨迹** | `get_callers` → `get_callees` → `search_attributes` → `search_variables` → `search_functions` × 2 | — |
| **AG 胜因** | 通过 `get_callers` 确认该函数无上游调用者（叶子函数），并用 `search_attributes` 和 `search_variables` 追踪参数传递链路 | RAG 检索到的是相关但不同的函数（mul_mat 相关函数），未能直接回答该函数本身的调用链 |

---

### 案例 4 — Case 145: 结构体成员 + 源码交叉验证
**问题**: `llama_grammar_element` 在系统整体架构中属于哪种类型的组件，它承担了哪些核心功能和职责？

| | Graph-Agent | Classic-RAG |
|--|-------------|-------------|
| **分数** | 0.75 | 0.25 |
| **工具轨迹** | 6 步: `search_attributes` → `search_variables` → `search_functions` → `find_module_by_keyword` → `get_file_functions` → `read_file_lines` | — |
| **AG 策略** | 搜索属性/变量/函数名定位 llama-grammar 模块，读取 `llama-grammar.cpp` 源码验证结构 | 检索结果主要聚焦于 `llama_grammar` 相关函数，缺乏对 `element` 组件的具体定位 |

**AG 胜因**: Graph-Agent 的 `read_file_lines` 能够读取源文件实际内容，验证了 `llama_grammar_element` 不是独立结构体而更多是 `llama_grammar` 模块中的概念，并给出了模块的核心功能职责（语法解析/校验/合并）。

---

### 案例 5 — Case 148: 谨慎推理 + 边界探索
**问题**: `iq2_entry_t` 的基本定义是什么，它包含了哪些核心组成部分？

| | Graph-Agent | Classic-RAG |
|--|-------------|-------------|
| **分数** | 0.75 | 0.25 |
| **工具轨迹** | 6 步: `search_attributes` → `search_variables` → `search_functions` → `get_directory_tree` → `search_variables` → `read_file_lines` | — |
| **AG 胜因** | 经过多步探索后明确说明在 llama.cpp 主代码库中没有找到 `iq2_entry_t` 的定义，建议查看 `ggml-quants.c` 中的 IQ2 量化数据结构，并给出合理的存在性分析 | RAG 给出了模糊的"建议查看文件"但缺乏实质性分析 |

---

## 四、核心模式总结

### RAG 胜出的模式

| 模式 | 描述 | 原因 |
|------|------|------|
| **annotation 过滤 bug** | 所有函数 annotation=NULL，搜索工具强制过滤，导致"搜不到" | `search_functions` 等强制 `annotation_json IS NOT NULL`，但 Neo4j 中所有 annotation 都是 NULL |
| **术语微小变体** | 函数/变量名含下划线、大小写差异等 | embedding 语义检索天然鲁棒，Agent 的 `CONTAINS` 精确匹配无法处理 |
| **过长关键词** | Issue/PR 搜索用完整名称匹配失败 | Agent 缺乏"缩短关键词重试"机制，RAG 不受此影响 |
| **过早放弃** | 26% 的题目 1-2 步就返回答案 | Graph-Agent 缺乏"多策略重试"机制 |

### Graph-Agent 胜出的模式

| 模式 | 描述 | 原因 |
|------|------|------|
| **文件内元素枚举** | 列举某文件内所有函数/变量 | `get_file_functions` + `get_function_detail` 组合提供结构化覆盖 |
| **调用链分析** | 上游调用者/下游被调用者追踪 | Neo4j CALLS 边提供 RAG 无法企及的结构化关系推理 |
| **源码行号引用** | 引用特定文件和行号的代码内容 | `read_file_lines` 提供精确源码上下文 |
| **多工具协同推理** | 6 步以上多工具组合 | 当 Agent 真正进行多步探索时，分析质量显著提升 |

---

## 五、改进方向

1. **【最紧急】移除 annotation_json 过滤**：所有函数 annotation 都是 NULL，`search_functions` 等工具的 `AND f.annotation_json IS NOT NULL` 条件应删除或改为 fallback——**这是 48.2% 失败案例的直接根因**。
2. **添加 fallback 查询**：`search_functions` 主查询失败后，自动尝试不带 annotation 过滤的 fallback 查询。
3. **多策略重试机制**：单次工具返回空结果后，自动尝试缩短关键词、去除空格/下划线、尝试复数形式等替代策略。
4. **Issue/PR 关键词预处理**：`search_issues` 在精确匹配失败后，自动截取关键词的前 3-5 个词重试。
5. **最小步数保障**：对 Issue 类问题强制至少 2 步（搜索 + 获取详情），防止 1 步放弃。
6. **RAG + Agent 混合**：让 Agent 在 RAG 检索结果基础上做进一步工具调用验证，或对 RAG 检索结果不理想时自动降级到 Graph-Agent。

---

## 附录：评测数据

- 评测集: `results/qav2_20260402_191755.csv` (360 题)
- Graph-Agent 结果: `results/graph_agent_20260402_200914.json`
- Classic-RAG 结果: `results/classic_rag_20260402_191755.json`
- Judge 结果: `results/judge_20260402_200914.json`
- 评测脚本: `experiments/parallel_runner.py`
- Graph-Agent 实现: `tools/agent_qa.py` (14 工具, MAX_STEPS=6)
- Classic-RAG 实现: `tools/classic_rag.py` (embedding index, TOP_K=6)
