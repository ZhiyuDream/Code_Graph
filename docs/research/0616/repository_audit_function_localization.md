# Repository Audit 的 Localization 真相：Question → Function 已基本解决

## 摘要

针对 llama.cpp post-hoc audit benchmark（50 题），我们把 repository audit 的入口定位问题从 **file-level** 下沉到 **function-level**，发现了一组和此前认知完全不同的数字：

| 阶段 | 指标 | 数值 |
|------|------|------|
| **Question → Function Recall** | Gold function 在 Top-10 | **86.0%** |
| **Function → Gold Selection** | Conditional Accuracy (name only) | **79.1%** |
| **Function → Gold Selection** | Conditional Accuracy (signature) | **81.4%** |
| **Function → Gold Selection** | Conditional Accuracy (signature + body) | **83.7%** |

合并估算：

```text
Question → Gold Function ≈ 86% × 82% ≈ 70%
```

也就是说，**仅依靠 embedding + LLM 选择，就能在约 70% 的题目上找到正确的 gold function**。

这个数字远高于早期 file-level topic routing（13.8%）和 file-level selection（62.2%）。

**核心结论**：对于 repository audit，**Question → Function 的 localization 阶段已经不是主要瓶颈**。真正值得研究的问题转向：

> **找到 gold function 之后，如何沿着调用/被调用关系扩展到完整的证据链？**

---

## 1. 研究背景：为什么从 File-Level 下沉到 Function-Level？

此前的工作把 "Question → Entry File" 作为核心定位问题。但我们发现：

- 文件名本身携带了很强的职责信号（Filename Only 在 file-level 上达到 84.6% conditional accuracy）。
- 但完整代码内容反而引入噪声（Full Content 降至 76.9%）。
- 这说明 **文件级粒度太粗**，真正应该定位的是 **function**。

我们的 benchmark 本质上也问的是"哪个实现负责这个行为"，而不是"哪个文件负责"。

---

## 2. Stage 0: Question → Function Recall

使用 function-chunk embedding 检索，测量 gold function / gold file 的召回率。

| K | Question Recall | Gold File Recall |
|---|-----------------|------------------|
| 1 | 58.0% | 26.4% |
| 5 | 76.0% | 44.5% |
| **10** | **86.0%** | **54.5%** |
| 20 | 92.0% | 67.3% |

**关键发现**：

- **86% 的题目**至少有一个 gold function 被召回到 Top-10。
- 但 Gold File Recall 只有 54.5%，说明 Top-10 通常只召回了一部分 gold files。
- Gold function 最佳排名中位数为 **2**，均值为 **5.1**（长尾拉高了均值）。

这说明 embedding 已经能把正确答案放到很靠前的位置，但召回的完整度还不够。

---

## 3. Stage 1: Function → Gold Selection

给定 Question + Embedding Top-10 function candidates，让 LLM 选择最相关的函数。

### 3.1 50 题结果

| 输入条件 | Selection Accuracy | Conditional Accuracy |
|----------|-------------------|---------------------|
| Function Name Only | 68.0% | **79.1%** |
| Name + Signature | 70.0% | **81.4%** |
| Name + Signature + Body | 72.0% | **83.7%** |

### 3.2 15 题 vs 50 题对比

| 条件 | 15 题 | 50 题 |
|------|-------|-------|
| name_only | 92.3% | 79.1% |
| signature | 92.3% | 81.4% |
| signature_body | 84.6% | 83.7% |

15 题的小样本高估了 name_only 和 signature，但 50 题结果仍然很强。

### 3.3 信息组织的启示

- **函数名是主要信号**：name_only 已经达到 79.1%。
- **签名提供少量补充**：+2.3%。
- **函数体提供少量补充**：+4.6%，但边际收益递减。

这和 RAG 领域 "Title + Abstract > Full Text" 的现象一致：**高层次的语义标签比实现细节更适合做初筛**。

---

## 4. File-Level vs Function-Level

| Level | Question Recall@10 | Conditional Selection Accuracy | Combined Correct Entry Rate |
|-------|-------------------|-------------------------------|----------------------------|
| File-Level | 90.0% | 62.2% | ~56% |
| **Function-Level** | **86.0%** | **~82%** | **~70%** |

Function-level 虽然 Question Recall 略低（因为函数粒度更细，召回池更小），但 Selection Accuracy 显著更高。

**核心原因**：function name 是比 file name 更精确的语义标签。

---

## 5. 失败 Case 分析

Function-level name_only 在 50 题中 Conditional Accuracy 为 79.1%，意味着在 gold function 已召回的 43 题中，有 **9 题 LLM 没有选中 gold function**。

### 5.1 失败题目列表

| 题号 | Gold 排名 | LLM 选择 | 失败类型 |
|------|----------|---------|---------|
| posthoc_public_002 | Rank 7 (`common_chat_template_direct_apply_impl`) | `common/chat-auto-parser-helpers.cpp::compare_variants` | Gold 排名太后 + 候选语义相近 |
| posthoc_public_014 | Rank 1,3,5,8,10 (多个 chat 函数) | `src/llama-chat.cpp::llm_chat_detect_template` | Gold 在 Top，但 LLM 选了相关但非 gold 的函数 |
| posthoc_public_027 | Rank 2 (`autoparser::build_parser`) | `common/peg-parser.cpp::build_peg_parser` | 名字相似但实现不同 |
| posthoc_public_030 | Rank 1-6 多个 gold | `common/chat-auto-parser.h::analyze_content` | Gold 都在 Top，但 LLM 选了 header 里的函数 |
| posthoc_public_032 | Rank 3 (`ggml_backend_webgpu_reg`) | `ggml/src/ggml-backend-reg.cpp::get_reg` | Gold 在 Top，但 LLM 选了通用注册基础设施 |
| posthoc_public_036 | Rank 4 (`common_ngram_map_begin`) | `common/ngram-cache.cpp::common_ngram_cache_update` | 名字相似但模块不同 |
| posthoc_public_038 | Rank 1 (`common_params_handle_model`) | `tools/server/server-models.cpp::server_models::update_status` | Gold 在 Top，但 LLM 被调用方吸引 |
| posthoc_public_042 | Rank 1,5,8,10 (多个 sycl 函数) | `ggml/src/ggml-sycl/set_rows.cpp::set_rows_sycl_q` | Gold 在 Top，但 LLM 选了具体实现函数 |
| posthoc_public_049 | Rank 10 (`common_params_print_completion`) | `examples/gen-docs/gen-docs.cpp::write_help` | Gold 排名太后 |

### 5.2 失败类型分类

从 9 个失败 case 可以归纳为 3 类：

#### 类型 A：Gold 排名太后（2/9）

- posthoc_public_002: gold 在 rank 7
- posthoc_public_049: gold 在 rank 10

这类题目**本质上是 retrieval 问题**：embedding 没有把最合适的 gold function 排到前面。

#### 类型 B：Gold 在 Top，但 LLM 被"相关但非 gold"的函数吸引（5/9）

- posthoc_public_014: gold 是 `common_chat_verify_template`，LLM 选了 `llm_chat_detect_template`
- posthoc_public_032: gold 是 `ggml_backend_webgpu_reg`，LLM 选了 `get_reg`
- posthoc_public_038: gold 是 `common_params_handle_model`，LLM 选了 server 调用方
- posthoc_public_042: gold 是 `ggml_check_sycl`，LLM 选了 `set_rows_sycl_q`

这类题目说明：**函数名语义匹配成功，但 LLM 分不清哪个函数是问题的核心，哪个是周边相关**。这类似于 RAG 中的 "relevance vs. centrality" 问题。

#### 类型 C：候选函数名相似导致混淆（2/9）

- posthoc_public_027: `autoparser::build_parser` vs `build_peg_parser`
- posthoc_public_036: `common_ngram_map_begin` vs `common_ngram_cache_update`

这类题目说明：**当多个函数名字语义相近时，LLM 仅凭名字难以区分**。

### 5.3 关键洞察

9 个失败中，只有 **2 个是 retrieval 失败**（gold 排名太后），其余 **7 个是 selection/re-ranking 失败**（gold 已在候选池，但 LLM 没选）。

这意味着：

> **进一步提升 Question → Function 的空间主要在 re-ranking，而不是 retrieval。**

但 79.1% 的 conditional accuracy 已经很高，继续提升的边际收益可能有限。更值得研究的是：即使选中了正确的 function，如何扩展到完整证据链。

---

## 6. 当前研究问题的转移

早期框架：

```text
Question → Entry (hard)
Entry → Evidence (solved by symbol expansion)
```

现在框架：

```text
Question → Function (基本解决：~70% 正确率)
Function → Evidence Chain (真正瓶颈，尚未测量)
Answer Generation (Oracle 显示基本解决)
```

因此，后续研究重心应转向：

> **给定一个（可能正确的）function，如何扩展到完整的证据链？**

具体子问题：

1. **Symbol Expansion 上限**：给定 gold function，caller/callee/definition 扩展能覆盖多少 gold files？
2. **Error Recovery**：如果选中的 function 不是 gold，Agent 能否在后续步骤中修正？
3. **Multi-Function Exploration**：同时从多个候选 function 出发，能否提高覆盖率？

---

## 7. 下一步实验计划

按优先级排序：

### 7.1 失败 Case 分析（已完成）

见第 5 节。

### 7.2 Question → Function → Evidence → Answer 端到端 Pipeline（第二优先级）

```text
Question
  ↓
Function Embedding Top-10
  ↓
LLM select function (name_only)
  ↓
Symbol expansion from selected function
  ↓
Generate answer
```

测量最终 coverage，看 70% 的 correct function rate 能转化多少端到端性能。

### 6.3 Function → Evidence Chain Completion（第三优先级）

给定 gold function（使用 benchmark `gold_evidence.symbol`），通过 grep 做 caller/callee/definition 扩展，测量能覆盖多少 gold files。

这能回答：symbol expansion 的上限到底有多高？

---

## 7. 附录：实验文件

| 实验 | 结果文件 | 脚本 |
|------|----------|------|
| Stage 0 Function-Level Recall (50题) | `results/stage0_function_recall_0_50.json` | `experiments/run_stage0_function_recall.py` |
| Stage 1 Function-Level Selection name_only (50题) | `results/stage1_function_selection_name_only_0_50.json` | `experiments/run_stage1_function_selection.py` |
| Stage 1 Function-Level Selection signature (50题) | `results/stage1_function_selection_signature_0_50.json` | `experiments/run_stage1_function_selection.py` |
| Stage 1 Function-Level Selection signature_body (50题) | `results/stage1_function_selection_signature_body_0_50.json` | `experiments/run_stage1_function_selection.py` |
| Stage 0 File-Level Recall (50题) | `results/stage0_region_recall_0_50.json` | `experiments/run_stage0_region_recall.py` |
| Stage 1 File-Level Selection (50题) | `results/stage1_region_selection_v3_0_50.json` | `experiments/run_stage1_region_selection.py` |
