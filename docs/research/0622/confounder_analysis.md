# 端到端 Citation Gap 的潜在混淆变量分析

之前的报告匆忙下了结论："答案生成是瓶颈"。但这个结论证据不够，里面混了至少 5-6 个变量。本文档逐一分析这些疑点，区分"现有数据能回答"和"需要补实验"。

---

## 疑点 1：Evidence extraction 是否真的把 gold 信息传递给了 LLM？

### 问题

"gold 文件进入 evidence_log"不等于"gold 文件的有效信息被保留"。

例如：
```
common/chat.cpp
key_facts:
- "handles chat template"
```
和
```
common/chat.cpp
key_facts:
- "llama_chat_apply_template selects template based on model name"
- "line 234: uses common_chat_templates array"
```
是完全不同的信息量。

### 现有数据能否回答？

**现在能部分回答了。**

已修改 `run_worklist_end_to_end_v2.py` 保存完整 `evidence_log`，并在前 10 题子集上跑了诊断实验。

### 实测结果（前 10 题）

| 指标 | 数值 |
|------|-----:|
| Gold 文件对应的 evidence 条目数 | 26 |
| 平均 key_facts 数 / gold 文件 | **2.7** |
| 平均 key_facts 字符数 / gold 文件 | **275.5** |
| 零 facts 的 gold 文件数 | 1 |
| 最高压缩后比例 | 12.54% |
| 最低压缩后比例 | **0.03%** |

**典型压缩示例：**

```
posthoc_public_003 src/llama-model.cpp
原始文件: 543,959 字符
extract_evidence 后: 166 字符 (0.03%)
```

```
posthoc_public_002 common/chat.cpp
原始文件: 103,633 字符
extract_evidence 后: 247-421 字符 (0.24%-0.41%)
```

### 发现

Gold 文件被极度压缩。一个 10 万字符的文件只剩几百字符的 key facts。这意味着 Stage 2 看到的不是"文件内容"，而是 Stage 1 对文件相关性的高度概括。

**这不能简单说"Stage 1 丢了信息"**，因为不压缩根本塞不进 prompt。问题是：**压缩后的 key facts 是否足够让答案生成模型决定引用该文件？**

从结果看，答案模型大量漏引 gold 文件，说明 key facts 可能太稀疏，或者没有突出"这个文件必须被引用"的信号。

---

## 疑点 2：LLM 到底看到 prompt 了吗？

### 问题

Stage 2 平均收到 57 个 evidence 文件，每个文件还有 1500 字符的 files_summary。总上下文可能达到数万 token。如果超过模型有效注意力范围，模型可能根本没看到后部内容。

### 现有数据能否回答？

**现在能回答了。**

已修改脚本保存实际 prompt 并统计 token 数（使用 tiktoken 估算）。

### 实测结果（前 10 题）

| 指标 | Token 数 |
|------|---------:|
| 平均值 | **37,089** |
| 中位数 | **38,917** |
| 最小值 | **29,121** |
| 最大值 | **41,791** |

### 解读

Prompt 约 30K-42K token，主要来自：
- `files_summary`：57 个文件 × 1500 字符 ≈ 85K 字符 ≈ 25K-30K token
- `evidence_log`：57 个文件的 key facts ≈ 5K-10K token

DeepSeek-v4-pro 的上下文窗口通常 >= 128K，所以**理论上装得下**。但 LLM 的**有效 attention** 在中间/后部会衰减。30K-40K 已经处于"可能开始衰减"的区间。

但这不是决定性的。因为：
- 如果模型"没看到"后部， citation 应该随位置单调下降；
- 实测位置效应**并非严格单调**（见疑点 3）。

所以 prompt length 是一个 contributing factor，但不是唯一原因。

---

## 疑点 3：Gold 文件在 prompt 中的位置是否影响引用？

### 问题

如果 gold 文件排在 prompt 后部，模型可能因注意力衰减而忽略它们。

### 现有数据能否部分回答

**能。**

### 全量 50 题的初步结果（字符串匹配）

| 在 prompt 中的位置 | 被引用比例 |
|---:|---:|
| 0-10 | 60.4% |
| 10-20 | 86.3% |
| 20-30 | 37.5% |
| 30-40 | 26.7% |
| 40-50 | 37.5% |
| 50-60 | 88.2% |

### 前 10 题子集的结果（字符串匹配，更可靠）

| 在 prompt 中的位置 | 被引用比例 |
|---:|---:|
| 0-10 | **83.3%** |
| 10-20 | **60.0%** |
| 20-30 | **33.3%** |
| 30-40 | **0.0%** |

### 解读

子集数据显示**明显的位置效应**：位置越靠前，引用率越高。位置 30-40 的 gold 文件完全没有被引用。

但需要注意：
- 排序本身可能和相关性有关（extract_evidence 先返回的文件可能更相关）。
- 30-40 的样本量较小（仅 2 个 gold 文件）。

### 需要补什么

1. 做 position ablation：把同一批 evidence 文件按不同顺序排列，看 citation 率是否变化。
2. 或者把 gold 文件手动提前到 prompt 前部，观察是否提升。

---

## 疑点 4：Gold 文件是不是都应该被引用？

### 问题

不能默认所有 gold evidence 都必须出现在答案中。有些 gold 文件可能是推导过程中的中间节点，答案只需要引用最关键的 1-2 个。

例如：
```
Question: temperature 默认值是多少？
Gold: llama.cpp → common/chat.cpp → sampling.cpp
```
如果答案只引用 `sampling.cpp` 并说"默认 0.8"，其实已经足够，不需要引用 `llama.cpp` 和 `common/chat.cpp`。

### 现有数据能否回答？

**不能自动回答。**

需要人工标注或基于 reference_answer 推断每个 gold 文件的重要性等级。

### 需要补什么

1. 随机抽取 20 题，人工标注每个 gold 文件：
   - **Primary**：答案必须引用，否则结论无法验证
   - **Secondary**：引用更好，但不是必须
   - **Optional**：只是上下文，不引用也合理
2. 重新计算 citation coverage，但只统计 Primary gold files。
3. 对比 reference_answer 中实际引用了哪些 gold 文件。

### 预期影响

如果大量 gold 文件其实是 Secondary/Optional，那 24% 的完整引用率就不像看起来那么低。

---

## 疑点 5：LLM 用了证据但没引用，还是根本没用？

### 问题

当前 judge 只问"答案里有没有提到文件路径"。但模型可能综合了多个文件的信息，用自然语言表述，没有写文件名。

这是两个不同的问题：
- **没引用**：其实用了文件内容，只是没写路径
- **没用**：根本没看该文件

### 现有数据能否回答？

**不能。**

需要一个新的 judge prompt：
```
Gold file: common/chat.cpp
Answer: ...

请判断：答案是否明显依赖了该文件中的信息？
回答：YES / NO
```

### 需要补什么

1. 写一个新的 `evidence_usage_judge.py`。
2. 对每个 gold 文件，让 LLM 判断"答案是否使用了该文件的信息"。
3. 比较两个指标：
   - `cited_ratio`：答案提到文件路径的比例
   - `used_ratio`：答案依赖文件信息的比例

### 预期发现

很可能 `used_ratio > cited_ratio`。如果是这样，问题就不是"LLM 不会用证据"，而是"LLM 不习惯显式引用"。

---

## 疑点 6：Prompt 是不是天然鼓励 Summary？

### 问题

当前 `generate_answer.txt` prompt 要求：
```text
1. 直接回答原始问题
2. 引用代码证据
3. 答案末尾包含引用文件清单
```

这种表述容易让模型"总结结论 + 举 1-2 个代表例子"，而不是"穷举所有相关文件"。

### 现有数据能否回答？

**不能直接回答。**

需要做 prompt ablation。

### 需要补什么

1. **Prompt A（原 prompt）**：正常回答。
2. **Prompt B（强制穷举）**：明确要求"必须引用 evidence_log 中的每一个相关文件，不允许遗漏"，并给出清单格式。
3. 在控制其他变量的情况下，比较两个 prompt 的 citation coverage。

### 预期影响

如果 Prompt B 能把 coverage 从 24% 提升到 50-60%，那说明问题主要是 instruction，而不是模型能力或 context length。

---

## 最可能的真实故事（基于前 10 题子集）

```text
Stage 0: 检索 90%  ✓

Stage 1: extract_evidence 极度压缩信息
         gold 文件平均只剩 2.7 个 facts / 275 字符
         压缩率 0.03% - 12.54%

Stage 2: prompt 约 37K token
         gold 文件位置分布不均
         位置 0-10 引用率 83%
         位置 30-40 引用率 0%

Stage 3: 答案只引用少数文件
         完整引用率 30%（字符串匹配）
```

### 关键判断

**不是单一瓶颈。** 至少有三个因素同时作用：

1. **Evidence 压缩过度**：Gold 文件只剩几百字符的 key facts，信号太弱，答案生成模型无法判断该文件的重要性。
2. **位置效应**：Prompt 中靠后的 gold 文件引用率显著降低。
3. **Prompt 鼓励总结**：模型天然倾向于选代表文件，而不是穷举。

所以更准确的结论不是"答案生成不会用证据"，而是：

> **检索到的证据在传递给答案生成阶段时，经历了过度压缩和位置偏置，导致关键 gold 文件被忽略。**

这和前面 retrieval/exploration 的工作是连贯的：先解决"找到"，现在要解决"保留和组织"。

---

## 下一步实验计划

按优先级排序：

### P0：在完整 50 题上重跑诊断版脚本
- 当前只有前 10 题子集数据
- 需要确认压缩率、位置效应在 full benchmark 上是否一致
- 已修改脚本，可跑完整版

### P0：Evidence 压缩策略 ablation
- 假设：当前 key facts 太稀疏，答案模型无法判断重要性
- 实验：对 top-K 最相关文件保留更多内容（如完整函数片段、更长摘要），而不是所有文件都 1500 字符
- 成本较低，只改 Stage 1 输出

### P1：Position ablation
- 把 gold 文件提前到 prompt 前部
- 如果 citation 率显著提升，说明位置效应是主因之一
- 如果提升不大，说明压缩/quality 是主因

### P1：Prompt ablation（用户建议暂缓）
- 等 P0/P1 做完后再做
- 避免在没搞清楚问题的情况下强行穷举

### 不再做
- ~~Evidence usage judge~~：用户认为 prompt 已要求引用清单，无需区分 used/cited
- ~~Gold 重要性标注~~：用户认为已排除头文件，剩余 gold 都应引用

---

## 方法论教训

1. **不要从单一现象跳到大结论。** "答案没引用"不等于"答案生成是瓶颈"。
2. **测量中间变量。** Prompt 长度、evidence 压缩率、gold 位置都是可观测的中间变量。
3. **先诊断，再干预。** 在改 prompt / 改模型之前，先用小样本搞清楚问题在哪。
4. **控制变量。** Position effect 和文件重要性可能混淆，需要 ablation。
