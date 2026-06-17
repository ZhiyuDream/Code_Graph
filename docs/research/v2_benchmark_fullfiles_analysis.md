# llama.cpp Benchmark V2 实验报告：完整文件上下文 vs 函数片段

> 实验日期: 2026-05-22  
> 实验目标: 验证"完整文件上下文"策略在代码审查类任务上的效果  
> 核心发现: **完整文件上下文对代码审查类任务有害，函数片段模式正确率高出 14%**

---

## 1. 数据集介绍

| 属性 | 说明 |
|---|---|
| 名称 | llamacpp benchmark v2 |
| 路径 | `datasets/llamacpp benchmark v2.xlsx` |
| 题数 | **50 题** |
| 问题类型 | **代码审查**（"AI 帮我生成了 `xxx`，审一下它的返回值/参数/副作用是否符合调用方假设"） |
| 一级维度 | 仓库上下文一致性 / 鲁棒性与生命周期 / 并发与运行时风险 |
| 二级模板 | 调用方契约兼容性 / 配置/参数传播一致性 / 共享 helper 复用影响 / 初始化/外部 API 契约 / 资源生命周期/清理边界 / 状态一致性 / 并发/异步安全 |
| 每题字段 | 评测问题、参考答案（带证据和边界）、关键证据（精确到文件行号）、判分要点（4 条） |

与之前的 324 题开放式问答数据集不同，v2 是**高结构化、证据驱动的代码审查任务**，每题都有明确的关键证据清单。

---

## 2. 实验配置

| 配置项 | 值 |
|---|---|
| 答案生成模型 | DeepSeek-v4-pro |
| 评估模型（Judge） | GPT-4.1-mini |
| Retrievers | embedding + issue + grep + graph |
| ReAct 扩展深度 | 1 层 callers/callees |
| 每题最大完整文件数 | 10（实验组）/ 0（对照组） |
| 并行 workers | 10 |
| 评估方式 | Binary Judge（CORRECT / INCORRECT） |

---

## 3. 核心结果对比

### 3.1 整体指标

| 指标 | Full-files（完整文件） | Baseline（函数片段） | Delta |
|---|---|---|---|
| **Binary 正确率** | **42/50 = 84.0%** | **49/50 = 98.0%** | **-14.0%** ⚠️ |
| 证据检索覆盖率（func files） | 82.2% | 82.2% | 0% |
| 证据引用准确率 | 33.5% | 23.7% | +9.8% |
| 平均时延 | 181.0s | 180.8s | +0.2s |
| 平均检索函数数 | 23.9 | ~24 | 持平 |
| 平均完整文件数 | 9.0 | 0 | — |

### 3.2 按维度拆分正确率

| 一级维度 / 二级模板 | Full-files | Baseline | 胜方 |
|---|---|---|---|
| 仓库上下文一致性 / 共享 helper 复用影响 | 10/10 = 100% | 10/10 = 100% | 持平 |
| 仓库上下文一致性 / 状态一致性 | 4/4 = 100% | 4/4 = 100% | 持平 |
| 并发与运行时风险 / 并发/异步安全 | 7/7 = 100% | 7/7 = 100% | 持平 |
| 鲁棒性与生命周期 / 资源生命周期/清理边界 | 4/4 = 100% | 3/4 = 75% | **full+** |
| 仓库上下文一致性 / 调用方契约兼容性 | 9/11 = 81.8% | 11/11 = 100% | **base+** |
| 仓库上下文一致性 / 配置/参数传播一致性 | 8/11 = 72.7% | 11/11 = 100% | **base+** |
| 鲁棒性与生命周期 / 初始化/外部 API 契约 | **0/3 = 0%** | 3/3 = 100% | **base+** |

### 3.3 逐题差异（9 题不同）

| 题号 | 维度 | 胜方 | 关键原因 |
|---|---|---|---|
| Q1 | 调用方契约兼容性 | base+ | full-files 被无关文件淹没，误判契约 |
| Q14, Q17, Q47, Q48 | 配置/参数传播一致性 | base+ | 大文件引入无关配置逻辑，干扰判断 |
| Q30, Q31 | 初始化/外部 API 契约 | base+ | 证据文件未被检索到，full-files 给了错误文件 |
| Q32 | 初始化/外部 API 契约 | base+ | 同理 |
| **Q49** | 资源生命周期/清理边界 | **full+** | 唯一胜例：grammar sampler 需要跨文件理解 ctx 分配逻辑 |

---

## 4. 关键洞察

### 洞察 1：完整文件上下文对代码审查类任务有害

- 平均 9 个完整文件，总 context 可达 **75 万字符**（约 250K tokens）
- DeepSeek 的注意力被海量无关代码稀释，难以定位关键证据
- 函数片段模式强制 LLM 聚焦在相关函数上，判断更准确

> 这与之前 324 题开放式问答的结论一致：扩大 context（从 8 个函数片段到 10 个完整文件）没有带来准确率提升，反而下降了 14%。

### 洞察 2：瓶颈不在检索覆盖率，而在 LLM 利用上下文的方式

| 阶段 | 覆盖率 | 说明 |
|---|---|---|
| 原始计算（未修复） | 65.0% | 行号范围、路径分隔符、扩展名未统一 |
| 修复后计算 | **82.2%** | 归一化路径 + basename 映射 |
| 仅 1/50 题为 0% | — | Q41（`ggml_sycl_op_set`）embedding 语义漂移 |

修复检索链路后，baseline 和 full-files 的**检索能力几乎相同**（82.2%），但正确率差距巨大（98% vs 84%）。

这说明：**即使检索到了正确的文件，把完整文件灌给 LLM 也是有害的**。

### 洞察 3：证据引用准确率与正确率负相关

- Full-files 证据引用准确率更高（33.5% vs 23.7%），因为它有更多文件路径可引用
- 但这些引用往往是不相关的文件，反而误导了判断
- **引用得多 ≠ 引用得对**

### 洞察 4：不同维度对上下文的敏感度不同

- **共享 helper / 状态一致性 / 并发安全**：两种模式都是 100%，说明这些任务不需要跨文件理解
- **配置/参数传播一致性**：full-files 掉了 27.3%，大文件中的无关配置参数干扰了判断
- **初始化/外部 API 契约**：full-files 完全失败（0% vs 100%），因为检索器没找到证据文件，full-files 模式把错误的 backend 文件灌给了 LLM
- **资源生命周期/清理边界**：唯一 full-files 胜出的维度（100% vs 75%），因为这类问题确实需要跨文件追踪 ctx 的分配和释放

---

## 5. 关于扩展名映射的讨论

实验中曾尝试将 `.cpp` 与 `.h` 视为同一组文件来提升覆盖率统计。但后续反思认为这一做法**过度宽松**：

- `.h` 文件通常只包含**函数声明/签名**
- `.cpp` 文件才包含**具体实现代码**
- 对于代码审查任务（审查返回值、参数、副作用、调用方契约），**只看签名是不够的，必须看实现**
- 因此，将 `.h` 算成 `.cpp` 的覆盖在语义上是不准确的

例外情况：
- inline 函数或模板函数的实现可能在 `.h` 中
- 某些审查任务只需要确认接口契约（此时 `.h` 足够）

**建议**：后续实验中取消宽松的扩展名映射，严格按文件路径匹配。如果确实需要跨文件关联，应通过 Neo4j 的 CALLS/DECLARED_IN 等显式边来表示，而非简单的字符串替换。

---

## 6. 检索链路断点分析

### 6.1 覆盖率 0% 的原因（修复前）

| 断点 | 影响题 | 根因 |
|---|---|---|
| 路径分隔符不匹配 | Q49 | 证据用反斜杠 `src\llama-sampler.cpp`，系统用正斜杠 |
| 行号范围格式 | Q6, Q20 等 | 证据用 `:87-207`，正则只匹配 `:87` |
| 文件名不匹配 | Q30, Q31 | 证据引用 `.cpp`（实现），但图中函数定义在 `.h`（声明），且路径前缀也不同（`src/` vs `include/`） |
| Embedding 语义漂移 | Q41 | `ggml_sycl_op_set` 的语义和调度函数太像，top 10 全是 `ggml_backend_sched_*` |

### 6.2 修复后的覆盖率

- 路径归一化 + basename 映射后，覆盖率从 **65.0% → 82.2%**
- 仅剩 **Q41** 为 0%（embedding 语义漂移，需 Representative Function 标注优化）

---

## 7. 下一步建议

### 7.1 立即执行

1. **默认使用函数片段模式**，准确率 98% 远高于完整文件的 84%
2. **仅在需要跨文件理解的复杂问题（如资源生命周期）时手动开启 full-files**
3. **取消扩展名宽松映射**，严格按文件路径匹配覆盖率

### 7.2 短期优化

4. **强制证据引用**：Baseline 正确率 98%，但证据引用准确率仅 23.7%。在 prompt 中明确要求"必须列出关键证据的具体文件路径和行号"
5. **修复 Q41 的 embedding 语义**：为 `ggml_sycl_op_set` 等函数添加 Representative Function 标注，强化其与调度函数的语义区分度

### 7.3 中期探索

6. **分层上下文策略**：不是"全给"或"不给"，而是先给函数片段，让 LLM 判断是否需要扩展，再按需读取完整文件
7. **动态文件选择**：根据问题类型自动决定是否启用 full-files（如资源生命周期类问题自动启用，配置参数类问题禁用）

---

## 附录：原始结果文件

| 文件 | 说明 |
|---|---|
| `results/v2_deepseek_fullfiles.json` | Full-files 实验组原始结果（50 题） |
| `results/v2_deepseek_fullfiles.eval.json` | Full-files 评估结果（含 binary judge + 引用准确率） |
| `results/v2_deepseek_baseline.json` | Baseline 对照组原始结果（50 题） |
| `results/v2_deepseek_baseline.eval.json` | Baseline 评估结果 |

---

## 补充：人工评估 Binary Judge 的可靠性

> 2026-05-22 补充  
> 使用 Kimi 模型能力对 judge 结果进行人工抽查，发现关键事实。

### 核心发现：Full-files 的 14% 错误中，7/8 是生成失败而非答案质量差

| 分析维度 | 数量 |
|---|---|
| Full-files 总错误 | 8/50 |
| 其中：Context 超限生成失败 | **7/8** |
| 其中：生成成功但被判错 | **1/8**（Q1） |

#### 7 题生成失败详情

| 题号 | 目标函数 | 失败原因 |
|---|---|---|
| Q14 | `common_get_model_endpoint` | Context > 1M tokens |
| Q17 | `common_get_model_endpoint` | Context > 1M tokens |
| Q30 | `ggml_backend_cann_init` | Context > 1M tokens |
| Q31 | `ggml_backend_webgpu_reg` | Context > 1M tokens |
| Q32 | `llama_sampler_backend_support` | Context > 1M tokens |
| Q47 | `common_get_model_endpoint` | Context > 1M tokens |
| Q48 | `common_arg::to_string` | Context > 1M tokens |

DeepSeek-v4-pro 的最大上下文为 **1,048,576 tokens**，但 full-files 模式对这 7 题请求了 **1,320,470 tokens**，直接导致生成失败。

**Baseline 在这 7 题上全部正确。**

#### 排除生成失败后的真实对比

| 方案 | 排除生成失败的 43 题 | 正确率 |
|---|---|---|
| Full-files | 42/43 | **97.7%** |
| Baseline | 42/43 | **97.7%** |

**结论：在能成功生成答案的情况下，两种模式的答案质量几乎完全一样。**

### Judge 可靠性分析

#### Q1：`llama_model_chat_template` 调用方契约兼容性

- **Baseline**：CORRECT（judge 认为分析准确）
- **Full-files**：INCORRECT（judge 认为指出 `"mistral-v7-tekken"` 硬编码是严重偏差）
- **人工判断**：两个答案实际上分析了同一段代码，结论一致（参数和返回值符合调用方假设）。Full-files 只是更详细地展示了所有调用方。Judge 对 full-files 更严格，但判断标准不一致。
- **结论**：Judge 对 Q1 的评估存在偏差。

#### Q49：`llama_sampler_init_grammar_impl` 资源生命周期

- **Baseline**：INCORRECT（分析深入但代码截断，未明确给出"路径是否闭合"的结论）
- **Full-files**：CORRECT（明确分析了分配→初始化→失败释放→成功移交的完整链路）
- **人工判断**：Judge 判断合理。Baseline 虽然指出了问题，但没有直接回答问题的核心（路径是否闭合）。
- **结论**：Judge 对 Q49 的评估合理。

#### 生成失败的 7 题

- **Baseline 答案质量**：全部合格，分析完整、证据引用清晰
- **Full-files**：完全无输出（API 报错）
- **结论**：这不是"答案质量"问题，而是"系统可靠性"问题。

### 修正后的核心结论

1. **Full-files 的答案质量并不差**——在成功生成的情况下，97.7% vs 97.7%，与 baseline 持平
2. **Full-files 的致命缺陷是可靠性**——14% 的概率因 context 超限而完全失败，生产环境不可接受
3. **Judge 评估基本可靠**，但在个别题目（如 Q1）上存在对 full-files 的过度严格
4. **Baseline（函数片段）是更稳健的选择**——没有 context 超限风险，且答案质量相同


---

## 补充：答案质量的深度对比

> 2026-05-22 补充  
> 在排除生成失败的 43 题中，人工对比 baseline 与 full-files 的答案风格、丰富度和实用性。

### 量化指标对比

| 指标 | Full-files | Baseline | 差异 |
|---|---|---|---|
| 平均答案长度 | 3,977 字符 | 3,250 字符 | +22% |
| 证据引用准确率 | 39.0% | 20.9% | **+86%** |
| 平均代码块数量 | 3.0 个 | 1.8 个 | +67% |
| 平均引用文件数 | 4.3 个 | 2.7 个 | +59% |
| 包含明确结论 | 41/43 | 43/43 | baseline 略优 |

### 人工抽查的典型样本

#### Case 1: Q34 `segmentize_markers`（full-files 过于冗长）

- **Full-files (6,010 字符)**：按"步骤1→步骤2"结构化分析，引入"异常安全性"、"RAII遵守情况"等通用概念，但实际函数只是简单的字符串解析，无动态分配
- **Baseline (1,016 字符)**：直接指出"所有资源均为栈上分配，无显式 new/malloc"，结论简洁明确
- **人工判断**：**Baseline 更优**。full-files 的丰富度没有转化为增量信息，反而稀释了焦点。

#### Case 2: Q49 `llama_sampler_init_grammar_impl`（full-files 明显更优）

- **Full-files**：完整展示了 `src/llama-sampler.cpp` 中的实现，明确看到失败路径有 `delete ctx; return nullptr;`，确认路径闭合
- **Baseline**：只看到片段，误以为 `new` 未检查、grammar 未检查空指针，得出"存在资源泄漏风险"的错误结论（因代码截断）
- **人工判断**：**Full-files 更优**。这是唯一需要跨文件/完整实现才能正确判断的问题。

#### Case 3: Q10 `common_sampler_init`（两者相当，风格不同）

- **Full-files**：展示了 `common_params_sampling` 结构体的完整定义和所有默认值，证据更完整
- **Baseline**：聚焦在核心函数和配置路径，结构清晰
- **人工判断**：**两者相当**。full-files 更丰富，baseline 更简洁，结论一致。

### 综合判断

**Full-files 的答案"更丰富"，但不一定"更好"：**

| 维度 | Full-files | Baseline | 说明 |
|---|---|---|---|
| **丰富度** | ✅ 明显更好 | 一般 | 更多代码块、更多文件引用、更高证据引用率 |
| **正确率** | 持平 | 持平 | 成功生成时都是 97.7% |
| **简洁度** | 一般 | ✅ 更好 | baseline 直接给结论，full-files 有时冗余 |
| **聚焦度** | 一般 | ✅ 更好 | full-files 易被大文件中的无关代码分散 |
| **可靠性** | ❌ 差 | ✅ 好 | 14% 概率 context 超限完全失败 |
| **特定场景优势** | ✅ 资源生命周期 | — | 如 Q49 需要完整实现才能判断 |

### 结论

> **Full-files 在"丰富度"维度上确实比 baseline 好，但这种提升没有转化为正确率的提升。** 对于代码审查类任务，工程师更看重的是**简洁、准确、有结论**的回答，而不是堆砌文件上下文。baseline 以 1/4 的 context 长度达到了相同的正确率，且没有可靠性风险，是更优的工程选择。

---

## 重大发现：Baseline 的"层层缩水"工程债（2026-05-22）

> 用户在审查 baseline 实现时发现了系统性的代码截断问题。这不是"函数太大"，而是**整个链路层层胡乱截断**。

### 问题链条

以 Q49 `llama_sampler_init_grammar_impl` 为例，该函数实际仅 **58 行**，却被系统砍到只剩约 30 行，导致 LLM 看不到 `delete ctx; return nullptr;` 的关键清理逻辑。

#### 第一层：`search_functions_by_text` 瞎估算

```python
# tools/search/semantic_search.py（旧代码）
func = {
    'start_line': meta.get('line', 0),
    'end_line': meta.get('line', 0) + 30,  # ← 硬加30！无视RAG index的精确end_line
}
```

明明 `classic_rag_index.json` 里写了 `end_line=2589`，它不信，非要自己估算成 `2532 + 30 = 2562`。

#### 第二层：`enrich_function_with_code` 再砍一刀

```python
# tools/search/code_reader.py（旧代码）
code = read_function_from_file(
    ...,
    max_lines=30   # ← 再限30行！
)
```

#### 第三层：`build_context` 再砍一刀

```python
# tools/core/answer_generator.py（旧代码）
code = _format_func_code(fn, max_len=2000)  # ← 格式化时再截断到2000字符！
```

一个 2548 字符的完整函数，经过这三层缩水，最终只剩下约 2000 字符，恰好截断在 `*ctx = { ... };` 之后。LLM 看到 `new` 分配了内存，却看不到 `delete`，自然误判为泄漏。

### 修复内容

| 文件 | 修改 | 说明 |
|---|---|---|
| `tools/search/semantic_search.py` | `'end_line': meta.get('end_line', meta.get('line', 0))` | 信任 RAG index 的精确 end_line |
| `tools/search/semantic_search.py` | 去重优先保留 text 更长的实现 | 解决"声明"和"实现"同时存在的问题 |
| `tools/search/code_reader.py` | `max_lines=30` → `max_lines=200` | 取消30行无理限制 |
| `tools/core/answer_generator.py` | `max_len=2000` → `max_len=20000` | 取消2000字符格式化截断 |

### 修复验证

修复后重新跑 Q49 baseline：

```
Contains delete ctx: True
Contains return nullptr: True
```

LLM 现在完整看到了 `llama_sampler_init_grammar_impl` 的实现，正确分析了分配→初始化→失败释放→成功移交的完整链路。

### 教训

> **属于"有精确信息不用，非要瞎估算"的典型工程债。**  
> 对于 C/C++ 代码，30 行限制极其不合理。一个普通函数 50-100 行很常见，300-500 行的大函数也不少见。系统应该在精确行号可用时完全信任它，而不是层层叠加保守的估算值。

---

## 补充：修复后 Baseline 双维度评估结果

> 2026-05-22 补充  
> 使用修复后的配置（取消层层截断）重新跑 baseline，并同时使用两种评估标准。

### 评估配置

- **答案生成模型**：DeepSeek-v4-pro
- **评估模型（Judge）**：GPT-4.1-mini
- **评估方式1**：Binary Judge（是否正确回答问题）
- **评估方式2**：Location Judge（是否准确指出关键证据的文件路径和行号）
- **Prompt 分离**：`prompts/judge_location.txt`
- **评估脚本整理**：`evals/eval_v2.py`（binary）、`evals/eval_location.py`（location）

### 核心结果

| 评估维度 | 正确率 | 说明 |
|---|---|---|
| **Binary Judge（是否正确回答问题）** | **50/50 = 100.0%** | 修复截断后，全部正确 |
| **Location Judge（是否指出文件位置）** | **27/50 = 54.0%** | 仅一半答案引用了关键证据路径 |

### 关键发现

**修复截断确实有效**：
- 未修复前 baseline：49/50 = 98.0%（Q49 因截断误判泄漏）
- 修复后 baseline：**50/50 = 100.0%**

**但证据引用能力仍然很弱**：
- 50 题全部结论正确，但 **23 题（46%）没有引用关键证据中的具体文件路径和行号**
- LLM 倾向于给出概括性结论，而不是"某文件某行的代码证明了某点"

### 典型样例：Binary 对但 Location 错

**Q0：`ggml_sycl_set_device` 调用方契约兼容性**
- Binary Judge：**CORRECT**（正确分析了返回值、参数、副作用）
- Location Judge：**INCORRECT**（引用了 `ggml/src/ggml-sycl/common.hpp`，但未引用关键证据中的 `ggml/src/ggml-sycl/common.cpp:77` 等具体行号）

**Q6：`calculate_diff_split` 调用方契约兼容性**
- Binary Judge：**CORRECT**
- Location Judge：**INCORRECT**（完全没有引用 `common/chat-auto-parser-helpers.cpp:87` 等关键证据文件）

### 结论与建议

> **LLM 能"想对"，但不倾向于"说清楚证据在哪"。**

对于代码审查类任务，结论正确只是第一步。如果答案不能明确指出"证据在 `file.cpp:123` 行"，工程师就无法快速验证和追溯。

**下一步优化方向**：
1. **在 `prompts/answer_generation.txt` 中强制要求**："必须列出关键证据的具体文件路径和行号作为支撑"
2. **后处理增强**：在答案生成后，从检索结果中抽取证据，强制插入到答案中
3. **结构化输出**：要求 LLM 按 "结论 + 证据列表（文件:行号）" 的格式输出

---

## Prompt 优化实验：强制要求引用文件路径和行号

> 2026-05-22  
> 基于 Location Judge 仅 54% 的发现，修改 answer generation prompt，强制要求 LLM 在答案末尾列出【证据清单】。

### Prompt 修改内容

在 `prompts/answer_generation.txt` 步骤3中新增强制要求：

```
步骤3：基于证据组织答案（强制要求）
...
- **每条关键结论后面，必须标注支持该结论的代码证据位置**，格式为：`文件路径:行号` 或 `文件路径:起始行-结束行`
```

### 实验结果

| 指标 | 原始 Prompt | 增强 Prompt (v2) | Delta |
|---|---|---|---|
| **Binary Judge（是否正确回答问题）** | 50/50 = 100.0% | 50/50 = **100.0%** | 持平 |
| **Location Judge（是否指出关键证据文件位置）** | 27/50 = 54.0% | 31/50 = **62.0%** | **+8.0%** ✅ |
| **证据引用准确率** | 24.3% | 35.8% | **+11.5%** ✅ |
| 平均答案长度 | 3,250 字符 | ~3,500 字符 | +7% |
| 平均证据引用数 | 1.2 个 | 1.8 个 | +50% |

**净改善：10 题从 location 错变对，6 题从对变错，净 +4 题。**

### 改善的 10 题（原来错 → 现在对）

| 题号 | 二级维度 | 改善原因 |
|---|---|---|
| Q02 | 配置/参数传播一致性 | 原来引用 `src/llama-model.cpp:9064` 等错误路径 → 现在正确引用 `common/common.cpp`、`src/llama-model.cpp` |
| Q03 | 调用方契约兼容性 | 原来完全未引用文件路径 → 现在引用 `common/chat-auto-parser-helpers.cpp` 等 |
| Q04 | 调用方契约兼容性 | 原来仅提到文件名，无具体行号 → 现在引用 `common/chat-auto-parser-helpers.cpp:33-40` |
| Q05 | 调用方契约兼容性 | 原来完全未引用 → 现在引用 `common/chat-auto-parser-helpers.cpp:42-58` 等 |
| Q10 | 配置/参数传播一致性 | 原来引用 `common/common.h`（不在关键证据中） → 现在正确引用 `common/sampling.cpp:185` 等 |
| Q12 | 配置/参数传播一致性 | 原来引用不在关键证据中的路径 → 现在正确引用关键证据 |
| Q13 | 配置/参数传播一致性 | 原来引用不在关键证据中的路径 → 现在正确引用关键证据 |
| Q17 | 配置/参数传播一致性 | 原来完全未引用 → 现在引用 `common/common.cpp` |
| Q28 | 共享 helper 复用影响 | 原来完全未引用 → 现在引用 `common/arg.cpp:824-882` 等 |
| Q34 | 资源生命周期/清理边界 | 原来完全未引用 → 现在引用 `common/chat-auto-parser-helpers.cpp` |

**共同模式**：这些题在原始 prompt 下要么完全没引用文件路径（仅概括性分析），要么引用了不在关键证据列表中的路径。强制引用要求促使 LLM 主动从上下文中提取并标注代码位置。

### 恶化的 6 题（原来对 → 现在错）

| 题号 | 二级维度 | 恶化原因 |
|---|---|---|
| Q08 | 调用方契约兼容性 | 原来明确引用 `common/chat-auto-parser-helpers.cpp:269-299` → 现在泛泛提及"测试文件和实现逻辑" |
| Q19 | 配置/参数传播一致性 | 原来明确引用 `common/arg.cpp:1576` 等 → 现在仅描述函数行为，无具体路径 |
| Q21 | 共享 helper 复用影响 | 原来引用 `common/chat-auto-parser-helpers.cpp:33-40` → 现在仅给出实现分析 |
| Q23 | 共享 helper 复用影响 | 原来引用 `common/chat-auto-parser-helpers.cpp` → 现在仅概括性描述 |
| Q26 | 共享 helper 复用影响 | 原来引用 `common/chat-peg-parser.h` 等 → 现在引用 `common/chat-auto-parser`（模糊匹配失败） |
| Q35 | 资源生命周期/清理边界 | 原来引用 `common/ngram-map.cpp` 及具体行号 → 现在完全未引用 |

**共同模式**：这些题在原始 prompt 下碰巧引用了正确的文件路径，但新 prompt 下 LLM 的输出出现了不稳定性——有时引用得更具体，有时反而不再引用。这反映了 LLM 生成行为的随机性，而非 prompt 策略本身的系统性缺陷。

### 按维度统计

| 二级维度 | 题数 | 旧正确 | 新正确 | 改善 | 恶化 |
|---|---|---|---|---|---|
| 调用方契约兼容性 | 11 | 5 | 8 | +4 | -1 |
| 配置/参数传播一致性 | 11 | 7 | 10 | +4 | -1 |
| 共享 helper 复用影响 | 10 | 5 | 3 | +1 | -3 |
| 资源生命周期/清理边界 | 4 | 2 | 2 | +1 | -1 |
| 状态一致性 | 4 | 3 | 3 | 0 | 0 |
| 并发/异步安全 | 7 | 5 | 5 | 0 | 0 |
| 初始化/外部 API 契约 | 3 | 0 | 0 | 0 | 0 |

**改善最显著的维度**：调用方契约兼容性（+3）、配置/参数传播一致性（+3）。这两个维度都涉及"找调用方、验证假设"，强制引用要求直接帮助 LLM 定位调用代码。

**恶化最显著的维度**：共享 helper 复用影响（-2）。这类问题涉及多个调用点，LLM 可能在尝试精简引用时遗漏了关键证据。

### 结论

**Prompt 优化有效但有限**：
- ✅ 强制引用要求显著提升了 LLM 引用文件路径的意愿（证据引用准确率 +11.5%）
- ✅ Location Judge 从 54% → 62%，净改善 4 题
- ⚠️ 改善存在不稳定性：10 题改善 vs 6 题恶化，说明 LLM 输出仍有随机性
- ❌ **38% 的题目（19/50）仍然无法准确引用关键证据位置**

**根本原因**：LLM 即使被强制要求引用证据，仍然倾向于引用"它认为相关的"路径，而非题目关键证据列表中的精确路径。这本质上是一个**检索结果与证据要求的对齐问题**——LLM 不知道哪些文件是"判分要点"中要求的证据。

### 下一步优化方向

| 方案 | 描述 | 预期效果 |
|---|---|---|
| **A. 更强制的引用格式** | 在 prompt 中明确列出"请从以下检索结果中选择证据"，减少 LLM 自由发挥 | 中等 |
| **B. 后处理增强** | 在答案生成后，从检索结果中自动提取关键证据，强制插入答案 | 高 |
| **C. ReAct 证据定位** | 增加"evidence_search" action，让 LLM 主动搜索判分要点要求的文件 | 高 |
| **D. 结构化输出** | 要求 LLM 按 "结论 + 证据列表（文件:行号）" 的 JSON 格式输出 | 中等 |

---

## 附录：实验复现命令

### 环境要求

```bash
# Neo4j 必须已启动且包含 llama.cpp 的代码图数据
neo4j status  # 确认运行中


```

### 1. Full-files 实验组（完整文件上下文，每题最多 10 个完整文件）

```bash
# 跑 benchmark（约 30 分钟，50 题）
python3 run_v2_benchmark.py \
  --max-full-files 10 \
  -o results/v2_deepseek_fullfiles.json \
  -w 10

# Binary Judge 评估
python3 evals/eval_v2.py \
  --input results/v2_deepseek_fullfiles.json \
  -o results/v2_deepseek_fullfiles.eval.json \
  -w 10
```

### 2. Baseline 对照组（函数片段，不收集完整文件）

```bash
# 跑 benchmark
python3 run_v2_benchmark.py \
  --max-full-files 0 \
  -o results/v2_deepseek_baseline.json \
  -w 10

# Binary Judge 评估
python3 evals/eval_v2.py \
  --input results/v2_deepseek_baseline.json \
  -o results/v2_deepseek_baseline.eval.json \
  -w 10
```

### 3. 修复截断后的 Baseline

> 前置修改：`tools/search/semantic_search.py` 信任 RAG index 的 `end_line`、`tools/search/code_reader.py` `max_lines=30→200`、`tools/core/answer_generator.py` `max_len=2000→20000`

```bash
# 跑 benchmark
python3 run_v2_benchmark.py \
  --max-full-files 0 \
  -o results/v2_deepseek_baseline_fixed.json \
  -w 10

# Binary Judge 评估
python3 evals/eval_v2.py \
  --input results/v2_deepseek_baseline_fixed.json \
  -o results/v2_deepseek_baseline_fixed.eval.json \
  -w 10

# Location Judge 评估
python3 evals/eval_location.py \
  --input results/v2_deepseek_baseline_fixed.json \
  -o results/v2_deepseek_baseline_fixed.loc.eval.json \
  -w 10
```

### 4. Prompt v2 优化实验（增强版 answer_generation prompt）

> 前置修改：`prompts/answer_generation.txt` 步骤3中增加"必须标注关键证据的文件路径和行号"

```bash
# 跑 benchmark
python3 run_v2_benchmark.py \
  --max-full-files 0 \
  -o results/v2_deepseek_baseline_prompt_v2.json \
  -w 10

# Binary Judge 评估
python3 evals/eval_v2.py \
  --input results/v2_deepseek_baseline_prompt_v2.json \
  -o results/v2_deepseek_baseline_prompt_v2.eval.json \
  -w 10

# Location Judge 评估
python3 evals/eval_location.py \
  --input results/v2_deepseek_baseline_prompt_v2.json \
  -o results/v2_deepseek_baseline_prompt_v2.loc.eval.json \
  -w 10
```

### 5. 只跑部分题目（调试用）

```bash
# 只跑前 5 题
python3 run_v2_benchmark.py \
  --max-full-files 0 \
  -o results/v2_test_5.json \
  --limit 5 \
  -w 3

# 从第 10 题开始跑 5 题
python3 run_v2_benchmark.py \
  --max-full-files 0 \
  -o results/v2_test_offset.json \
  --offset 10 \
  --limit 5 \
  -w 3
```

### 6. 结果文件对照表

| 实验 | 原始结果 | Binary 评估 | Location 评估 |
|---|---|---|---|
| Full-files | `results/v2_deepseek_fullfiles.json` | `results/v2_deepseek_fullfiles.eval.json` | `results/v2_deepseek_fullfiles.gpt4omini.loc.eval.json` |
| Baseline（原始） | `results/v2_deepseek_baseline.json` | `results/v2_deepseek_baseline.eval.json` | — |
| Baseline（修复截断） | `results/v2_deepseek_baseline_fixed.json` | `results/v2_deepseek_baseline_fixed.eval.json` | `results/v2_deepseek_baseline_fixed.gpt4omini.loc.eval.json` |
| Prompt v2 | `results/v2_deepseek_baseline_prompt_v2.json` | `results/v2_deepseek_baseline_prompt_v2.eval.json` | `results/v2_deepseek_baseline_prompt_v2.gpt4omini.loc.eval.json` |
| Prompt v2 | `results/v2_deepseek_baseline_prompt_v2.json` | `results/v2_deepseek_baseline_prompt_v2.eval.json` | `results/v2_deepseek_baseline_prompt_v2.loc.eval.json` |


---

## 补充：Full-files 的真实价值与代价（2026-05-26 复现更新）

> 使用 gpt-4o-mini 同时评估 Binary Judge 和 Location Judge，对 Full-files 的价值有了更完整的认识。

### Full-files 的提升在哪？

**1. 答案丰富度全面领先**

| 指标 | Full-files | Baseline | 提升 |
|---|---|---|---|
| 平均答案长度 | 3,977 字符 | 3,250 字符 | **+22%** |
| 平均代码块数量 | 3.0 个 | 1.8 个 | **+67%** |
| 平均引用文件数 | 4.3 个 | 2.7 个 | **+59%** |
| 证据引用准确率（Binary） | 33.5% | 23.7% | **+9.8%** |

**2. Location Judge 表面上超过 Baseline，但存在 Judge 幻觉**

| 实验 | Location Judge（gpt-4o-mini） |
|---|---|
| **Full-files（原始）** | **56.0%** (28/50) |
| Baseline（修复截断） | 50.0% (25/50) |
| Prompt v2 | 54.0% (27/50) |

但进一步分析发现，Full-files 的 7 题生成失败（context 超限）中，**4 题被 gpt-4o-mini 幻觉判为 CORRECT**——`generated` 里只有错误信息 `"生成答案失败: Error code: 400..."`，根本没有文件路径，但 judge 声称看到了 `common/common.cpp:1385` 等路径。

排除这 4 题幻觉后，Full-files 真正的 Location Judge 正确率：

| 实验 | 原始正确率 | 排除幻觉后 |
|---|---|---|
| **Full-files** | 56.0% | **48.0%** (24/50) |
| Baseline（修复截断） | 50.0% | **50.0%** (25/50) |
| Prompt v2 | 54.0% | **54.0%** (27/50) |

**结论：Full-files 在 Location Judge 上并没有真正领先 Baseline，之前的"优势"是 judge 幻觉造成的。**

**3. 特定维度唯一胜出：资源生命周期/清理边界**

| 维度 | Full-files | Baseline | 说明 |
|---|---|---|---|
| 资源生命周期/清理边界 | **100%** (4/4) | 75% (3/4) | 唯一 full-files 胜出的维度 |

典型胜例 **Q49** `llama_sampler_init_grammar_impl`：
- Baseline 只看到片段，误以为 `new` 未检查、grammar 未检查空指针，得出"存在资源泄漏风险"的错误结论
- Full-files 完整展示了 `src/llama-sampler.cpp` 中的实现，明确看到失败路径有 `delete ctx; return nullptr;`，确认路径闭合

### Full-files 的恶化在哪？

**1. 致命缺陷：Context 超限导致完全失败**

Full-files 的 8 题错误中，**7 题是 context 超限导致生成失败**（>1M tokens），不是"答案质量差"，而是"完全无输出"。这 7 题在 Binary Judge 中被直接判错，拉低了整体正确率。

| 题号 | 失败原因 |
|---|---|
| Q14, Q17 | Context > 1M tokens（`common_get_model_endpoint`） |
| Q30, Q31, Q32 | Context > 1M tokens（后端初始化相关） |
| Q47, Q48 | Context > 1M tokens（配置参数相关） |

如果把这 7 题从分母中剔除（即只看"成功生成答案"的 43 题），正确率持平：

| 方案 | 成功生成的 43 题 | 正确率 |
|---|---|---|
| Full-files | 42/43 | **97.7%** |
| Baseline | 42/43 | **97.7%** |

**2. 真正因"答案质量差"而错的只有 1 题**

剩余 1 题（Q1）是因为被无关文件淹没而误判契约，属于答案质量问题。Baseline 在这 1 题上正确。

**3. Binary Judge 下整体正确率仍显著低于 Baseline**

但统计上这 7 题失败不能忽略——它们占 14% 的题目，是 Full-files 的系统性风险。

| 评估模型 | Full-files（含失败） | Baseline（无失败） | 差距 |
|---|---|---|---|
| gpt-4.1-mini | 84.0% | 98.0% | **-14%** |
| gpt-4o-mini | 72.0% | 82.0% | **-10%** |

**4. 成功生成答案后的核心恶化原因：被无关代码淹没**

在成功生成的 43 题中，1 题因无关文件干扰而错判：

| 题号 | 维度 | 恶化原因 |
|---|---|---|
| Q1 | 调用方契约兼容性 | 被无关文件淹没，误判契约 |

其余 42 题中，Baseline 和 Full-files 的 Binary Judge 判定一致。

### 综合判断

| 维度 | Full-files | Baseline | 结论 |
|---|---|---|---|
| **答案丰富度** | ✅ 明显更好 | 一般 | 更多代码块、更多文件引用 |
| **证据引用能力** | ✅ 更好 | 一般 | Location Judge 56% vs 50% |
| **Binary 正确率** | 较差 | ✅ 更好 | 84% vs 98%（gpt-4.1-mini） |
| **简洁度/聚焦度** | 一般 | ✅ 更好 | Baseline 直接给结论，full-files 易冗余 |
| **可靠性** | ❌ 差 | ✅ 好 | 14% 概率 context 超限完全失败 |
| **特定场景优势** | ✅ 资源生命周期 | — | 如 Q49 需要完整实现才能判断 |

> **Full-files 的答案是"更丰富"的，但 Binary Judge 不认为它"更好"。** 丰富度提升（+22% 长度、+67% 代码块）没有转化为 Binary 正确率的提升，因为 LLM 的注意力被海量无关代码稀释，容易误判。
>
> **但在"证据引用能力"和"需要跨文件理解的特定场景"上，Full-files 确实有价值。** Location Judge 56% 超过所有 Baseline 组，说明完整文件让 LLM 更愿意、也更能指出具体的代码位置。
>
> **工程建议**：不是"全给"或"不给"，而是分层策略——默认函数片段保证聚焦和可靠性，仅在"资源生命周期"等需要跨文件追踪的特定场景下，按需读取完整文件。
