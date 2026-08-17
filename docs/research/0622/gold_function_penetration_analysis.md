# Gold Function 三层穿透分析

## 背景

之前端到端实验发现：

- 文件级检索召回率：90%
- 端到端答案完整引用率：34%

中间存在巨大 gap。为了定位信息到底在哪一步丢失，我们把分析粒度从**文件**下沉到**函数**，对每个 gold evidence entry 做三层穿透分析。

---

## 分析框架

```text
Gold Evidence Entry (function-level)
        ↓
[Stage 0] 所在文件是否被 worklist 访问？
        ↓
[Stage 1] 该函数是否被 retrieve 进 top-K 函数？
        ↓
[Stage 2] 答案是否引用了该函数所在文件？
```

每个 gold function 落入四类之一：

| 类别 | 含义 |
|---|---|
| **A** | 文件未被访问 |
| **B** | 文件已访问，但函数未 retrieve |
| **C** | 函数已 retrieve，但答案未引用 |
| **D** | 函数已 retrieve 且答案引用 |

---

## 数据来源

本分析完全基于现有实验结果，未新跑任何 pipeline：

| 数据 | 来源 |
|---|---|
| Gold evidence（文件、行号、函数名） | `datasets/benchmark_hard.json` |
| Worklist 访问文件列表 | `results/symgraph_worklist_hard_L100_K20_E5_M15_H2_S5.json` |
| V3 检索函数与答案引用 | `results/worklist_e2e_v3_symgraph_worklist_hard_L100_K20_E5_M15_H2_S5.json` |
| 函数名 ↔ 行号范围映射 | `data/qa_embedding_index.json` |

分析脚本：`experiments/analyze_gold_function_penetration.py`

---

## 核心结果

总 gold function entries（排除 .h/.hpp）：**234**

| 类别 | 数量 | 比例 |
|---|---:|---:|
| **D. Cited** | 136 | **58.1%** |
| **B. File visited, function not retrieved** | 65 | **27.8%** |
| **C. Function retrieved, not cited** | 24 | **10.3%** |
| **A. File not visited** | 9 | **3.8%** |

---

## 关键发现

### 1. 最大瓶颈不是答案生成，而是函数级检索

答案生成问题（C 类）只有 **10.3%**。

而**函数没 retrieve 进来**（B 类）高达 **27.8%**。

这说明：

> **Worklist 已经找到了正确的文件，但没有把文件内正确的函数选出来。**

```text
文件级检索     : 90%  ✓
函数级检索     : 62%  ✗  （B + C + D）
答案引用       : 58%  ✗  （D）
```

### 2. Gold 函数的 Rank 分布：embedding 其实不错，截断太激进

进一步分析 gold 函数在 visited files 内的全局排名（取 top-500）：

| Rank 区间 | 数量 | 比例 |
|---:|---:|---:|
| 0-5 | 79 | 37.4% |
| 5-10 | 16 | 7.6% |
| 10-20 | 31 | 14.7% |
| 20-50 | 29 | 13.7% |
| 50-100 | 29 | 13.7% |
| 100-200 | 11 | 5.2% |
| 200-500 | 16 | 7.6% |

关键数字：

- **Median rank = 12**
- **Mean rank = 50.7**
- Top-50 recall: **73.5%**
- Top-100 recall: **87.2%**
- Top-200 recall: **92.4%**

这意味着：

> **Embedding 排名质量其实不差。** 37% 的 gold 函数排在前 5，中位数 rank 只有 12。问题不是"找不到"，而是 **top-50 截断太激进**，很多 gold 函数正好卡在 51-100 区间。

这也说明：单纯把 top-K 从 50 调到 100 就能把 function recall 从 73.5% 提升到 87.2%，但这是工程调参，没有回答"为什么"的问题。

### 3. 文件级 coverage 有误导性

如果只看"gold 文件有没有被访问"，会得到 90% 的乐观数字。

但同一份 gold evidence 里，真正需要的**函数**只有 62% 被 retrieve 到。

这意味着：

> **文件级 retrieval coverage 不能准确预测最终答案质量。** 必须看函数级 coverage。

### 4. 真正的 answer generation 问题比想象中轻

C 类只有 10.3%，说明：

> **只要函数被正确 retrieve 进来，deepseek-v4-pro 大概率会引用。**

答案生成不是主要瓶颈，函数选择才是。

---

## 典型案例

### B 类：文件找到了，函数没 retrieve

**posthoc_public_013**
- 问题：AI 改了缓存扩容和重新分配逻辑
- 8 个 gold entries 全在 `ggml/src/ggml-cann/aclnn_ops.cpp`
- 具体函数：`get_cache_acl_tensor`（lines 1008-1042）
- 文件已被 worklist 访问 ✓
- 但 V3 top-50 函数中**没有任何一个**来自 `aclnn_ops.cpp`
- 被 `common/chat-diff-analyzer.cpp` 的函数以更高 embedding 相似度挤掉

**posthoc_public_044**
- 7 个 gold entries 全在 `src/llama-context.cpp`
- 文件已被访问 ✓
- 但 top-50 中一个相关函数都没 retrieve 到

### C 类：函数 retrieve 了，答案没引用

**posthoc_public_002**
- Gold: `common/chat.cpp:llama_model_chat_template` lines 601/606
- 实际 retrieve 到：`common_chat_templates_init`（rank 43）
- 答案没有引用 `common/chat.cpp`

**posthoc_public_031**
- 9 个 gold functions 全部被 retrieve 到
- 但答案一个都没引用

---

## 这意味着什么？

### 对研究问题的修正

之前怀疑：
> "答案生成不会用证据" 或 "memory organization 有问题"

现在发现：
> **核心问题是 repository investigation 的粒度不够细。找到了文件，但没聚焦到关键函数。**

进一步看，rank 分布说明 embedding  ranking 并不差，真正的问题是：

> **Function retrieval 仍然使用原始问题作为唯一查询，没有利用已经获得的文件级上下文。**

这指向一个更有研究价值的问题：

> **在已经定位到相关文件后，如何利用文件信息（路径、摘要、模块、假设）重写或优化函数级检索？**

这不是 LLM 理解能力问题，而是**调查策略问题**：
- 如何在已访问的文件中识别关键函数？
- 如何利用代码结构（调用关系、符号图）而非仅靠 embedding 相似度？
- 如何根据已读文件更新检索查询，而不是一直用原始问题？

### 对 evaluation 的启示

文件级 retrieval coverage 会高估系统能力。评估 repository QA 系统时，应该同时报告：

- 文件级 coverage
- 函数级 coverage
- 最终 citation coverage

---

## 下一步实验

### 实验 1：Contextual Function Retrieval（最高优先级）

**问题**：利用已访问文件的信息重写函数检索查询，能否把 gold 函数从 rank 50-100 推进 top-50？

**设计**：

```text
Original Query (Q0):
  原始问题

Context:
  已访问文件路径列表
  （或 v2 的 evidence summary）

LLM (flash):
  基于原始问题和上下文，重写一个更精确的函数检索查询

Contextual Query (Q1):
  重写后的查询

比较：
  Q0 retrieve top-50 的 gold function recall
  Q1 retrieve top-50 的 gold function recall
```

**预期贡献**：如果 Q1 >> Q0，说明 function localization 需要利用 file-level context，而不是只用原始问题。

### 实验 2：增加 top-K 函数数量

把 top-K 从 50 调到 100/200，验证 rank 分布的预测：
- Top-100 预计 function recall 87.2%
- Top-200 预计 function recall 92.4%

这主要是工程验证，用于确认 rank 分布分析的可靠性。

### 实验 3：调用关系扩展

对 retrieve 到的函数，加入其 callers/callees。利用符号图而非仅靠 embedding 相似度。

### 实验 4：LLM 重排序函数

先用 embedding 粗筛 top-200，再用小模型根据问题和文件上下文精筛 top-50。

---

## 优先顺序

1. **先做 Contextual Function Retrieval 实验**（研究价值最高）
2. **同时验证 top-K=100**（低成本确认 rank 分布）
3. 根据前两个结果，决定是否需要调用关系扩展或 LLM 重排序

---

## 后续实验：Dynamic Focus 的探索

### 实验 A：基于文件路径的 LLM 快速筛选（失败）

**方法**：不看文件内容，只给 LLM 文件路径，让它把文件分类为 essential/relevant/background/irrelevant，然后只在 essential+relevant 文件内检索函数。

**结果**：

| K | Baseline Recall | Focus Recall |
|---:|---:|---:|
| 5 | 35.1% | 0.4% |
| 50 | 68.9% | 1.8% |

**失败原因**：LLM 把 98% 的文件标成 background 或 irrelevant，平均每题只保留 0.4 个文件。

**教训**：
> **文件路径信息量不足以判断文件重要性。** Repository investigation 不是 metadata reasoning，而是 evidence reasoning。

---

### 实验 B：改造 extract_evidence 输出 importance score

**方法**：修改 prompt，让 LLM 在提取 evidence 的同时输出：
- `relevance_score`: 1-10
- `importance`: essential/relevant/background/irrelevant
- `explanation`

关键改动：取消"2-4 个 key_facts"的硬限制，让 LLM 自己决定文件里有多少证据。

**结果（subset 10）**：

| 类型 | 分布 |
|---|---:|
| irrelevant | 53.4% |
| background | 28.5% |
| relevant | 10.8% |
| essential | 7.2% |

**Gold 文件 vs 非 gold 文件的重要性分布**：

| | Gold Files | Non-Gold Files |
|---|---:|---:|
| essential | 42.9% | 5.8% |
| relevant | 28.6% | 10.1% |
| background | 14.3% | 29.1% |
| irrelevant | 14.3% | 55.0% |
| **avg relevance_score** | **6.5** | **2.6** |

**结论**：
> **LLM 在读文件内容后，能够较好地区分重要文件和无关文件。** Gold 文件平均 relevance score 6.5，非 gold 文件只有 2.6。

---

### 实验 C：基于 importance 的函数检索 focus

**方法**：只保留 essential+relevant 文件，在这些文件内做函数检索。

**结果**：

| K | Baseline | Focus (essential+relevant) | Improvement |
|---:|---:|---:|---:|
| 5 | 18.5% | 22.2% | +3.7% |
| 10 | 25.9% | 40.7% | **+14.8%** |
| 20 | 51.9% | 51.9% | 0.0% |
| 50 | 81.5% | 55.6% | -25.9% |

**解读**：

- **小 K（5-10）focus 有效**：因为 concentrate 在最相关文件里， precision 提高。
- **大 K（50）focus 反而下降**：因为 LLM 不是 100% 准确，有 28.6% 的 gold 文件被错误标记为 background/irrelevant，一旦过滤就永远找不到了。

这说明：
> **Evidence-driven focus 有潜力，但不能用硬截断。需要在 precision 和 recall 之间做权衡。**

可能的改进：
1. **Soft focus**：不按 category 硬过滤，而是按 relevance_score 加权排序函数。
2. **Two-stage retrieval**：先在 focused 文件内检索 top-K，再从全部文件补充 top-K。
3. **Iterative focus**：根据新证据不断更新 importance，而不是一次性决定。

---

## 结论

本分析把端到端 gap 从"文件 → 答案"拆成了"文件 → 函数 → 答案"，发现：

- **27.8% 的 gold evidence 丢失在函数级检索阶段**
- **10.3% 丢失在答案生成阶段**
- **3.8% 丢失在文件级检索阶段**

进一步探索 Dynamic Focus 发现：

1. **Metadata-based focus 失败**：只看文件路径无法判断重要性。
2. **Evidence-based focus 可行**：LLM 读完文件后能给合理 importance score，gold 文件 score 显著更高。
3. **Hard filtering 有代价**：会错误过滤掉部分 gold 文件，更适合小 K precision 场景，不适合大 K recall 场景。

因此，下一步应该研究 **soft evidence-driven focus** 或 **iterative focus update**，而不是简单的文件过滤。
