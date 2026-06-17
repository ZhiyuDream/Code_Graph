# Repository Audit 的两阶段结构：语义定位 + 符号化调查

## 摘要

针对 llama.cpp 仓库的 post-hoc audit benchmark（前 15 题），我们把 **repository audit** 重新界定为两个连续阶段：

1. **语义定位（Semantic Localization）**：`Question → Code Region`，即从自然语言问题定位到代码库中一个或多个相关区域。
2. **符号化调查（Symbol-Guided Investigation）**：`Region → Evidence`，即从已定位的区域出发，通过符号（symbol）与结构化工具扩展出完整的证据链。

分层实验表明：

| 条件 | 平均覆盖率 | 引用全对 |
|------|-----------|----------|
| Gold-files Oracle（证据已给定） | **100%** | **15/15** |
| Symbol Fast Path（入口为 gold definition） | **93.3%** | **13/15** |
| Multi-Entry Symbol Expansion（n=3，embedding 候选） | **60.6%** | **6/15** |
| LLM-Guided Region → Evidence（Top-5 region，无硬编码优先级） | **40.0%** | **2/15** |
| End-to-End（embedding Top-1 + symbol fast path） | **42.8–45.0%** | **4–5/15** |
| Entry-only Dynamic Agent（入口为 gold） | 66.7% | 4/15 |

关键发现：

- **Stage 2（Region → Evidence）在当前设置下几乎已被解决**：只要给定一个高质量的 definition 入口，基于 symbol 的结构化扩展即可达到 93.3% 的覆盖率和 13/15 的全对率。
- **Stage 1（Question → Code Region）是主导瓶颈**：embedding 语义检索 Top-1 命中 73.3% 的问题，但 Top-10 仅能召回 51.1% 的 gold files；直接从问题中提取 symbol 的成功率几乎为 0。
- **端到端性能主要受 Stage 1 质量制约**：当入口从 gold definition 换成 embedding 检索结果时，覆盖率从 93.3% 骤降至 42.8–45.0%；引入多个候选入口可部分缓解，n=3 时回升到 60.6%。

因此，repository audit 的核心挑战不是"LLM 能否理解证据"，而是**如何把自然语言问题可靠地锚定到代码库中正确的符号区域**。

---

## 1. 研究问题：Repository Audit 到底是什么？

Repository audit 任务通常被描述为：给定一个关于代码库的技术问题，让 Agent 自己调查代码并给出带引用的回答。此前的工作把重点放在"Agent 如何逐步推理和搜索"，但本研究通过逐步剥离变量，发现可以把任务拆成两个性质不同的子问题：

- **Stage 1：语义定位**  
  问题通常是自然语言描述（例如"AI 修改了设备切换逻辑，担心调用方对返回值的理解不一致"），并不直接包含函数名。Agent 需要把这段描述映射到代码库中的一个或多个相关文件/区域。

- **Stage 2：符号化调查**  
  一旦进入代码区域，真正有效的导航单位是 **symbol**（函数、类、全局变量、宏），而不是 topic、directory 或自然语言关键词。Agent 需要从初始区域中提取关键 symbol，并通过 grep、caller/callee 等结构化工具扩展出完整证据链。

两阶段框架改变了对瓶颈的判断：如果 Stage 2 的上限已经接近 100%，那么端到端差距主要来自 Stage 1 的质量，而不是 Agent 的推理或证据解释能力。

---

## 2. 实验与结果

### 2.1 Stage 2 上限：给定黄金入口，symbol 扩展能走多远？

#### 2.1.1 Oracle：Question + Gold Files

把 benchmark 中所有 gold evidence 文件直接提供给 LLM，测量"证据已在面前时，LLM 能否正确理解并引用"。

结果：**100% 平均覆盖率，15/15 引用全对。**

这说明 LLM 的阅读、理解和引用能力都不是瓶颈。

#### 2.1.2 Symbol Fast Path（入口来自 gold definition）

设计极简流程验证 Stage 2 上限：

```text
Question + Gold Definition File
  ↓
LLM 只选 1 个最关键 symbol
  ↓
grep_symbol(symbol) 找到所有出现位置
  ↓
按规则排序：src/common/ggml-src 优先，tests/examples 降权
  ↓
自动读取前 5 个文件
  ↓
生成答案
```

这个流程不让 LLM 每步猜文件位置，只让 LLM 做"从入口文件中选 symbol"这一件事。

结果：**平均覆盖率 93.3%，13/15 题引用全对。**

#### 2.1.3 Entry-only Dynamic Agent（入口来自 gold definition）

作为对比，让 Agent 每步读取文件、提取证据、更新搜索方向、选择下一个文件。

结果：**平均覆盖率 66.7%，仅 4/15 题引用全对。**

同样给定 gold definition 入口，Symbol Fast Path 比动态 Agent 高出 26.6 个百分点。这说明：一旦进入代码区域，**基于 symbol 的结构化扩展优于自由推理式导航**。

### 2.2 Stage 1 难度：从自然语言问题到代码区域

#### 2.2.1 Question → Entry：Embedding 语义检索

直接根据问题文本找到合理入口文件，对比四种方法：

| 方法 | Top-1 Hit | Top-5 Hit | Top-10 Hit |
|------|-----------|-----------|------------|
| Embedding 语义检索 | **73.3%** | **80.0%** | **86.7%** |
| LLM 关键词 + Grep | 20.0% | 20.0% | 33.3% |
| LLM Symbol + Grep | 6.7% | 6.7% | 6.7% |
| LLM 多假设路由 + Grep | 0.0% | 0.0% | 6.7% |

Embedding 显著优于基于文本提取的 grep 方法。但它解决的是"问题→相关文件"，而非"问题→最佳 definition 文件"。

进一步分析 embedding 召回能力：平均每题有 3.13 个 gold files，embedding Top-10 平均仅召回 1.67 个，**gold file 召回率 51.1%**。这说明 embedding 能把问题定位到一个大致区域，但通常无法一次性召回完整证据链。

#### 2.2.2 Question → Symbol：直接从问题文本提取符号

如果 Stage 2 是 symbol-guided，那么能否跳过 embedding，直接从问题中提取 symbol？

让 LLM 仅根据问题文本提取可能相关的函数/类/全局变量名，然后检查这些 symbol 是否出现在 gold files 中。

结果：**总预测 symbol 数 6，命中 gold file 的 symbol 数 0，命中率 0.0%，至少命中一题的比例 0.0%。**

大部分问题返回 `UNKNOWN` 或空列表。这说明**自然语言问题本身通常不包含可直接搜索的符号**，因此不能期望 LLM 凭空猜出 symbol。但这并不意味着 symbol 不可获取——更合理的做法是先通过 embedding 定位到相关 region，再让 LLM 从 region 的代码内容中识别 symbol（见 2.3.3）。

### 2.3 Stage 1 + Stage 2：端到端 Pipeline

#### 2.3.1 Embedding Top-1 + Symbol Fast Path

组合两层的最佳方法：

```text
Question
  ↓
Embedding 检索 → Top-1 Entry File
  ↓
Symbol Fast Path 扩展
  ↓
生成答案
```

结果：**平均覆盖率 42.8–45.0%，4–5/15 题引用全对。**

与 gold-entry Symbol Fast Path（93.3%）相比，端到端性能下降了约 48 个百分点。

#### 2.3.2 Multi-Entry Symbol Expansion

由于 embedding Top-10 仅召回 51.1% 的 gold files，我们尝试从多个候选入口并行扩展：

```text
Question
  ↓
Embedding 检索 → Top-k Candidates
  ↓
对前 M 个候选分别做短 symbol fast path
  ↓
合并所有访问文件
  ↓
生成答案
```

初步结果（n=3）：**平均覆盖率 60.6%，6/15 题引用全对。**

多入口扩展显著优于单入口，但仍远低于 gold-entry 上限，说明候选入口中仍有大量噪声，或合并策略需要改进。

> **注**：早期 n=1/n=2 的独立运行因 LLM 温度设置产生了较大方差。后续用确定性 symbol 选择重新测量的成长曲线（n=1..5）效果较差（n=3 仅 42.2%），主要原因是启发式 symbol 选择无法替代 LLM 基于问题的判断。

#### 2.3.3 LLM-Guided Region → Evidence（无硬编码优先级）

为了避免仓库相关的硬编码规则（如 `src/` 优先、`tests/` 降权），我们设计了一个更通用的流程：

```text
Question
  ↓
Embedding 检索 → Top-5 Region
  ↓
LLM 基于 Question + Region 内容选择关键 symbols 和下一步要读的文件
  ↓
读取 LLM 选择的文件 + grep 结果
  ↓
生成答案
```

在这个流程中，**所有文件重要性判断都交给 LLM**，不预设优先级。

结果：**平均覆盖率 40.0%，2/15 题引用全对。**

这个数字低于带硬编码优先级的 symbol fast path（42.8–45.0%）和 multi-entry（60.6%），但它是一个更通用、更可迁移的 baseline。更重要的是，它再次确认：**即使 LLM 能从 region 内容中识别 symbol，端到端性能仍受 region 本身质量限制**。当 region 没有包含核心 definition 文件时，LLM 也无法凭空找到正确证据。

---

## 3. 结果分析

### 3.1 Stage 2 接近解决，但依赖入口质量

| 条件 | 覆盖率 | 与 Oracle 差距 |
|------|--------|----------------|
| Oracle | 100% | — |
| Symbol Fast Path（gold entry） | 93.3% | 6.7% |
| Multi-Entry（embedding candidates, n=3） | 60.6% | 39.4% |
| End-to-End（embedding top-1） | 42.8–45.0% | 55.0–57.2% |
| LLM-Guided Region → Evidence | 40.0% | 60.0% |

Stage 2 在入口质量高时几乎填平了与 Oracle 的差距；入口质量下降时，性能迅速衰减。这符合"Region → Evidence 相对容易，Question → Region 相对困难"的假设。

### 3.2 为什么 Stage 1 困难？

问题文本与代码符号之间存在语义鸿沟：

- 问题描述现象（"设备切换"、"模板选择"、"默认参数初始化"），不给出具体函数名。
- 直接从问题做 keyword/symbol grep 效果很差（Top-1 最高 20%）。
- Embedding 能跨鸿沟定位到语义相关区域，但召回的粒度较粗：Top-10 仅能召回 51.1% 的 gold files。

LLM-Guided Region → Evidence 实验进一步验证：当把文件重要性判断完全交给 LLM 时，端到端 coverage 也只有 40.0%。这说明**问题不在 LLM 不会判断文件重要性，而在 embedding 召回的 region 本身就不够完整**。因此 Stage 1 不是简单的"检索问题"，而是**自然语言语义到代码区域的映射问题**。

### 3.3 为什么 Stage 2 有效？

一旦进入代码区域，信息结构变得高度结构化：

- 函数名、类名、调用关系、定义位置都是离散符号。
- Grep 可以精确定位 symbol 的所有出现。
- 文件优先级规则可以过滤测试/示例噪声。

这与 topic-based document routing（~13.8% 文件命中率）形成鲜明对比，说明**代码层面的最优导航单位是 symbol**。

### 3.4 端到端差距的来源

端到端与 gold-entry 的 48 个百分点差距，可以主要归因于 Stage 1：

- Embedding Top-1 只有 73.3% 的问题命中相关入口。
- 即使命中，入口通常是"相关文件"而非"最佳 definition 文件"。
- Embedding Top-10 仅能召回 51.1% 的 gold files，说明语义定位的粒度不足。
- 当 region 不完整时，无论 Stage 2 使用硬编码优先级（45%）还是 LLM 判断（40%），都无法弥合差距。

Multi-entry 把覆盖率从 ~45% 提升到 ~60%，说明通过增加候选入口可以部分补偿 Stage 1 的不确定性，但也带来新的挑战：如何合并、去重和排序多个入口的证据。

---

## 4. 关键结论

1. **Repository audit 应被建模为"语义定位 + 符号化调查"的两阶段任务。**
   - Stage 1：`Question → Code Region`
   - Stage 2：`Region → Evidence`

2. **Stage 2 的上限很高，但前提是有高质量的 region。**
   - 给定 gold definition 入口，symbol fast path 达到 93.3% coverage。
   - 结构化 symbol 扩展显著优于自由推理式 Agent 导航。
   - 当 region 不完整时，即使让 LLM 自行判断文件重要性，coverage 也只有 40.0%。

3. **Stage 1 是端到端性能的主导瓶颈。**
   - Embedding 可以定位到语义区域（Top-1 73.3%），但召回粒度不足（Top-10 仅召回 51.1% gold files）。
   - 直接从问题文本提取 symbol 几乎不可能（0% 命中），但从 region 内容中识别 symbol 是可行路径。

4. **端到端 pipeline 的性能受 region 质量严重制约。**
   - 入口从 gold 换成 embedding Top-1 时，coverage 从 93.3% 降至 42.8–45.0%。
   - 多入口扩展可部分缓解，n=3 时回升到 60.6%。
   - LLM-guided 无优先级版本为 40.0%，说明通用 Stage 2 仍有提升空间，但主要限制仍在 Stage 1。

5. **提升 repository audit 性能的关键在于改进语义定位阶段。**
   - 目标不是让 Agent "更会搜索"，而是让 Agent 更可靠地把问题锚定到包含核心 definition 的代码区域。

---

## 5. 下一步建议

### 5.1 改进 Stage 1：语义定位

1. **Top-k 候选 + 入口质量评估**  
   不直接取 embedding Top-1，而是返回 Top-k 候选，并对每个候选执行一次短步骤的 symbol fast path，选择能召回最多相关证据的入口。

2. **问题重写与查询扩展**  
   让 LLM 把自然语言问题扩展成多个候选查询（paraphrase + 关键词），分别做 embedding 检索，再合并结果。

3. **结合代码结构信息**  
   在 embedding 索引中加入函数签名、调用关系图、文件类型等元信息，帮助检索器把问题映射到 definition 文件而非调用点/测试文件。

### 5.2 改进 Stage 2：鲁棒的符号化调查

1. **让 LLM 基于 region 内容做所有判断**  
   避免硬编码文件优先级（如 `src/` 优先、`tests/` 降权），把所有文件/符号重要性判断交给 LLM，提升跨仓库泛化能力。

2. **多 symbol 并行扩展**  
   不依赖单一 symbol，而是从 region 中提取 top-k symbols 并行 grep，合并结果。

3. **加入 caller/callee/definition 工具**  
   在 grep 基础上加入 `find_definitions`、`find_callers`、`find_callees`，减少依赖启发式排序。

4. **迭代式 region refinement**  
   允许 Agent 在 Stage 1 和 Stage 2 之间多轮交互：先用 embedding 得到初始 region，通过 symbol grep 发现新文件，再让 embedding/LLM 判断是否需要扩大或调整 region。

### 5.3 验证与扩展

1. **完成确定性 multi-entry 成长曲线**（n=1,2,3,5），确认入口数量与覆盖率的稳定关系。
2. **在更大样本（前 50 题）上验证两阶段结论的稳定性。**
3. **引入人工评估**：除了 coverage，评估答案的准确性、完整性和可验证性。

---

## 附录：实验文件

| 实验 | 结果文件 | 脚本 |
|------|----------|------|
| Gold-files Oracle | — | `experiments/run_oracle.py` |
| Symbol Fast Path（gold entry） | `results/symbol_fastpath_0_15.json` | `experiments/run_symbol_fastpath.py` |
| Entry-only Dynamic Agent | `results/trajectory_merged_0_15.json` | `experiments/run_investigation_trajectory.py` |
| Question → Entry（embedding） | `results/question_to_entry_embedding_0_15_v2.json` | `experiments/run_question_to_entry.py` |
| Question → Entry（keyword） | `results/question_to_entry_keyword_0_15.json` | `experiments/run_question_to_entry.py` |
| Question → Symbol | `results/question_to_symbol_0_15.json` | `experiments/run_question_to_symbol.py` |
| End-to-End Pipeline | `results/end_to_end_0_15.json` | `experiments/run_end_to_end_pipeline.py` |
| Multi-Entry Symbol Expansion（n=3） | `results/multi_entry_0_15.json` | `experiments/run_multi_entry_symbol_expansion.py` |
| Multi-Entry Growth Curve（确定性） | `results/multi_entry_growth_curve_0_15.json` | `experiments/run_multi_entry_growth_curve.py` |
| LLM-Guided Region → Evidence | `results/llm_guided_region_to_evidence_0_15.json` | `experiments/run_llm_guided_region_to_evidence.py` |
| Symbol Quality 分析 | — | `scripts/analysis/symbol_quality_check.py` |
| Case Study 可视化 | `docs/research/0614/investigation_trajectory_case_study.md` | `scripts/analysis/visualize_trajectory.py` |
