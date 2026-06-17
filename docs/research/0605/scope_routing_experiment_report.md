# Scope Routing Experiment Report — Document-Based Search Space Reduction

> **Date**: 2025-06-05  
> **Branch**: `feat/navigation-architecture`  
> **Experiment**: V0 — README/docs Section → BM25 → File Scope  
> **Benchmark**: `datasets/posthoc_audit_benchmark_v2.json` (50 items, post-hoc audit QA)

---

## 1. 实验设计

### 1.1 核心假设

> 仓库文档（README + docs/）的 section 结构可以作为"认知路由层"，将搜索空间从全仓几千个文件压缩到几十个相关文件，同时不丢失正确答案。

### 1.2 实现方法（极简 V0）

```
Question
  ↓
BM25 检索 README/docs section（top-3）
  ↓
从 section 内容提取文件引用（正则匹配 *.cpp/*.h）
  ↓
section 标题关键词匹配文件路径
  ↓
合并文件列表（≤20 个）
  ↓
对比：正确文件是否在范围内
```

**关键技术选择**：
- 不切词权重优化，直接 BM25
- 不用 embedding（避免 API 成本）
- 不用 LLM（验证"纯规则是否有效"）
- 文档按 H2/H3 切 section，每个 section = 一个 DocumentUnit

### 1.3 评估指标

| 指标 | 说明 |
|------|------|
| File Hit Rate | 正确文件被 scope 覆盖的比例（1.0 = 全部覆盖） |
| Scope Size | scope 中包含的文件数 |
| Build Pollution | scope 是否包含 build/ 目录下的编译生成文件 |

---

## 2. 总体结果

```
======================================================================
Scope Routing Evaluation Summary
======================================================================
Total questions : 50
Errors          : 0
OK              : 50

--- File Hit Rate (gold files covered by scope) ---
  Average       : 13.8%
  Perfect (1.0) : 1/50 (2.0%)
  Partial       : 15/50 (30.0%)
  Zero (0.0)    : 34/50 (68.0%)

--- Scope Size ---
  Average files : 16.2 files
  Empty scope   : 0/50

--- Build Pollution ---
  Scopes with build/ files: 29/50 (58%)
======================================================================
```

**结论：文档路由对审计类问题基本不成立。**

---

## 3. 逐题轨迹与人工分析

### 3.1 Perfect Hit（1 题）

#### `common_ngram_map_begin` — File Hit Rate: 1.0

| 项目 | 内容 |
|------|------|
| **Gold** | `common/ngram-map.cpp` |
| **Scope** | `common/ngram-map.cpp`, `common/ngram-cache.cpp`, `common/speculative.cpp` 等 20 个文件 |
| **Sections** | `docs/speculative.md#n-gram Map (...)`, `docs/speculative.md#Command-Line Options` |

**为什么成功**：
- `docs/speculative.md` 这个文档的 section "n-gram Map" 的**内容里直接提到了 `ngram-map.cpp`** 等文件名
- 这是文档和代码**有直接引用关系**的罕见案例
- 成功路径：`Question 中的 "ngram" → BM25 命中 speculative.md#n-gram Map → section 内容提到 ngram-map.cpp → 文件命中`

**启示**：只有当文档内容**显式引用**了实现文件时，文档路由才能工作。

---

### 3.2 Partial Hit（15 题）

#### `llama_model_chat_template` — File Hit Rate: 0.33

| 项目 | 内容 |
|------|------|
| **Gold** | `common/chat.cpp`, `common/common.cpp`, `src/llama-model.cpp` |
| **Scope** | 命中 `common/chat.cpp`，没命中 `common/common.cpp` 和 `src/llama-model.cpp` |
| **Sections** | `README.md#llama-cli`, `docs/multimodal/llava.md#Chat template`, `docs/autoparser.md#Files` |

**为什么部分成功**：
- `docs/autoparser.md#Files` 这个 section 的内容里提到了大量 `common/chat*.cpp` 文件
- 所以 `chat.cpp` 被命中了
- 但 `src/llama-model.cpp`（`llama_model_chat_template` 的定义所在）**从未在文档中被提及**

**失败根因**：文档只提到了调用方文件（`chat.cpp`），没提到定义方文件（`llama-model.cpp`）。

---

#### `common_get_model_endpoint` — File Hit Rate: 0.67

| 项目 | 内容 |
|------|------|
| **Gold** | `common/arg.cpp`, `common/common.cpp`, `common/hf-cache.cpp` |
| **Scope** | 命中 `common/arg.cpp`, `common/common.cpp`，没命中 `common/hf-cache.cpp` |
| **Sections** | `README.md#llama-server`, `README.md#Obtaining and quantizing models`, `docs/development/parsing.md#Common AST Shapes` |

**为什么部分成功**：
- `README.md#llama-server` 的内容里提到了 server 相关的参数处理，可能引用了 `arg.cpp` 或 `common.cpp`
- `hf-cache.cpp` 没被提及

---

#### `cpu_get_num_physical_cores` — File Hit Rate: 0.50

| 项目 | 内容 |
|------|------|
| **Gold** | `common/common.cpp`, `common/common.h` |
| **Scope** | 命中 `common/common.cpp`，没命中 `common/common.h` |
| **Sections** | `docs/development/token_generation_performance_tips.md#Verifying that the CPU is not oversaturated` |

**分析**：
- 这个 section 确实讲 CPU 性能，标题里有 "CPU"
- 路径匹配 `common/common.cpp` 是因为 "common" 被匹配到了（太宽泛）
- `.h` 文件因为不在 `_get_all_source_files` 的主扩展名里？不对，`.h` 在 `_SOURCE_EXTS` 里。可能是路径匹配时只命中了 `.cpp` 文件。

---

### 3.3 Zero Hit（34 题）—— 重点分析

#### 模式 A：文档完全不引用实现文件（最常见，~20 题）

**代表案例：`ggml_sycl_set_device`**

| 项目 | 内容 |
|------|------|
| **Gold** | `ggml/src/ggml-sycl/common.cpp`, `common.hpp`, `cpy.cpp`, `element_wise.cpp` |
| **Scope** | `build/CMakeFiles/...`, `common/jinja/runtime.cpp`, `examples/model-conversion/...` |
| **Sections** | `docs/backend/SYCL.md#Build`, `docs/backend/SYCL.md#III. Run the inference`, `docs/backend/SYCL.md#Q&A` |

**人工分析**：
1. BM25 匹配是对的 —— "sycl" 确实应该匹配到 `docs/backend/SYCL.md`
2. 但 `SYCL.md#Build` 的内容是讲**怎么编译 SYCL backend**，不会说 "`ggml_sycl_set_device` 在 `ggml-sycl/common.cpp` 里"
3. section 内容里**没有文件引用**可提取
4. 路径关键词匹配 `build/` 是因为 "Build" 这个标题关键词匹配了所有路径含 "build" 的文件（包括 `build/CMakeFiles/...`）
5. **根本问题**：文档是用户指南，不是 API 文档。它不会引用具体实现文件。

---

**代表案例：`trim_whitespace`**

| 项目 | 内容 |
|------|------|
| **Gold** | `common/chat-auto-parser-helpers.cpp`, `common/chat-auto-parser-helpers.h`, `common/chat-diff-analyzer.cpp` |
| **Scope** | `common/json-partial.cpp`, `common/json-schema-to-grammar.cpp`, `tests/test-json-...`, `vendor/nlohmann/...` |
| **Sections** | `docs/autoparser.md#Mapper`, `docs/development/parsing.md#Character Classes & Utilities`, `docs/llguidance.md#JSON Schema` |

**人工分析**：
1. "trim whitespace" 这个描述太泛化了，BM25 匹配到了 "Mapper"、"Character Classes"、"JSON Schema"
2. 这些 section 的内容里提到了 `json-schema-to-grammar.cpp` 等文件（因为 autoparser 文档确实引用了一些实现文件）
3. 但 `chat-auto-parser-helpers.cpp` **从未在这些 section 中被提及**
4. **根本问题**：即使 section 匹配"相关"（都是 parser 相关），但文档引用的文件和实际问题需要的文件**不是同一批**

---

**代表案例：`llama_init_from_model`**

| 项目 | 内容 |
|------|------|
| **Gold** | `src/llama-context.cpp` |
| **Scope** | `build/CMakeFiles/...`, `common/arg.cpp`, `common/chat-...` 等完全无关文件 |
| **Sections** | `docs/backend/VirtGPU/configuration.md#APIR_LLAMA_CPP_GGML_LIBRARY_REG`, `docs/build-s390x.md#FAQ`, `docs/autoparser.md#Enums` |

**人工分析**：
1. BM25 匹配**完全错误** —— "llama init from model" 匹配到了 VirtGPU 配置、build FAQ、autoparser Enums
2. 这些 section 和问题**没有任何语义关联**
3. 这说明 BM25 在 section 数量少（~60 个）时，关键词稀疏会导致错误匹配
4. "init" 这个词出现在 build FAQ 里（"How to init..."），"model" 出现在 autoparser Enums 里
5. **根本问题**：question 中的通用词（init, model）和文档中的通用词产生了假阳性匹配

---

#### 模式 B：路径匹配引入噪音（~10 题）

**代表案例：`ggml_backend_cann_init`**

| 项目 | 内容 |
|------|------|
| **Gold** | `ggml/src/ggml-cann/ggml-cann.cpp` |
| **Scope** | `common/arg.cpp`, `common/chat-...`, `common/common.cpp` 等 20 个 `common/` 文件 |
| **Sections** | `docs/build.md#CANN`, `docs/backend/VirtGPU/configuration.md#...` |

**人工分析**：
1. `docs/build.md#CANN` 被 BM25 命中（因为 "CANN" 匹配）
2. 但这个 section 的内容里没有提到 `ggml-cann/ggml-cann.cpp`
3. 路径关键词匹配：title "CANN" → 关键词 ["cann"] → 文件路径匹配
4. 但 `ggml/src/ggml-cann/` 下的文件路径含 "cann"，为什么没匹配到？
5. **原因**：`_get_all_source_files` 遍历 `.cpp` 文件时，可能 `ggml-cann.cpp` 因为某些原因没被找到？或者匹配到了但排在了后面被截断？
6. 实际上看 scope 文件全是 `common/` 下的文件，说明 "init" 或其他关键词匹配到了 `common/` 目录
7. **根本问题**：路径关键词匹配太宽泛，section 标题 "CANN" 和 "init" 组合后，匹配到了大量 `common/` 文件（因为 `common` 是高频词）

---

#### 模式 C：build/ 目录污染（29/50 = 58%）

**几乎每个 zero hit 的 scope 都包含 `build/CMakeFiles/...`**

**原因**：
- `_get_all_source_files()` 用 `repo_root.rglob("*.cpp")` 遍历，没有排除 `build/` 目录
- `build/` 是 CMake 编译输出目录，包含大量生成的 `.cpp` 文件（如 `CMakeCXXCompilerId.cpp`）
- 路径关键词匹配时，标题 "Build" 匹配到了所有路径含 "build" 的文件
- 这是一个**实现 bug**，但也反映了路径关键词匹配的脆弱性

---

#### 模式 D：Section 被过度复用（最严重）

**统计：最频繁被匹配的 sections**

| Section | 被匹配次数 | 说明 |
|---------|-----------|------|
| `docs/autoparser.md#Files` | 15 | 几乎所有 parser/trim/chat 相关问题都匹配到了这里 |
| `docs/autoparser.md#Core Mechanism: Differential Comparison` | 11 | "diff" 这个词匹配到了大量问题 |
| `docs/autoparser.md#Mapper` | 7 | "map" 匹配到了 `ngram_map` 等问题 |
| `docs/backend/VirtGPU/configuration.md#APIR_LLAMA_CPP_GGML_LIBRARY_REG` | 7 | 被完全无关的问题匹配到 |

**分析**：
- `docs/autoparser.md#Files` 被匹配 15 次，因为它包含大量文件引用（`chat-auto-parser-*.cpp` 等）
- 但这些问题中只有少数确实和 autoparser 文件相关
- 大多数问题（如 `ggml_backend_free`, `llama_sampler_init_grammar_impl`）和 autoparser 完全无关
- **这说明 BM25 在 60 个 section 上的区分度不够**，很多问题的 top-3 都撞到了同一个 section

---

## 4. 错误模式归纳

### 4.1 错误类型分布

| 错误类型 | 影响题数 | 占比 | 说明 |
|---------|---------|------|------|
| **文档不引用实现文件** | ~20 | 40% | 文档是用户指南，不会说"函数X在文件Y里" |
| **BM25 匹配错误 section** | ~10 | 20% | 通用词匹配（init, model, diff）导致假阳性 |
| **路径匹配太宽泛** | ~10 | 20% | "Build" → `build/...`，"common" → `common/...` |
| **build/ 目录污染** | ~29 | 58% | 实现 bug，但也反映了路径匹配的脆弱性 |
| **Section 内容引用的是"相关但不同"的文件** | ~5 | 10% | 如 `chat_template` 引用了 `chat.cpp` 但没引用 `llama-model.cpp` |

*注意：一题可能有多种错误类型，所以占比总和 > 100%*

### 4.2 根本根因

```
Question: "AI 帮我生成了 `trim_whitespace`，帮我审一下..."
          │
          ▼
    【认知鸿沟】
          │
    Question 层面：函数级审计（需要具体实现文件）
          │
    Document 层面：用户指南（只讲怎么用，不讲实现在哪）
          │
          ▼
    Result: 文档不知道 `trim_whitespace` 在 `chat-auto-parser-helpers.cpp` 里
```

**这是结构性鸿沟，不是算法可以优化的。**

---

## 5. 关键发现

### 5.1 文档覆盖率极低

对于审计类问题（需要定位具体函数实现），llama.cpp 的文档**几乎不覆盖**正确答案所在的文件。

| Gold 文件目录 | 数量 | 文档是否覆盖 |
|-------------|------|-----------|
| `common/` | 103 | 部分覆盖（文档提到了一些 `common/chat*.cpp`） |
| `ggml/` | 19 | 几乎不覆盖（backend 文档只讲编译配置） |
| `src/` | 14 | 几乎不覆盖（模型架构文档只讲添加新模型） |

### 5.2 唯一成功的模式

**只有当同时满足以下条件时，文档路由才有效：**
1. 问题中的关键词**精确匹配**到某个文档 section（如 "ngram" → `speculative.md#n-gram Map`）
2. 该 section 的**内容中明确引用了**目标实现文件（如 "n-gram Map uses `ngram-map.cpp`"）
3. 目标文件**确实在该 section 引用的文件列表中**

50 题中只有 1 题（`common_ngram_map_begin`）满足这三个条件。

### 5.3 build/ 污染是致命问题

58% 的 scope 包含 `build/CMakeFiles/...` 文件，这意味着：
- 即使文档路由找到了正确的 section，路径匹配也会引入大量噪音
- `build/CMakeCXXCompilerId.cpp` 这种文件永远不应该出现在搜索范围内

---

## 6. 结论

### 6.1 核心结论

> **对于 llama.cpp 这个仓库，README/docs 不能作为审计类问题的可靠认知路由来源。**
>
> 文档认知层 → 实现文件 的映射**结构性缺失**，这不是算法问题，是文档性质问题。

### 6.2 对"Repository Investigation"范式的启示

这个结果并不意味着"缩小搜索空间"的故事失败，而是说明：

1. **认知来源需要多样化**：文档只是来源之一，不能是主要来源
2. **问题本身携带最强信号**：反引号中的函数名（如 `` `trim_whitespace` ``）本身就是最强的路由信号
3. **代码结构比文档更可靠**：目录结构（`ggml/src/ggml-sycl/`）、文件命名（`ggml-cann.cpp`）比文档更稳定

### 6.3 下一步建议

| 优先级 | 方案 | 预期效果 |
|--------|------|---------|
| **P0** | **符号名直接定位**：提取问题中的反引号函数名 → 在 embedding index 中查找同名函数 → 找到所在文件 | 预计命中 80%+ |
| **P1** | **排除 build/ 目录**：修复 `_get_all_source_files` 的目录过滤 | 消除 58% 的噪音 |
| **P2** | **目录继承**：符号名找不到时，用关键词匹配目录（如 "sycl" → `ggml/src/ggml-sycl/`） | 兜底方案 |
| **P3** | **文档作为辅助信号**：文档路由只在高 confidence 时使用，不作为主要来源 | 降低假阳性 |

---

## 7. 附录：完整逐题数据

### Perfect Hit (1)

| # | Symbol | Hit Rate | Scope Size | Sections |
|---|--------|----------|-----------|----------|
| 1 | `common_ngram_map_begin` | 1.00 | 20 | `docs/speculative.md#n-gram Map (...)`, `docs/speculative.md#Command-Line Options` |

### Partial Hit (15)

| # | Symbol | Hit Rate | Scope Size | 命中文件 | 缺失文件 | Sections |
|---|--------|----------|-----------|---------|---------|----------|
| 1 | `llama_model_chat_template` | 0.33 | 20 | `common/chat.cpp` | `common/common.cpp`, `src/llama-model.cpp` | `README.md#llama-cli`, `docs/multimodal/llava.md#Chat template`, `docs/autoparser.md#Files` |
| 2 | `llama_model_default_params` | 0.25 | 20 | `common/common.cpp` | `src/llama-model.cpp`, `src/llama-quant.cpp`, `src/llama.cpp` | `README.md#llama-bench`, `docs/build-riscv64-spacemit.md#Performance`, `docs/development/HOWTO-add-model.md#2. Define...` |
| 3 | `until_common_prefix` | 0.33 | 20 | `common/chat-diff-analyzer.cpp` | `common/chat-auto-parser-helpers.cpp`, `.h` | `docs/autoparser.md#Core Mechanism`, `docs/autoparser.md#Phase 3`, `docs/autoparser.md#analyze_tools` |
| 4 | `common_sampler_init` | 0.25 | 20 | `common/common.cpp` | `common/sampling.cpp`, `.h`, `common/speculative.cpp` | `docs/autoparser.md#Enums`, `docs/backend/VirtGPU/configuration.md#APIR...`, `docs/autoparser.md#Files` |
| 5 | `common_get_model_endpoint` | 0.67 | 20 | `common/arg.cpp`, `common/common.cpp` | `common/hf-cache.cpp` | `README.md#llama-server`, `README.md#Obtaining...`, `docs/development/parsing.md#Common AST...` |
| 6 | `common_list_cached_models` | 0.33 | 20 | `common/preset.cpp` | `common/arg.cpp`, `common/download.cpp` | `docs/backend/CANN.md#GGML_CANN...`, `docs/development/parsing.md#Simple`, `docs/preset.md#Using a Remote Preset` |
| 7 | `cpu_get_num_physical_cores` | 0.50 | 20 | `common/common.cpp` | `common/common.h` | `docs/development/token_generation_performance_tips.md#Verifying...`, `docs/backend/BLIS.md#llama.cpp execution` |

（其余 partial hit 详见 `results/scope_routing_eval.json`）

### Zero Hit 典型案例 (6)

| # | Symbol | Gold 文件 | Scope 文件（前5个） | Sections |
|---|--------|----------|-------------------|----------|
| 1 | `ggml_sycl_set_device` | `ggml/src/ggml-sycl/common.cpp` 等 | `build/CMakeFiles/...`, `common/jinja/runtime.cpp` | `docs/backend/SYCL.md#Build` |
| 2 | `trim_whitespace` | `common/chat-auto-parser-helpers.cpp` 等 | `common/json-partial.cpp`, `common/json-schema-to-grammar.cpp` | `docs/autoparser.md#Mapper` |
| 3 | `ggml_backend_cann_init` | `ggml/src/ggml-cann/ggml-cann.cpp` | `build/CMakeFiles/...`, `common/arg.cpp` | `docs/backend/VirtGPU/configuration.md#APIR...` |
| 4 | `ggml_backend_free` | `ggml/src/ggml-backend.cpp` 等 | `build/CMakeFiles/...`, `common/arg.cpp` | `docs/backend/VirtGPU.md#Device Operations` |
| 5 | `llama_sampler_init_grammar_impl` | `src/llama-sampler.cpp` | `build/CMakeFiles/...`, `common/arg.cpp` | `docs/autoparser.md#Enums` |
| 6 | `common_params_handle_model` | `common/arg.cpp` | `convert_hf_to_gguf.py`, `convert_llama_ggml_to_gguf.py` | `docs/docker.md#Usage` |

---

## 8. 实验代码

- **Document Index**: `src/qa/document_index.py`
- **Scope Planner**: `src/qa/scope_planner.py`
- **Eval Script**: `scripts/eval_scope_routing.py`
- **Raw Results**: `results/scope_routing_eval.json`
