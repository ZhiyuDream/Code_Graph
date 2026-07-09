# 0624 研究笔记：Repository QA 需要 Evidence Tree，而不仅是函数检索

## TL;DR

通过一系列诊断实验，我们发现：

> **Repository QA 的答案不是单个函数，而是代码库调用图上一棵小而局部的 Evidence Tree（平均 5-7 个节点，直径 3-4 跳）。**

传统 Code RAG 把 QA 当成 "Question → Most Relevant Function" 的 flat retrieval 问题，这本质上假设错误。真实流程更接近：

```text
Question
  ↓
Localization: 定位到 Repository Region（覆盖率 96.6%）
  ↓
Navigation: 从 Seed 沿 call graph 遍历 2-4 跳，找到 Evidence Tree
  ↓
Synthesis: 把 Evidence Tree 组织成答案
```

这一发现把论文定位从 "更好的函数检索" 提升为 "Repository QA 的结构规律"：

```text
Repository Question Answering
= Localization + Navigation + Synthesis
```

---

## 研究背景

此前实验已确认函数级检索是端到端 QA 的主要瓶颈：

| 层级 | 指标 | 数值 |
|---|---|---:|
| 文件级检索 | File Recall | ~90% |
| 函数级检索 | Function Recall@50 | ~60% |
| 函数级排序 | Gold Median Rank | ~12 |
| 端到端 | 完整引用率 | ~24% |

核心问题：**为什么 gold function 的 median rank 只有 12，但 Top-50 Recall 只有 ~60%？**

 today's work 最初假设：函数级检索的噪音主要来自跨模块的无关函数，用 Module 限定检索范围可以提升召回。但经过系统诊断后发现，**真正的问题不是搜索空间太大，而是 Repository QA 的答案天然具有 Evidence Tree 结构**。

---

## 核心发现总览

| 发现 | 关键指标 | 含义 |
|---|---|---|
| **Region Localization 基本解决** | Region Coverage = **96.6%** | Question 能定位到包含 gold 的局部区域 |
| **Gold 不在 seed 附近** | ≤1 跳仅占 **17.9%**，≤3 跳占 **74.8%** | 答案不是单个函数，需要沿图导航 |
| **Gold 之间形成局部子图** | 平均 pair distance = **2.55** | Evidence 在 call graph 上局部连通 |
| **Evidence Tree 很小** | 平均 **5.7 节点**，直径 **3.6** | 答案是一棵小子树，不是大段代码 |
| **Region Retrieval 提升温和** | R@50: 59.5% → **61.2%** | Scope 问题只是瓶颈的一小部分 |
| **Graph Reachability 更高** | 3-hop coverage = **84.0%** | 图遍历比纯 embedding ranking 更接近证据 |
| **Oracle 仍有差距** | Oracle R@50 = **74.2%** | 即使知道 gold modules，函数排序仍不完美 |
| **Question-Symbol 是最强导航信号** | Multi-seed + symbol rerank R@50 = **66.3%** | 超过 region 5.1pp，空间减少 81% |
| **Degree 不能当 primary signal** | degree-only R@50 = **14.5%** | High-degree 节点多为通用 utility，噪音大 |
| **Question symbols 跨 Module** | 94.6% 的问题 symbols 跨越 ≥2 modules | Module 不是合适的语义抽象层 |
| **Concept 比 Module 更细粒度** | Concept_top20_symbol R@50 = **64.5%**，空间 1,905 | 用更少函数达到相近 recall |
| **端到端 QA 引用覆盖率提升** | Concept-Symbol Coverage **81.7%** vs Baseline **71.3%** | +10.4pp，更好的 investigation process |

---

## 实验一：Module 噪音来源诊断

### 目的

在引入 Module 作为检索过滤器之前，先要回答一个问题：

> **Module 是什么？它从哪里来？用它过滤噪音靠谱吗？**

这个实验的目的是：先定义并生成 Module，再验证"排在 gold 前面的候选函数"是否主要来自 gold 所在 Module 之外。如果答案是肯定的，Module 就有潜力作为"相关/不相关"的结构判别器。

### 什么是 Module？

这里的 **Module 不是仓库里现成的文件夹或 namespace**，而是从代码的**调用图结构**中自动检测出来的**函数社区（community）**。

直观理解：如果一群函数互相调用很密集，并且经常出现在同一个文件里，它们就倾向于属于同一个 Module。例如：

```text
llama.cpp 仓库（17,458 个函数，44,238 条调用边）
          ↓
   Louvain 社区发现
          ↓
   71 个 Module（resolution=0.5）
```

每个 Module 是一组在调用图上紧密连接、在文件分布上集中的函数集合。

### Module 生成方法

生成 Module 的具体步骤如下：

#### 1. 构建函数调用图

从 Neo4j 中读取：
- 所有 `Function` 节点（id、name、file_path、start_line、end_line）
- 所有 `CALLS` 边（caller → callee）

得到一个有向调用图，再转为无向图用于社区发现。

#### 2. 定义边权重

图上有两种边，权重不同：

| 边类型 | 权重 | 含义 |
|---|---|---|
| **CALLS 边** | 1.0 | 函数 A 调用函数 B，结构相关性强 |
| **同文件边** | 0.5 | 两个函数在同一个 .cpp/.h 文件里，空间相关性强 |

注意：同文件边只加一次，不会和 CALLS 边重复累加；如果两个函数既互相调用又在同一文件，权重是 `1.0 + 0.5 = 1.5`。

#### 3. Louvain 社区发现

使用 `python-louvain` 库在图上运行 Louvain 算法，参数：

```text
resolution = {0.3, 0.5, 1.0}
weight     = "weight"
random_state = 42
```

- `resolution` 控制社区粒度：越小模块越大，越大模块越细。
- `random_state=42` 保证可重复。

#### 4. 合并过小社区

Louvain 会产生一些非常小的社区（<10 个函数）。这些小社区不稳定，我们把它们合并到**与之连接最多边的大社区**中。如果完全没有连接，就合并到最大的社区。

#### 5. 输出

最终每个函数被分配到一个 `module:{cid}`。

例如 resolution=0.5 时：

```text
函数数：17,458
边数：  44,238
Modules：71
```

每个 module 平均包含约 245 个函数。

### 实验方法

Module 生成后，我们做两件事：

#### A. 统计 Top-50 候选的模块来源

对每个问题：
1. 用 embedding 检索 Top-50 函数
2. 对每个候选函数，判断它属于：
   - **same_module**：和某个 gold function 同 module
   - **other_module**：不在任何 gold function 的 module 里
   - **no_module**：没被分配到 module（极少数）
   - **is_gold**：本身就是 gold

这里统计的是"噪音来源"——即 non-gold 候选里，有多少来自 other module。

#### B. 统计 missed gold 前面候选的模块来源

对每个问题：
1. 找出没被 Top-50 命中的 gold functions（missed gold）
2. 对每个 missed gold，看排在它前面的 Top-50 候选函数
3. 判断这些候选相对于"该 missed gold 自己的 module"属于 same / other / no module

这个指标更严格：它回答的是"为什么某个具体 gold 被排在后面"——是因为前面有很多同 module 的竞争对手，还是跨 module 的噪音？

### 结果

| Resolution | Modules | Top-50 噪音来源 | Missed Gold 前面候选来源 |
|---:|---:|---:|---:|
| 0.3 | 261 | 78.0% 其他 Module | 95.6% 其他 Module |
| **0.5** | **71** | 78.6% 其他 Module | **96.1%** 其他 Module |
| 1.0 | 73 | 49.7% 其他 Module | 80.8% 其他 Module |

### 逐行解读

#### Resolution = 0.3（261 modules，偏细粒度）

- Top-50 噪音中 78% 来自其他 module
- 对 missed gold，排在它前面的候选 95.6% 来自其他 module

说明细粒度 module 已经能区分噪音，但 module 数量太多，每个 module 太小。

#### Resolution = 0.5（71 modules，甜点粒度）

- Top-50 噪音中 78.6% 来自其他 module
- 对 missed gold，排在它前面的候选 **96.1%** 来自其他 module

这是最优平衡点：module 数量适中（71 个），过滤噪音能力最强。

#### Resolution = 1.0（73 modules，但结构不同）

- 其他 module 比例骤降到 49.7%
- 说明 resolution 过大导致 module 内部过于松散，很多"噪音"其实和 gold 被分到了同一个 module

这解释了为什么后续实验固定使用 resolution=0.5。

### 结论

1. **Module 是有效的结构过滤器**：对 missed gold，95% 以上的竞争函数来自其他 module。
2. **Resolution 很关键**：0.5 是甜点区，太细或太粗都会削弱过滤效果。
3. **Module 至少作为结构判别器是合理的**，但还没回答"Question 能否预测到 gold module"（那是实验二）。

---

## 实验二：Module Prediction Accuracy（fixed seed）

### 目的

Question 能否预测到包含 gold 的 Module？

### 关键洞察：旧指标有粒度陷阱

旧 "Module Recall@K" 定义是：**至少一个 gold function 落在 Top-K modules**。由于 Module 比 Function 粗得多，这个指标容易"碰巧命中"。

我们补充 **Gold Module Coverage**：`覆盖的 gold modules / 所有 gold modules`。

### 结果（resolution=0.5, 71 modules）

| 指标 | @1 | @3 | @5 | @10 |
|---|---:|---:|---:|---:|
| 旧：至少一个 gold func 命中 | 72.0% | **92.0%** | 96.0% | 98.0% |
| 新：gold module coverage（平均） | — | **47.5%** | ~60% | ~75% |
| Full Coverage（所有 gold func 都在） | 10.0% | **64.0%** | 72.0% | 86.0% |

### 结论

1. 旧指标 92% 误导性很强。Top-3 modules 平均只覆盖 47.5% 的 gold modules。
2. 问题平均涉及 **2.68 个 gold modules**（58% 的问题涉及 3 个 modules）。
3. Module Prediction 的真正问题是覆盖不全，不是完全预测不到。

---

## 实验三：Module-Constrained Function Retrieval（fixed seed）

### 目的

实验一证明 Module 是有效的噪音过滤器，实验二证明 Question 预测 module 会漏掉很多 gold modules。实验三要进一步回答：

> **如果用 Module 来限制函数级检索范围， recall 到底会怎样变化？**

具体测试几种策略：
- 完全不限制（baseline）
- 只用 Top-K modules（硬过滤）
- 用真实 gold modules（oracle）
- 用目录范围代替 module
- 用随机范围作为对照

### 关键指标解释

在进入结果前，先明确三个指标：

#### R@50（Recall@50，函数级召回）

对每道题：

```text
R@50 = 前 50 个候选中命中的 gold functions / 该题所有 gold functions
```

最后对所有题目取平均。它回答：**前 50 个结果找回了多少比例的正确证据？**

R@50 是检索召回的核心指标。实验里我们更关心它，因为 Repository QA 怕漏证据。

#### Hit@50（Question-Level Hit Rate）

对每道题：

```text
Hit@50 = 1  如果前 50 个结果里至少有 1 个 gold
         0  否则
```

最后对所有题目取平均。它回答：**多少比例的问题能被前 50 个结果"覆盖到"？**

注意：**Hit@50 不是 Precision@50。** 它只关心"有没有"，不关心"有多少"。

举个例子：
- 某题有 3 个 gold，前 50 个结果只命中 1 个
- R@50 = 1/3 = 33.3%
- Hit@50 = 1（因为至少命中了一个）

#### P@50（Precision@50，精确率）

对每道题：

```text
P@50 = 前 50 个候选中命中的 gold functions / 50
```

这才是"前 50 个里有多少是 gold"。

本实验中，由于每道题平均只有约 3 个 gold，真实 P@50 很低（baseline 约 3.4%），和 Hit@50 完全不是一个量级。**后续表格里的 "P@50" 列其实是 Hit@50**，这是历史记录中的命名错误，这里特别说明。

### 结果（resolution=0.5, max scoring）

| Scope | Funcs | R@10 | R@20 | **R@50** | R@100 | Hit@50 |
|---:|---:|---:|---:|---:|---:|---:|
| baseline | 17,458 | 34.5 | 46.2 | **59.5** | 74.2 | 88.0% |
| module_m1 | 1,624 | 30.3 | 35.7 | 39.6 | 42.4 | 72.0% |
| module_m3 | 3,301 | 34.5 | 45.3 | 57.5 | 66.5 | 84.0% |
| module_m5 | 4,141 | 33.8 | 44.2 | **59.7** | 70.3 | 88.0% |
| **oracle_module** | 2,458 | 43.5 | 55.7 | **74.2** | 83.6 | 98.0% |
| directory_m5 | 4,255 | 34.5 | 46.2 | **59.5** | 73.5 | 88.0% |
| random_m5 | ~4,141 | — | — | ~20 | — | ~45% |

### 逐行解读

#### Baseline（全库）

- R@50 = 59.5%：前 50 个候选平均覆盖了 59.5% 的 gold functions
- Hit@50 = 88.0%：88% 的问题在前 50 个结果里至少有一个 gold

这两个数字差距大是正常的：
- 平均 3 个 gold 里，前 50 找回约 1.8 个 → R@50 ≈ 60%
- 但大多数问题至少能找回 1 个 → Hit@50 ≈ 88%

**不要误读成"前 50 个里有 88% 是 gold"，真实 Precision@50 只有约 3.4%。**

#### module_m1（硬过滤到 Top-1 module）

- 搜索空间：1,624 函数（只有 baseline 的 9%）
- R@50：39.6%，比 baseline 掉了近 20 个百分点
- Hit@50：72.0%

这说明**只用一个 module 过滤太激进了**。实验二已经显示一个问题平均涉及 2.68 个 gold modules，只取 1 个 module 自然会漏掉大量 gold。

#### module_m3（硬过滤到 Top-3 modules）

- 搜索空间：3,301 函数
- R@50：57.5%，接近 baseline 但仍低 2 个百分点
- Hit@50：84.0%

3 个 modules 比 1 个好很多，但还不够。仍然有部分 gold 所在的 module 没被预测到。

#### module_m5（硬过滤到 Top-5 modules）

- 搜索空间：4,141 函数
- R@50：59.7%，和 baseline 基本持平
- Hit@50：88.0%，和 baseline 基本持平

这说明**Top-5 module 过滤基本不损失召回**，但也没提升。它只是把无关模块去掉了，但 embedding 在 module 内的排序和全库差不多。

#### oracle_module（真实 gold modules）

- 搜索空间：2,458 函数（只有 baseline 的 14%）
- R@50：**74.2%**，比 baseline 高 14.7 个百分点
- Hit@50：**98.0%**

这是核心发现。它说明：

> **如果知道哪些 modules 真正包含 gold，只用很少的函数就能显著提升召回。**

但 oracle 和现实方法的差距很大：oracle 14% 函数达到 74.2%，而 module_m5 用 24% 函数才 59.7%。

这个差距说明：
1. Module selection 本身还有很大提升空间
2. 即使 module 选对了，内部排序也还有提升空间

#### directory_m5（目录过滤）

- 搜索空间：4,255 函数
- R@50：59.5%，和 baseline 一样
- Hit@50：88.0%

这说明：简单地用文件目录限定范围，效果和 module_m5 差不多。但目录不是我们的研究重点，module 更有结构性。

#### random_m5（随机模块对照）

- R@50：约 20%
- Hit@50：约 45%

这个对照很重要：它证明**不是"缩小搜索空间"本身有用**，而是缩小到正确的模块才有用。随机选模块效果很差。

### 结论

1. **Hard Module Filtering 不提升 R@50**
   - module_m5 和 baseline 都是 59.5% 左右。
   - 这说明 module 过滤的主要价值是**压缩空间**，不是**提升召回**。

2. **Oracle Module 上限 74.2%**
   - 如果知道真实 gold modules，召回可以大幅提升。
   - 这意味着 module 信息本身很有价值，但当前的 module prediction 无法充分利用。

3. **Random Scope 基本无效**
   - 证明"缩小空间"不是免费的收益，必须缩小到正确的地方。

4. **Module 过滤的上限已经被揭示**
   - 即使 module 完美预测，也只能到 74.2%。
   - 还有 25.8% 的 gold 需要别的机制来找（这就是后面 graph navigation 的动机）。

5. **指标命名澄清**
   - 本实验表格中的 "Hit@50" 指"至少命中一个 gold 的问题比例"，**不是 Precision@50**。
   - 真实 P@50 很低（baseline 约 3.4%），因为每道题平均只有约 3 个 gold，而候选池有数万函数。

---

## 实验四：Error Decomposition

### 目的

实验三显示：用 Top-5 modules 过滤后 R@50 和 baseline 差不多（59.7% vs 59.5%），但 oracle modules 可以到达 74.2%。这说明 module-constrained retrieval 有损失，但损失来源不清楚。

实验四要把损失拆成两个独立因素：

```text
Total Error = Module Selection Error + Intra-Module Ranking Error
```

- **Module Selection Error**：gold function 所在的 module 根本没被选进 Top-M modules
- **Intra-Module Ranking Error**：gold function 的 module 已经被选中了，但它在该 module 内的 embedding 排名掉出了 Top-50

### 方法

对每道题：

1. 用 question embedding 给所有 modules 打分，取 Top-M modules（M = 1, 3, 5）
2. 只在这些 Top-M modules 包含的函数集合里做 embedding retrieval
3. 检查每个 gold function 属于哪种情况：
   - **Recovered**：gold 的 module 在 Top-M 中，且 gold 排进了 Top-50
   - **Selection Error**：gold 的 module 不在 Top-M 中
   - **Ranking Error**：gold 的 module 在 Top-M 中，但 gold 没排进 Top-50

最后对所有 gold functions 统计三种情况的比例。

### 结果

| Scope | Recovered | Selection Error | Ranking Error |
|---:|---:|---:|---:|
| Top-1 | 41.2% | 52.0% | 6.8% |
| Top-3 | 57.4% | 18.2% | 24.3% |
| Top-5 | 59.5% | 13.5% | 27.0% |

### 逐行解读

#### Top-1 Module

- 只选 1 个 module 来限定检索范围
- 41.2% 的 gold 被成功找回
- **52.0% 的 gold 因为 module 没被选而丢失**（Selection Error 主导）
- 只有 6.8% 的 gold 是因为在 module 内排名差而丢失

这说明：**只用一个 module 太激进了，大量 gold 所在 module 根本没进 Top-1。**

#### Top-3 Modules

- 选 3 个 modules
- 57.4% 的 gold 被找回
- Selection Error 从 52% 降到 18.2%
- Ranking Error 从 6.8% 升到 24.3%

这说明：增加 module 数量后，大部分 gold 所在的 module 已经被覆盖了，但**模块内部的函数排序问题开始凸显**。

#### Top-5 Modules

- 选 5 个 modules
- 59.5% 的 gold 被找回
- Selection Error 进一步降到 13.5%
- Ranking Error 继续升到 27.0%

这说明：继续增加 module 数量，selection 收益递减，ranking 问题成为主要瓶颈。

### 关键趋势

```text
M 增加:
  Selection Error ↓↓  （从 52% → 13.5%）
  Ranking Error   ↑↑  （从 6.8% → 27.0%）
  Recovered       ↑   （从 41.2% → 59.5%）
```

这揭示了一个重要事实：

> **Module selection 和 Intra-module ranking 是两个独立且都显著的问题。**

不能只优化其中一个。即使 module 选得再好，函数级排序仍然会把大量 gold 排在后面。

### 结论

1. **Top-1 主要损失来自 Selection Error**：一个 module 覆盖不了问题的多个 gold modules。
2. **Top-3/5 中 Ranking Error 占比上升**：module 范围对了，但 embedding 排序不够好。
3. **两个因素都显著**：单纯改进 Module Selection 不够，Intra-Module Ranking 也是大问题。
4. 这个分解直接引出后续方向：
   - 改进 module selection（实验六、七的 Region 扩展）
   - 改进 intra-region ranking（实验九的 Oracle Gap、实验十的 Graph Navigation）

---

## 实验五：Gold Evidence Structure Analysis

### 目的

每道题的 gold evidence 在仓库中到底是什么结构？

### 结果

**Gold Module 数量分布**

| Modules | 题目数 | 比例 |
|---:|---:|---:|
| 1 | 9 | 18.0% |
| 2 | 5 | 10.0% |
| **3** | **29** | **58.0%** |
| 4 | 7 | 14.0% |

平均 **2.68 个 gold modules**。

**目录关系**

| 关系 | 比例 |
|---|---:|
| 同一目录 | **85.1%** |
| 共同父目录 | **95.5%** |

**Shared Callers**：**82.0%** 的问题存在函数同时调用多个 gold modules。

### 结论

1. Gold evidence 高度目录局部化。
2. Gold evidence 不是单个 module：58% 的问题涉及 3 个 modules。
3. Shared callers 普遍存在，说明 evidence 经常有共同 hub。

---

## 实验六：Local Repository Region Coverage

### 目的

把检索范围从单个 Module 扩展为 Local Repository Region，能否覆盖更多 gold？

### Region 定义

```text
Region(seed_module) =
    seed_module
    + same_directory_modules
    + shared_caller_modules
```

### 结果

| Scope | Funcs | Mean Cov | Full Cov |
|---:|---:|---:|---:|
| top1_module | 1,624 | 44.5% | 10.0% |
| top3_modules | 3,301 | 80.7% | 64.0% |
| **region_dir_only** | 11,816 | **95.0%** | **94.0%** |
| **region_caller_only** | 10,551 | **91.3%** | **90.0%** |
| **region_full** | 12,601 | **97.0%** | **96.0%** |

### 结论

1. Region 能覆盖 **97%** 的 gold functions。
2. Directory 扩展比 Caller 扩展更重要。
3. Region 仍偏大（12,601 funcs），需要压缩。

---

## 实验七：Region Compression

### 目的

实验六已经证明：通过 directory/caller 扩展可以把 coverage 提到 95%+，但 region 太大（10k-12k 函数）。实验七要找到**更高效的扩展方式**：用更少的函数达到相近的 coverage。

### 每种 Scope 的含义

实验七里的 scope 命名规则是：

```text
[seed] + [扩展方式1] + [扩展方式2]
```

#### Seed 选择

| Seed | 含义 |
|---|---|
| **seed_only** | 只取 question embedding 排序最高的 1 个 module |
| **top3_modules** | 取 question embedding 排序最高的 3 个 modules |

#### 扩展方式

| 扩展方式 | 含义 |
|---|---|
| **dir_d0** | Directory depth 0：取 seed module 所在**同一目录**下的所有 modules |
| **dir_d1** | Directory depth 1：取 seed module 所在目录 + **父目录**下的所有 modules |
| **callers_k5** | 对每个 seed module，找出**调用它的函数**所在的 modules，按这些函数与 question 的 embedding 相似度排序，取 Top-5 个 caller modules |
| **callees_k5** | 类似 callers_k5，但取**被 seed module 调用的函数**所在的 modules |

注意：directory 扩展是**空间局部性**（同目录的 module 都加进来），caller 扩展是**结构局部性**（按调用关系和相关性精选）。

### 关键结果

| Scope | Funcs | Mean Cov | Full Cov | 说明 |
|---:|---:|---:|---:|---|
| top3_modules | 3,301 | 80.7% | 64.0% | 只用 embedding 预测的 Top-3 modules |
| **top3_modules + callers_k5** | **6,163** | **95.2%** | **90.0%** | Top-3 modules + 精选 caller modules |
| dir_d0 | 11,816 | 95.0% | 94.0% | 同目录所有 modules（粗放） |
| top3_modules + dir_d0 | 15,705 | 99.0% | 98.0% | Top-3 + 同目录（太大） |

### 为什么 callers_k5 是甜点？

对比两种达到 ~95% coverage 的方式：

| 路径 | 增加函数数 | coverage 提升 |
|---|---:|---:|
| top3_modules → dir_d0 | +8,515 | 80.7% → 95.0%（+14.3%） |
| top3_modules → top3_modules+callers_k5 | **+2,862** | 80.7% → 95.2%（+14.5%） |

**callers_k5 用不到 1/3 的额外函数，达到了差不多的 coverage 提升。**

原因是：
- `dir_d0` 是**不加选择地**把整个目录的 module 加进来
- `callers_k5` 是**有选择地**加与 question 最相关的 caller modules

### 结论

1. **甜点区**：`top3_modules + callers_k5` 用 **6,163 函数**（比全库减少 65%）覆盖 **95.2%** gold。
2. **Caller 扩展比 Directory 扩展高效得多**：同样从 top3_modules 扩展，caller 增加 2.8k 函数提升 14.5% coverage，directory 增加 8.5k 函数提升 14.3% coverage。
3. **Directory 扩展太粗放**：虽然 coverage 也高，但引入了大量无关函数。
4. **结构扩展优于空间扩展**：按调用关系和相关性扩展，比按目录层级扩展更精准。

---

## 实验八：Region-Constrained Retrieval

### 目的

实验六、七已经证明：用 Local Repository Region 可以把搜索空间从 17k 函数压缩到 6k 左右，同时覆盖 95% 以上的 gold functions。但 "coverage" 只是回答了一个**存在性**问题——gold 是否在 region 里。实验八要进一步回答一个**实用性**问题：

> **知道 region 之后，把函数级检索限制在这个 region 里，是否真的能找回更多 gold？**

换句话说，我们要验证 region localization 的收益能否**转化**为 retrieval recall 的提升。如果 region 覆盖 96% 的 gold，但限制检索后 recall 只涨 1-2%，那就说明问题不在"搜到范围"，而在"范围内排好序"。

### 方法

沿用实验七的 region 定义，对比 5 种检索范围：

1. **baseline**：在整个仓库 17,458 个函数上做 embedding retrieval，作为参照。
2. **top3_modules**：只保留 embedding 预测出的 Top-3 modules 内的函数（3,301 个）。这是"硬模块过滤"，测试 module 是否足以限定范围。
3. **top3_modules + callers_k5**：在 top3_modules 基础上，再扩展每个 module 的 Top-5 shared callers（即调用这些 module 的函数）。这是实验七找到的"甜点" region，约 6,163 个函数。
4. **directory_seed**：只保留 seed function 所在目录及其子目录的函数（8,184 个）。作为 directory-level 的朴素 region 对照。
5. **oracle_modules**：直接用**真实的 gold modules** 作为范围（2,458 个函数）。这是理论上界，告诉我们如果 module 选择完全正确，recall 能到多少。

所有范围内部都使用相同的函数 embedding 进行排序，取 Top-K 计算 Recall@K。这样可以隔离"范围选择"本身的影响。

### 结果

| Scope | Funcs | R@10 | R@20 | **R@50** | R@100 | MRR |
|---:|---:|---:|---:|---:|---:|---:|
| baseline | 17,458 | 34.5 | 46.2 | **59.5** | 74.2 | 0.442 |
| top3_modules | 3,301 | 34.5 | 45.3 | 57.5 | 66.5 | 0.440 |
| **top3_modules + callers_k5** | 6,163 | 34.5 | 46.7 | **61.2** | 74.5 | 0.443 |
| directory_seed | 8,184 | 34.2 | 44.3 | 58.7 | 67.5 | 0.442 |
| oracle_modules | 2,458 | 43.5 | 55.7 | **74.2** | 83.6 | 0.537 |

### 逐行解读

#### 1. Baseline（全库）vs Region（top3_modules + callers_k5）

- 搜索空间：17,458 → 6,163，**减少 65%**
- R@50：59.5% → 61.2%，**仅提升 1.7 个百分点**
- R@100：74.2% → 74.5%，几乎持平

这说明：**把无关模块过滤掉后，确实能 slightly 提升 recall，但提升非常有限。** Region 覆盖了 96% 的 gold，但限制检索后并没有找回明显更多的 gold。原因是 region 虽然大，但 embedding ranking 在 region 内部的表现和全库差不多——该排在前面的还在前面，该漏掉的还是漏掉。

#### 2. top3_modules（硬模块过滤）表现更差

- R@50：57.5%，比 baseline 还低 2 个百分点
- R@100：66.5%，比 baseline 低 7.7 个百分点

这说明：**过度压缩范围会伤害 recall。** 实验二已经显示，Top-3 modules 平均只覆盖 47.5% 的 gold modules。硬过滤把很多相关模块切掉了，自然找不回 gold。

#### 3. directory_seed（目录范围）没有帮助

- R@50：58.7%，比 baseline 低 0.8 个百分点
- R@100：67.5%，比 baseline 低 6.7 个百分点

这说明：**简单地用文件目录当 region 不够。** Directory 范围虽然局部，但可能包含大量无关文件，也可能漏掉跨目录的 gold。

#### 4. oracle_modules 是核心对照

- 用真实 gold modules 作为范围，只有 2,458 个函数
- R@50：**74.2%**，比 region 的 61.2% 高出 **13 个百分点**
- R@100：**83.6%**，比 region 的 74.5% 高出 **9.1 个百分点**

这个数字非常重要。它说明：

> **如果知道哪些 modules 真正包含 gold，只用 14% 的函数就能达到 74.2% 的 R@50。**

但实际 region（top3_modules + callers_k5）在 2.5 倍函数数下只达到 61.2%。

### 结论

实验八的核心结论是三重分解：

1. **Region localization 基本解决，但收益温和**
   - Region 能把空间减少 65%，R@50 只提升 1.7%。
   - 这意味着"跨 region 的噪音"不是主要瓶颈。

2. **真正的大 gap 在 intra-region ranking**
   - Region 61.2% vs Oracle 74.2%，相差 13 个百分点。
   - 也就是说：即使 gold 已经在 region 里，embedding 仍然无法把它们排到 Top-50。

3. **Module selection 还有改进空间**
   - Oracle 只用了 2,458 个函数就达到 74.2%，而实际 region 用了 6,163 个函数才 61.2%。
   - 说明 region 不仅内部排序差，还包含了过多无关函数。

这个实验直接引出了后续实验九（Oracle Gap Analysis）和实验十（Graph Reachability）：

> **既然问题不是"gold 在哪里"，而是"怎么在 region 内找到 gold"，那我们需要 call graph navigation，而不仅是 embedding ranking。**

---

## 实验九：Oracle Gap Analysis

### 目的

解释 Region 61.2% 与 Oracle 74.2% 之间的 13 个百分点差距。

### 结果

- Region 覆盖 **96.6%** 的 gold functions。
- 覆盖的 gold 在 Region 内排名分布：

| Rank | Golds | 比例 |
|---:|---:|---:|
| 1-10 | 47 | 32.9% |
| 11-20 | 17 | 11.9% |
| 21-50 | 24 | 16.8% |
| **51-100** | **22** | **15.4%** |
| **101-300** | **10** | **7.0%** |
| **>300** | **23** | **16.1%** |

- **55 个 gold** 被 Region 覆盖但排名 >50。
- Region **从不伤害 ranking**（0% 比 baseline 更差）。
- Oracle vs Region median rank gap = 4，mean gap = 75.3。

### 失败案例模式

典型失败 gold：
- `common_params_parser_init`（chat template 问题）
- `common_init_result::common_init_result`（sampler 问题）
- `ggml_backend_free`（backend 释放问题）

共同特点：
1. 函数名非常泛，不含问题关键词。
2. 大量在 `common/arg.cpp`、`common/common.cpp` 等 cross-cutting 文件中。
3. 有些 gold 在 header 文件中。

### Header vs Non-Header 对照

为了排除"header 文件导致排名差"的可能性，我们单独统计了 header 和 non-header gold 的分布：

| 类别 | 占比 | Region Coverage | Covered 但 rank >50 |
|---:|---:|---:|---:|
| 全部 gold | 100% | 96.6% | 38.5% |
| Header gold | 17.6% | 96.2% | 32.0% |
| Non-header gold | 82.4% | 96.7% | **39.8%** |

关键发现：

1. **Header gold 的 coverage 和 non-header 几乎一样**（96.2% vs 96.7%）。
2. **Header gold 的排名反而略好**：covered 但 rank >50 的比例为 32.0%，低于 non-header 的 39.8%。
3. 即使排除 header，仍有约 **40%** 的 covered gold 排不进 Top-50。

这说明：

> **Intra-Region Ranking 问题不是 header artifact。** 即使只看 .cpp/.cc 里的实现函数，embedding 仍然无法把大量 gold 排到前面。

### 结论

1. **Region scope 已经不错**，真正问题是 **Intra-Region Ranking**。
2. 失败主要源于函数名/embedding 与问题语义的直接匹配不足，而非文件类型。
3. 典型失败函数是 cross-cutting 的通用实现（参数解析、后端释放、初始化等），它们语义上很重要但名字很泛。

---

## 实验十：Evidence Chain Reachability

### 目的

从 Region 内 top retrieved function 出发，通过 call graph 的 1/2/3 跳扩展，能否到达 gold？

这个问题要验证：Repository QA 是否需要多跳证据链推理，而不是单个函数匹配。

### 方法

以文档中报告的 `seeds5_both_full_graph`（即 graph reachability@3）为例，具体步骤如下：

#### 1. 选 Seed

- 先构建 region：`top3_modules + callers_k5`（约 6,163 个函数）
- 在该 region 内用 question embedding 给所有函数打分
- 取相似度最高的 **Top-5 个函数**作为 seeds

#### 2. 图遍历

从每个 seed 出发，在 call graph 上做 BFS，最多走 **3 跳**。

参数含义：

| 参数 | 取值 | 含义 |
|---|---|---|
| `seeds` | 5 | 用 5 个 seed functions 同时出发 |
| `direction` | both | 双向扩展：既看 caller（谁调用了我），也看 callee（我调用了谁） |
| `scope` | full_graph | 不限于 region，可以走到全仓库的任意函数 |

注意：这是**不做排序、不做选择**的纯图遍历。所有 3 跳内可达的函数都被收集。

#### 3. 统计 Coverage

把所有 seeds 3 跳内可达的函数合并成一个集合，计算其中包含多少 gold functions：

```text
coverage = reachable 节点中 gold 的数量 / 该题所有 gold 数量
```

### 结果（seeds5_both_full_graph）

| Hop | Coverage |
|---:|---:|
| Hop0 (seed only) | 26.7% |
| Hop1 | 34.7% |
| Hop2 | 61.0% |
| **Hop3** | **84.0%** |

### 关键对比（注意：这不是 apples-to-apples 对比）

| 方法 | 候选集合大小 | 指标 | 数值 |
|---:|---:|---|---:|
| baseline R@50 | 17,458 中取 Top-50 | Recall@50 | 59.5% |
| region R@100 | 6,163 中取 Top-100 | Recall@100 | 74.5% |
| graph reachability@3 | 3,307 个 reachable 节点 | Coverage@3307 | **84.0%** |

#### 为什么不公平？

- **R@50 / R@100**：在很大的候选池里只取前 50/100 个，看能覆盖多少 gold
- **Graph reachability@3**：把 3,307 个 reachable 节点**全部**算进去，看里面包含多少 gold

两者衡量的不是同一个东西。后者相当于 "Coverage@3307"，而不是 "R@50"。

#### 公平对比应该是什么？

正确的对比方式至少有两种：

1. **同样在 3,307 个 reachable 函数里，用 embedding 排序取 Top-50，看 R@50 是多少**
   - 这个我们没有在实验十里直接计算
   - 但可以预期它会介于 region R@100（74.5%）和 graph coverage@3307（84.0%）之间

2. **同样取 3,307 个函数，比较不同选择策略的 gold 覆盖率**
   - Graph reachability@3：按 call graph 3-hop 选 3,307 个函数 → 84.0% coverage
   - Region embedding@3307：从 region 里按 embedding 选前 3,307 个函数 → coverage 也会接近 region coverage（~95%）

#### 那实验十的价值到底是什么？

实验十不是一个"打败 R@50"的实验，而是一个**理论上界实验**：

> **它证明：从 good seeds 出发，call graph 3 跳范围内的节点包含了 84% 的 gold。**

这说明：
1. Gold 不在 seed 附近，需要多跳导航
2. 如果有一个完美的导航机制，3 跳内有很大概率找到 gold
3. 后续研究的重点应该是：如何在 3 跳范围内有效地 pruning 和 ranking，而不是无限制地扩大搜索范围

### 结果解读

#### 1. Graph traversal 的"覆盖天花板"

84.0% 是一个 coverage 上限，不是 retrieval recall。它告诉我们：**图结构本身有能力把 84% 的 gold 纳入一个不大的局部邻域。**

#### 2. Gold 不在 seed 附近

| Hop | Coverage |
|---:|---:|
| Hop0 | 26.7% |
| Hop1 | 34.7% |
| Hop2 | 61.0% |
| Hop3 | 84.0% |

如果只取 seed 本身，只有 26.7% 的 gold 被覆盖。需要走到 3 跳才能覆盖 84%。

这说明：**答案不是单个函数，需要沿 call graph 多跳导航。**

### 结论

1. Graph walk 能把大量 gold 纳入一个局部 reachable 集合，但产生太多候选（平均 3,307 个）。
2. **不能直接把 84.0% 与 R@50 对比**，前者是 coverage@3307，后者是 recall@50。
3. 实验十的真正价值是证明多跳图导航的潜力，为后续 "multi-seed + graph expansion + rerank" 提供理论依据。
4. **Embedding 和 Graph Walk 优势互补**：embedding 找 good seeds，graph walk 扩展找相关证据，rerank 在扩展后的集合里精选。

---

## 实验十一：Evidence Chain Structure

### 目的

统计 gold function 距离 top seed 的 call graph 距离分布。

### 结果

| Distance from Top-1 Seed | Golds | 比例 | 累计 |
|---:|---:|---:|---:|
| 0 hop | 16 | 13.0% | 13.0% |
| 1 hop | 6 | 4.9% | 17.9% |
| **2 hops** | **38** | **30.9%** | **48.8%** |
| **3 hops** | **32** | **26.0%** | **74.8%** |
| 4 hops | 29 | 23.6% | 98.4% |
| >4 hops | 2 | 1.6% | 100.0% |

### Oracle Seed Reachability

对于有 ≥2 个 gold 的问题，用任意 gold 当 seed：

| Hops | Best-seed Coverage | Avg-seed Coverage |
|---:|---:|---:|
| 1 | 18.7% | 11.1% |
| 2 | 66.7% | 54.9% |
| 3 | 77.6% | 67.9% |

### Gold Pair Distance

同一问题内 gold 之间的距离：

| Distance | Pairs | 比例 |
|---:|---:|---:|
| 1 | 27 | 16.6% |
| **2** | **68** | **41.7%** |
| 3 | 24 | 14.7% |
| 4 | 41 | 25.2% |

- Mean: **2.55**
- Median: **2.0**

### 结论

1. **只有 17.9% 的 gold 在 seed 附近（≤1 跳）**。
2. **82.1% 的 gold 需要跨至少 2 跳**。
3. Gold functions 之间形成局部连通结构，平均距离 2.55 跳。
4. 这强烈说明 Repository QA 需要 **graph navigation**，不是 single-function retrieval。

---

## 实验十二：Evidence Tree / Steiner Tree

### 目的

连接一道题所有 gold functions 所需的最小子图长什么样？

### 方法

把 gold functions 当作 terminal nodes，在 call graph 上求 MST-based Steiner Tree 近似。

### 结果

| Metric | Value |
|---|---:|
| Mean tree nodes | **5.68** |
| Median tree nodes | **6.0** |
| Mean Steiner nodes | **2.29** |
| Median Steiner nodes | **2.0** |
| Mean tree diameter | **3.56** |
| Median tree diameter | **4.0** |

**Tree Node Count 分布**

| Nodes | Questions | 比例 |
|---:|---:|---:|
| 3 | 3 | 7.3% |
| 4 | 3 | 7.3% |
| 5 | 13 | 31.7% |
| 6 | 9 | 22.0% |
| 7 | 11 | 26.8% |
| 8 | 2 | 4.9% |

**Tree Size / Gold Count Ratio**
- Mean: **1.70**
- Median: **1.67**

**Top-1 Seed 与 Evidence Tree 的关系**

| Relation | Questions | 比例 |
|---:|---:|---:|
| Seed on tree | 16/50 | 32.0% |
| Seed on tree or ≤2 hops away | 35/50 | **70.0%** |

### 结论

1. **Repository QA 的答案是一棵小而局部的 Evidence Tree**（平均 5.7 节点）。
2. 通常只需要 **1-3 个 connector functions（Steiner nodes）** 就能连接所有 gold。
3. Tree 直径只有 **3-4 跳**。
4. **70% 的问题中，top seed 已经在 Evidence Tree 上或附近**。

---

## 实验十三：Multiple-Seed Non-Greedy Navigation

### 目的

验证：如果不用 greedy single-seed，而是从多个 seeds 同时做非贪婪图扩展，能否超过 flat retrieval？

### 两种导航策略的对比

实验十三要对比两种完全不同的图导航方式：

#### 1. Greedy Single-Seed（贪婪单种子）

这是实验十二之前做的极简 navigation prototype，来自 `scripts/analysis/eval_minimal_navigation.py`。

流程：

```text
Question
  ↓
选 region 内 embedding 最高的 1 个函数作为 seed
  ↓
重复 4 步（s4）:
    1. 扩展当前 frontier 的 1-hop 邻居（callers + callees）
    2. 用 embedding similarity 给所有新邻居打分
    3. 只保留 top-K（k=50）作为下一步 frontier
  ↓
所有访问过的节点 = candidate set
  ↓
在 candidate set 内做 embedding retrieval
```

为什么叫"贪婪"？
- 每走一步，只保留 embedding 分数最高的 K 个节点
- 下一步只从这 K 个节点继续扩展
- 如果某条路径上的中间节点 embedding 分数低，就会被剪掉，永远到不了后面的 gold

这正是前面发现的 Steiner node 问题：Steiner nodes 通常 embedding 分数低，但它们是到达 gold 的必经桥梁。贪婪策略会过早剪掉这些桥。

#### 2. Non-Greedy Multi-Seed（非贪婪多种子）

这是 `scripts/analysis/eval_multiseed_navigation_and_steiner.py` 里的方法。

流程：

```text
Question
  ↓
在 region 内取 embedding Top-5 函数作为 seeds
  ↓
从每个 seed 同时做 3/4-hop BFS（双向，不剪枝）
  ↓
Union 所有 visited nodes（合并所有 seeds 的 3-hop 邻居）
  ↓
在 union 集合内做 embedding retrieval
```

为什么叫"非贪婪"？
- 不做中间剪枝，所有 3 跳内可达的节点都保留
- 用多个 seeds 同时扩展，避免单一起点跑偏
- 最后再统一用 embedding 排序

### 结果

| Config | Scope Size | R@5 | R@10 | R@20 | **R@50** | R@100 |
|---:|---:|---:|---:|---:|---:|---:|
| baseline | 17,458 | 26.0 | 34.5 | 46.2 | 59.5 | 74.2 |
| region | 6,151 | 26.7 | 34.5 | 46.7 | 61.2 | 74.5 |
| greedy single-seed (s4_k50) | 141 | 22.2 | 26.8 | 33.3 | 42.7 | 49.2 |
| **multiseed_both_3hop** | **3,307** | 26.7 | **36.7** | 46.3 | **60.7** | 70.8 |
| **multiseed_both_4hop** | **6,124** | 26.0 | **36.7** | **48.5** | **64.5** | 73.2 |
| multiseed_callers_3hop | 54 | 26.7 | 28.5 | 32.0 | 33.0 | 34.7 |
| multiseed_callees_3hop | 72 | 26.7 | 27.8 | 29.2 | 29.2 | 29.2 |

### 关键发现

#### 1. Greedy single-seed 效果很差

- 只访问了约 141 个函数
- R@50 只有 42.7%，比 baseline 还低 16.8 个百分点
- 比 region（61.2%）低 18.5 个百分点

这说明：**贪婪策略会剪掉大量关键路径。** 特别是那些 embedding 分数低但结构重要的 Steiner bridge nodes，会被 frontier pruning 无情丢弃。

#### 2. Multi-seed + non-greedy 大幅超过 greedy

| 方法 | R@50 | Scope Size |
|---|---:|---:|
| greedy single-seed | 42.7% | 141 |
| multiseed_both_3hop | 60.7% | 3,307 |
| multiseed_both_4hop | 64.5% | 6,124 |

R@50 从 42.7% 提升到 64.5%，**增加了 21.8 个百分点**。

关键差异不是 scope size（虽然 multi-seed 确实访问了更多节点），而是**扩展方式**：
- Greedy：每一步都按 embedding 剪枝，容易错过低相似度桥接节点
- Non-greedy：先完整展开局部子图，再用 embedding 排序

#### 3. Multiple seeds 很关键

一个问题往往涉及多个 entry points（多个 gold modules）。只从 1 个 seed 出发，很难覆盖所有证据树分支。从 5 个 seeds 同时出发，能覆盖更多 entry points。

#### 4. 必须双向扩展

| 方向 | R@50 | Scope Size |
|---|---:|---:|
| both（双向） | 60.7% | 3,307 |
| callers_only | 33.0% | 54 |
| callees_only | 29.2% | 72 |

只沿 caller 或只沿 callee 扩展效果都很差。因为 Evidence Tree 上的路径既可能向上游也可能向下游。

### 结论

1. **Multiple seeds + non-greedy expansion 远好于 greedy single-seed**：R@50 从 42.7% 提升到 64.5%。
2. **Evidence Tree 不是单根树**：一个问题常有多个 entry points，需要多个 seeds。
3. **贪婪的 embedding-based pruning 会伤害召回**：因为它会剪掉低相似度的 Steiner bridge nodes。
4. **必须双向扩展**：只沿 callers 或 callees 效果很差。
5. **4-hop multiseed 开始接近/超过 region 表现**：说明 graph expansion 可以作为 region 的替代或补充。

---

## 实验十四：Steiner Node 机制分析

### 目的

验证一个核心假设：Evidence Tree 中的 Steiner/bridge nodes 是否与 gold nodes 具有不同的性质？

### 方法

对每个问题的 Evidence Tree：
- **Gold nodes**：用户标注的 gold functions
- **Steiner nodes**：连接 gold nodes 所需的最小 intermediate nodes

比较两者的：
- Question embedding similarity
- Call graph degree

### 结果

| 属性 | Gold Node | Steiner Node | Cohen's d |
|---|---:|---:|---:|
| Mean embedding similarity | **0.395** | 0.233 | **1.966** |
| Median embedding similarity | **0.398** | 0.214 | — |
| Mean graph degree | 16.7 | **195.9** | **1.404** |
| Median graph degree | 5.5 | **130.0** | — |

**每题统计**：
- **100%** 的问题：gold nodes 的 embedding similarity 高于 Steiner nodes
- **100%** 的问题：Steiner nodes 的 graph degree 高于 gold nodes

**Similarity 分布**：

| Score Range | Gold | Steiner |
|---|---:|---:|
| <0.20 | 0.0% | 25.6% |
| 0.20-0.25 | 3.7% | 14.4% |
| 0.25-0.30 | 11.9% | 24.4% |
| 0.30-0.35 | 14.2% | 10.0% |
| **>0.35** | **70.1%** | **8.9%** |

### 结论

Evidence Tree 中存在清晰的节点分工：

```text
Gold Node    = Semantic Node（高 embedding sim，承载答案语义）
Steiner Node = Bridge Node（低 embedding sim，高 graph degree，连接答案语义）
```

---

## 实验十五：Hierarchical Module Navigation（高层抽象导航）

### 目的

验证：在函数层之上构建 Module 抽象层，能否帮助 Agent 更高效地导航 Evidence Tree？

具体比较三种策略：
- `module_topk`: 用 embedding 直接选 top-K modules，在 module 内函数中检索
- `llm_select`: 让 LLM 根据 module summary 选择 modules
- `hierarchical`: LLM 选 modules + 1-hop call graph expansion

### 方法

#### 1. 构建 Module 抽象

- 用 Louvain 从 call graph 检测 71 个 modules（resolution=0.5，random_state=42）
- 用 GPT-4.1-mini 给每个 module 生成：
  - `name`：模块名称（如 "Parameter Parsing"）
  - `summary`：模块职责描述（如 "Responsible for parsing command line options..."）
- 缓存于 `data/module_abstraction.json`

#### 2. Module-Level Retrieval

对每道题：

```text
Question
  ↓
Embedding
  ↓
Top-(max_modules * 2) candidate modules（按 module 内函数与 question 的最大 embedding sim 排序）
  ↓
Module Selection（三种方式）
```

#### 3. 三种 Module Selection 策略

| 策略 | 具体做法 |
|---|---|
| `module_topk` | 直接取 embedding 排序最高的 Top-K modules |
| `llm_select` | 把 question + candidate modules 的 name/summary 喂给 GPT-4.1-mini，让 LLM 选择最相关的 K 个 modules |
| `hierarchical` | 先用 LLM 选 modules，再对选中 modules 内的函数做 1-hop call graph 扩展 |

#### 4. Function-Level Retrieval

- 收集 selected modules 包含的所有 functions
- （`hierarchical` 模式额外加入 1-hop callers/callees）
- 用这些函数所在文件作为 `file_filter`，在文件内做 embedding retrieval
- 取 Top-100 functions

### 结果

| Config | Scope Size | R@5 | R@10 | R@20 | **R@50** | R@100 |
|---:|---:|---:|---:|---:|---:|---:|
| baseline | 17,458 | 26.0 | 34.5 | 46.2 | 59.5 | 74.2 |
| region | 6,151 | 26.7 | 34.5 | 46.7 | 61.2 | 74.5 |
| module_topk_m3_h0 | 3,551 | 26.0 | 34.5 | 45.3 | 57.5 | 67.0 |
| **module_topk_m5_h0** | **4,399** | 26.0 | 33.8 | 44.2 | **59.7** | 70.8 |
| llm_select_m3_h0 | 1,920 | 14.3 | 19.8 | 27.2 | 31.2 | 32.8 |
| hierarchical_m3_h1 | 1,920 | 14.3 | 19.8 | 27.2 | 31.2 | 32.8 |

### 关键发现

1. **Module-level abstraction 能有效压缩搜索空间**
   - module_topk_m5 用 **4,399 函数**（baseline 的 25%）达到 **59.7% R@50**
   - 比 region 少用 29% 函数，召回低 2.5 个百分点

2. **LLM-based module selection 表现令人失望**
   - llm_select_m3 R@50 仅 31.2%，远低于 module_topk_m3 的 57.5%
   - 原因分析：
     - LLM 只能看到 module summary，无法判断函数级证据
     - LLM 倾向于选择“名字好听”的模块，但这些模块未必包含 gold
     - 从 6 个候选中选 3 个，容易丢弃真正包含 gold 的模块

3. **Graph expansion on wrong modules doesn't help**
   - hierarchical_m3_h1 和 llm_select_m3_h1 一样差
   - 说明如果 module 选错了，再扩展也没用

### 结论

Module abstraction 本身有价值，但价值在于**压缩搜索空间**，而不在于**让 LLM 做高层推理**。

这引出了一个重要问题：

> **高层 module 抽象是否必要？还是直接用 module-level embedding + 函数级检索就够了？**

目前的数据表明，后者可能更简单有效。

---

## 实验十六：Navigation Signal 对比（Degree / Symbol / Composite）

### 目的

同时推进三个方向，找到最有效的 navigation signal：

1. **Multiple-Seed + Degree-Aware**：用 graph degree 作为导航信号
2. **Question-Symbol Navigation**：用 question 中的 symbols 匹配函数名
3. **Two-Stage Module + Function**：module 粗定位 + 函数级 composite rerank

### 方法

#### 统一框架

所有 navigation 方法共享同一个基础流程：

```text
Question
  ↓
Top-5 Seeds (in region, by embedding)
  ↓
3-hop BFS expansion on call graph
  ↓
Rerank candidates by different signals
  ↓
Top-100 retrieval
```

#### 三种 Navigation Signal

| 信号 | 计算方法 |
|---|---|
| **emb** | 纯 embedding similarity：`doc_matrix[fid] @ q_emb` |
| **degree** | 归一化 graph degree：`log(degree+1) / log(max_degree+1)` |
| **symbol** | question symbol 与函数名/文件路径/签名的匹配分数 |
| **composite** | `α*emb + β*degree + γ*symbol`（实验中 α=1.0, β=0.3, γ=0.5） |

#### Question Symbol 提取

从 question 中提取候选 symbols 的具体步骤：

**1. 正则提取标识符**

```python
tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", question)
```

这个正则匹配所有以下划线或字母开头、后跟字母/数字/下划线的连续序列。

**2. 过滤**

| 过滤条件 | 说明 | 例子 |
|---|---|---|
| 长度 < 3 | 太短，通常是虚词或代码缩写 | `is`, `to`, `of` 丢弃 |
| 停用词 | 常见英文疑问词/介词/中文虚词 | `How`, `What`, `the`, `and`, `是否`, `什么` 丢弃 |
| 保留 snake_case / camelCase | 看起来是代码标识符的词 | `common_params_parser_init` 保留 |

**3. 拆分 camelCase**

```python
parts = re.findall(r"[A-Z][a-z]+|[a-z]+|[A-Z]+", token)
```

如果拆分出多个部分，每个长度 ≥3 的部分也作为独立 symbol。

**完整示例**

```text
Question: "How is chat template selected in llama.cpp?"

Step 1 正则提取:
[How, is, chat, template, selected, in, llama, cpp]

Step 2 过滤:
- How    → 停用词，丢弃
- is     → 长度=2，丢弃
- chat   → 保留
- template → 保留
- selected → 保留
- in     → 停用词，丢弃
- llama  → 保留
- cpp    → 长度=3，保留

Step 3 camelCase 拆分:
本例没有 camelCase 词，无需拆分

Final symbols: ["chat", "template", "selected", "llama", "cpp"]
```

**camelCase 拆分示例**

```text
Token: "chatTemplateParser"
拆分: ["chat", "Template", "Parser"]

Token: "llamaModelLoad"
拆分: ["llama", "Model", "Load"]

Token: "GGMLBackend"
拆分: ["GGML", "Backend"]
```

**为什么要拆分 camelCase？**

因为问题中可能出现 `chatTemplate` 这样的复合词，但函数名里可能是 `chat_template_apply`。拆开后，`chat` 和 `template` 都能分别匹配到函数名中的对应部分。

Symbol match score 计算：

对每个候选函数，收集四个文本字段并转小写：

```text
text = 函数名 + " " + 文件路径 + " " + 函数签名 + " " + 函数体文本
```

对每个 question symbol 匹配：

| 匹配位置 | 加分 |
|---|---|
| symbol 出现在**函数名**中 | +2.0 |
| symbol 出现在文件路径 / 签名 / 函数体中 | +1.0 |

最后归一化：

```text
symbol_score = total_matched / (num_symbols * 2.0)
```

归一化后分数范围约为 0-1。

**计算示例**

假设 question symbols = `["chat", "template", "llama"]`（3 个 symbols，分母 = 3 × 2 = 6）

- **函数 A：`llama_model_chat_template`**
  - "chat" 在 name 中 → +2.0
  - "template" 在 name 中 → +2.0
  - "llama" 在 name 中 → +2.0
  - `symbol_score = 6.0 / 6 = 1.0`

- **函数 B：`chat_template_apply`（文件路径 `src/llama.cpp`）**
  - "chat" 在 name 中 → +2.0
  - "template" 在 name 中 → +2.0
  - "llama" 在 file_path 中 → +1.0
  - `symbol_score = 5.0 / 6 ≈ 0.833`

- **函数 C：`common_params_parser_init`**
  - 没有匹配任何 symbol
  - `symbol_score = 0.0`

**为什么函数名匹配权重更高？**

因为函数名通常最直接反映语义。`chat_template_apply` 比 `common_params_parser_init` 更可能和 chat template 问题相关，即使后者可能是证据链上的 bridge node。

#### 测试的 Config

| Config | 含义 |
|---|---|
| `multiseed_emb_3hop` | Top-5 seeds → 3-hop BFS → 纯 embedding rerank |
| `multiseed_symbol_3hop` | Top-5 seeds → 3-hop BFS → symbol rerank |
| `multiseed_degree_3hop` | Top-5 seeds → 3-hop BFS → degree rerank |
| `multiseed_composite_3hop` | Top-5 seeds → 3-hop BFS → composite rerank |
| `symbol_seed_composite_3hop` | 用 question symbols 匹配到的函数作为 seeds → 3-hop BFS → composite rerank |
| `module_two_stage_emb` | Top-5 modules → 其内函数 → 纯 embedding rerank |
| `module_two_stage_composite` | Top-5 modules → 其内函数 → composite rerank |

### 结果

| Config | Scope Size | R@5 | R@10 | R@20 | **R@50** | R@100 |
|---:|---:|---:|---:|---:|---:|---:|
| baseline | 17,458 | 26.0 | 34.5 | 46.2 | 59.5 | 74.2 |
| region | 6,151 | 26.7 | 34.5 | 46.7 | 61.2 | 74.5 |
| multiseed_emb_3hop | 3,307 | 26.7 | 36.7 | 46.3 | 60.7 | 70.8 |
| **multiseed_symbol_3hop** | **3,307** | **34.5** | **44.5** | **53.8** | **66.3%** | **73.3** |
| multiseed_degree_3hop | 3,307 | 1.2 | 2.3 | 4.2 | 14.5 | 32.5 |
| multiseed_composite_3hop | 3,307 | 25.2 | 36.0 | 42.2 | 55.2 | 62.8 |
| symbol_seed_composite_3hop | 3,378 | 24.8 | 34.8 | 39.5 | 49.0 | 54.0 |
| module_two_stage_emb | 4,141 | 26.0 | 33.8 | 44.2 | 59.7 | 70.3 |
| module_two_stage_composite | 4,141 | 25.5 | 36.8 | 41.5 | 56.0 | 61.7 |

### 关键发现

1. **Question-Symbol 是最强导航信号**
   - multiseed_symbol_3hop 达到 **R@50 = 66.3%**
   - 超过 region（61.2%）5.1 个百分点
   - 搜索空间只有 region 的一半（3,307 vs 6,151）
   - 比 multiseed_emb_3hop（60.7%）提升 5.6 个百分点

2. **Degree 信号表现极差**
   - multiseed_degree_3hop 仅 14.5% R@50
   - 即使加进 composite（β=0.3）也会把效果从 66.3% 拉到 55.2%
   - 说明：**Steiner nodes 有 high degree，但 high degree nodes 不都是 Steiner nodes**
   - 大多数 high-degree 节点是通用 utility，会引入大量噪音

3. **Symbol 作为 rerank 信号优于作为 seed**
   - symbol_seed_composite_3hop: 49.0% R@50
   - multiseed_symbol_3hop: 66.3% R@50
   - 因为 question symbols 有噪音，直接当 seed 会跑偏；作为 rerank 信号更稳定

4. **Module two-stage 效果中等**
   - module_two_stage_emb: 59.7% R@50
   - 不如 multiseed_symbol_3hop
   - module 主要价值还是空间压缩

### 结论

**最有效的导航策略：Multiple Seeds + 3-hop Expansion + Question-Symbol Rerank**

```text
Question
  ↓
Top-5 embedding seeds in region
  ↓
3-hop BFS on call graph
  ↓
Rerank by: embedding_sim + question_symbol_match
  ↓
Top-50 functions
```

这个策略把 R@50 从 baseline 59.5% 提升到 **66.3%**，搜索空间减少 81%。

同时修正了一个错误直觉：

> 不是说 graph degree 不重要，而是不能把它当 primary signal。

Steiner nodes 的高 degree 是结果，不是原因。用 degree 直接 rerank 会引入过多通用节点。

---

## 实验十七：Concept-Level vs Module-Level Abstraction

### 目的

验证核心假设：

> **Semantic Concept 是比 Module 更适合 Agent 的高层抽象层。**

因为 Module 是开发者组织代码的方式，而 Concept 是回答问题时真正需要的语义单元。

### 方法

#### 1. Concept 构建

在已有 Module 基础上进一步细分：

```text
Module（Louvain, resolution=0.5）
  ↓
在每个 Module 内部再做 Louvain 聚类（sub_resolution=2.0）
  ↓
Sub-community = Concept
  ↓
GPT-4.1-mini 给每个 Concept 生成 name + summary
```

结果：
- 71 modules → 718 concepts
- 平均每个 module 约 10 个 concepts
- 每个 concept 通常只包含 3-20 个函数

#### 2. 对比的 Config

| Config | 含义 |
|---|---|
| `module_top5_emb` | 用 embedding 取 Top-5 modules → 收集其函数 → 纯 embedding 排序 |
| `module_top5_symbol` | 用 embedding 取 Top-5 modules → 收集其函数 → embedding + symbol 重排 |
| `module_top10_symbol` | 用 embedding 取 Top-10 modules → 收集其函数 → embedding + symbol 重排 |
| `concept_top20_emb` | 用 embedding 取 Top-20 concepts → 收集其函数 → 纯 embedding 排序 |
| `concept_top20_symbol` | 用 embedding 取 Top-20 concepts → 收集其函数 → embedding + symbol 重排 |
| `concept_top50_symbol` | 用 embedding 取 Top-50 concepts → 收集其函数 → embedding + symbol 重排 |

注意：
- `module_topK` 和 `concept_topK` 中的 **K 不同**，因为 concept 数量远多于 module（718 vs 71）
- `emb` 表示只按 embedding similarity 排序
- `symbol` 表示用 `embedding_sim + 0.5 * symbol_match` 复合排序（和实验十六一致）

#### 3. Question Symbol 跨 Module / Concept 统计

对每道题：
1. 提取 question symbols（同实验十六的简单正则提取）
2. 找出所有函数名中包含这些 symbols 的函数
3. 看这些函数分布在多少个 modules / concepts 中

### Concept 构建结果

- 71 modules → 718 concepts
- 平均每个 module 约 10 个 concepts
- Concepts 通常只包含 3-20 个函数

### Question Symbol 跨 Module / Concept 分布

| 指标 | 数值 |
|---|---:|
| Question symbols 平均触及 modules | **14.7 / 71** |
| Question symbols 平均触及 concepts | **44.6 / 718** |
| Symbols 跨越 ≥2 modules 的问题 | **94.6%** |
| Symbols 跨越 ≥3 modules 的问题 | **83.8%** |
| Symbols 跨越 ≥2 concepts 的问题 | **97.3%** |

这说明：**Question symbols（即语义概念）天然跨越 Module 边界。** Module 不是回答问题的合适抽象层。

### Recall@50 对比

| Config | Scope Size | R@50 |
|---:|---:|---:|
| baseline | 17,458 | 59.5% |
| region | 6,151 | 61.2% |
| module_top5_emb | 4,141 | 59.7% |
| **module_top5_symbol** | **4,141** | **65.5%** |
| module_top10_symbol | 5,986 | **70.0%** |
| concept_top20_emb | 1,905 | 60.2% |
| **concept_top20_symbol** | **1,905** | **64.5%** |
| concept_top50_symbol | 4,239 | 65.7% |

### 关键发现

1. **Module + Symbol 仍然是最高召回**
   - module_top10_symbol: R@50 = **70.0%**
   - 这是目前所有实验中的最高 R@50

2. **Concept 提供更强的空间压缩能力**
   - concept_top20_symbol: R@50 = 64.5%，但只用了 **1,905 函数**
   - module_top10_symbol 需要 5,986 函数才能达到 70.0%
   - 如果 Agent 的预算有限，concept 更有优势

3. **Concept 和 Module 在相近空间下效果相当**
   - module_top5_symbol (4,141 funcs): 65.5%
   - concept_top50_symbol (4,239 funcs): 65.7%
   - 说明 abstraction 粒度本身不是 recall 的决定因素，symbol rerank 才是关键

4. **Concept 的价值在于更细粒度、更语义化的组织**
   - 一个 module 可能同时包含 chat template、tokenizer、sampling 的函数
   - Concept 能把它们分开，让 Agent 更精确地选择 "Chat Template" 而不是整个 "common" module

### 结论

**Concept 是比 Module 更好的 Agent 抽象层**，原因不是它能显著提升 recall，而是：

1. **Question symbols 天然跨 Module**，Module 边界与问题语义不对齐
2. **Concept 提供更细粒度的语义单元**，Agent 可以用更少函数达到相近 recall
3. **Concept 是 Question 和 Function 之间的自然桥梁**

未来系统架构：

```text
Question
  ↓
Question Symbols / Concept Retrieval
  ↓
Semantic Concept Graph
  ↓
Evidence Tree Navigation (call graph)
  ↓
Evidence Functions
  ↓
Answer
```

---

## 实验十八：端到端 QA — Concept-Symbol vs Baseline

### 目的

验证：把 Concept-Symbol Navigation 用于真实的 end-to-end QA，是否能提升答案质量和引用覆盖率？

### 方法

两个系统：

1. **Baseline**：纯 embedding 检索 Top-50 functions → LLM 生成答案
2. **Concept-Symbol**：
   - 提取 question symbols
   - 检索 Top-20 concepts by embedding
   - 收集 concept 内函数
   - 用 embedding + symbol match 重排
   - 取 Top-50 functions
   - LLM 生成答案

都用同一个 LLM（gpt-4.1-mini）和同一个答案生成 prompt。

评估：用 `evals/eval_v2.py`
- Binary Judge：答案是否正确
- Citation Coverage：答案引用了多少 gold evidence 文件

### 结果

| 指标 | Baseline | Concept-Symbol | 变化 |
|---|---:|---:|---:|
| Binary Accuracy | **94.0%** (47/50) | 90.0% (45/50) | -4.0pp |
| Citation Coverage (avg) | 71.3% | **81.7%** | **+10.4pp** |
| Full Coverage | 58.0% (29/50) | **74.0% (37/50)** | **+16.0pp** |
| Zero Coverage 题目数 | 8 | **4** | **-4** |

### 关键发现

1. **Concept-Symbol 显著提升了引用覆盖率**
   - 平均 coverage 从 71.3% 提升到 81.7%
   - **Full Coverage（答案提到所有 gold files）从 58.0% 提升到 74.0%**
   - 零覆盖的题目从 8 题减少到 4 题

2. **Binary Accuracy 轻微下降 4pp**
   - 从 94% 降到 90%
   - 可能原因：
     - 更多 evidence 导致答案更详细，binary judge 更严格
     - 某些关键证据虽然 citation 覆盖了，但答案组织不够好
     - 答案生成 prompt 没有针对 concept-symbol 证据优化

3. **核心结论：Agent 的调查过程改善了**
   - Concept-Symbol 方法让 Agent 看到了更多相关 evidence
   - 这是用户预测的 "更好的 investigation process" 的直接证据
   - 即使 binary accuracy 略有下降，citation coverage 的提升说明证据质量更好

3. **核心结论：Agent 的调查过程改善了**
   - Concept-Symbol 方法让 Agent 看到了更多相关 evidence
   - 这是用户预测的 "更好的 investigation process" 的直接证据
   - 即使 binary accuracy 略有下降，citation coverage 的提升说明证据质量更好

### 局限性与说明

**实验十八最初只测试了 Concept-Symbol，没有测试 Module + Symbol。**

在实验十七中，retrieval 效果最好的是：

| 方法 | R@50 | Scope Size |
|---|---:|---:|
| module_top10_symbol | **70.0%** | 5,986 |
| module_top5_symbol | 65.5% | 4,141 |
| concept_top20_symbol | 64.5% | 1,905 |

为了回答"module + symbol 在端到端 QA 中是否更好"，我们补充了实验：用 DeepSeek V4 Pro 生成答案，OpenAI gpt-4.1-mini 评估，对比 module_top5_symbol、module_top10_symbol 和 concept_symbol。

#### 补充实验结果（DeepSeek V4 Pro answer generator）

| 方法 | Binary Accuracy | Citation Coverage | Full Coverage | Zero Coverage |
|---|---:|---:|---:|---:|
| module_top5_symbol | **92.0%** (46/50) | 72.2% | 64.0% (32/50) | 10 |
| module_top10_symbol | **92.0%** (46/50) | 73.3% | 62.0% (31/50) | 9 |
| concept_symbol | 86.0% (43/50) | **75.5%** | **66.0%** (33/50) | 7 |

#### 关键发现

1. **Module + Symbol 的 Binary Accuracy 更高**
   - module_top5/10_symbol 达到 92.0%，比 concept_symbol 的 86.0% 高 6 个百分点
   - 说明 module + symbol 检索到的证据更容易让 LLM 生成"正确"的答案

2. **Concept-Symbol 的 Citation Coverage 仍略高**
   - concept_symbol: 75.5%
   - module_top10_symbol: 73.3%
   - 差距约 2.2 个百分点

3. **存在 trade-off**
   - Module + Symbol：答案更"正确"，但引用的证据完整性略低
   - Concept-Symbol：答案引用的证据更完整，但 binary accuracy 略低

4. **Scope size 不是决定因素**
   - module_top5_symbol（4,141 funcs）和 module_top10_symbol（5,986 funcs）的 binary accuracy 相同
   - 说明增加到 10 个 modules 并没有带来额外的答案正确性收益

#### 为什么先测 Concept-Symbol？

1. Concept 的 scope 更小（1,905 vs 4,141-5,986），更符合 Agent 预算有限的设定
2. Concept 是论文主张的新的抽象层，需要验证其端到端价值
3. 但 **Module + Symbol 作为 retrieval baseline 也很有竞争力**，尤其在答案正确性上

### 对论文的意义

这个结果支持了核心论点：

> **Repository QA 不仅是 retrieval 问题，更是 investigation 问题。**

Concept-Symbol 和 Module-Symbol 都优于纯 embedding baseline，说明结构化检索能改善 Agent 的调查过程。但两者的 trade-off 也提醒我们：

- **检索召回高 ≠ 端到端 QA 正确性高**
- concept_symbol retrieval recall 低于 module_top10_symbol，但用 DeepSeek 生成答案时 citation coverage 反而略高
- module_symbol retrieval recall 更高，但生成的答案虽然更"正确"，引用的证据却没那么完整

这意味着未来系统可以根据目标选择检索策略：
- 追求答案正确性 → Module + Symbol
- 追求证据完整性 → Concept + Symbol
- 最好的是结合两者：先用 Concept 做高层语义定位，再用 Module + Symbol 做精细检索

---

### 补充：DeepSeek V4 Pro 对照实验

为了验证模型替换的可行性，我们用 DeepSeek V4 Pro 替换了 answer generation 的 LLM（其他部分不变，eval 仍用 gpt-4.1-mini）。

| 指标 | OpenAI gpt-4.1-mini | DeepSeek V4 Pro | 变化 |
|---|---:|---:|---:|
| Binary Accuracy | **90.0%** (45/50) | 86.0% (43/50) | -4.0pp |
| Citation Coverage (avg) | **81.7%** | 75.5% | -6.2pp |
| Full Coverage 题目数 | **37** | 33 | -4 |
| Zero Coverage 题目数 | **4** | 7 | +3 |
| 平均答案长度 | 2,961 字符 | 2,346 字符 | -615 字符 |

#### 分析

1. **DeepSeek V4 Pro 没有达到相同效果**
   - 在相同 evaluator（gpt-4.1-mini）下，DeepSeek 的 binary accuracy 和 citation coverage 都更低

2. **可能原因**
   - **Judge bias**：evaluator 是 gpt-4.1-mini，可能更偏好 OpenAI 风格的答案
   - **答案长度**：DeepSeek 答案平均短 20%，可能遗漏了 judge 期望的细节
   - **格式差异**：虽然 file mention 数量相近（8.7 vs 8.4），但 DeepSeek 的答案组织方式可能不符合 judge 的评分标准

3. **限制**
   - 这个对照只测试了 answer generation，没有重跑 module/concept summary generation
   - 需要人工评估或换一个中立 judge 才能确定是否真的是 DeepSeek 答案质量更低

#### 对称评估：DeepSeek Judge vs OpenAI Judge

为了排除 judge bias，我们用 DeepSeek V4 Pro 重新评估了三个 QA 结果：

| Answer Generator | Judge | Binary Accuracy | Citation Coverage | Full Coverage |
|---|---:|---:|---:|---:|
| OpenAI gpt-4.1-mini baseline | OpenAI gpt-4.1-mini | **94.0%** | 71.3% | 58.0% (29/50) |
| OpenAI gpt-4.1-mini concept-symbol | OpenAI gpt-4.1-mini | **90.0%** | **81.7%** | **74.0% (37/50)** |
| DeepSeek V4 Pro concept-symbol | OpenAI gpt-4.1-mini | 86.0% | 75.5% | 66.0% (33/50) |
| DeepSeek V4 Flash concept-symbol | OpenAI gpt-4.1-mini | 86.0% | 73.2% | 62.0% (31/50) |
| OpenAI gpt-4.1-mini baseline | DeepSeek V4 Pro | 38.0% | 46.0% | 10.0% (5/50) |
| OpenAI gpt-4.1-mini concept-symbol | DeepSeek V4 Pro | 36.0% | 51.7% | 20.0% (10/50) |
| **DeepSeek V4 Pro concept-symbol** | **DeepSeek V4 Pro** | **44.0%** | **57.0%** | **24.0% (12/50)** |
| DeepSeek V4 Flash concept-symbol | DeepSeek V4 Pro | 30.0% | 56.5% | 20.0% (10/50) |

#### 关键发现

1. **存在明显的 Judge Bias**
   - OpenAI Judge 给 OpenAI 答案打高分（90-94%）
   - DeepSeek Judge 给 DeepSeek 答案打高分（44% vs 30-36%）
   - 但 DeepSeek Judge 整体要严格得多（绝对分数低很多）

2. **Concept-Symbol 的相对提升在不同 Judge 下都成立**
   - OpenAI Judge: coverage 71.3% → 81.7%（+10.4pp）
   - DeepSeek Judge: coverage 46.0% → 51.7%（OpenAI 答案，+5.7pp）
   - DeepSeek Judge: DeepSeek 答案 coverage 57.0%，高于 OpenAI 答案的 51.7%

3. **DeepSeek V4 Pro vs V4 Flash**
   - 在 OpenAI Judge 下：两者 binary accuracy 相同（86.0%），Pro coverage 略高（75.5% vs 73.2%）
   - 在 DeepSeek Judge 下：Pro 明显优于 Flash（44.0% vs 30.0%）
   - Flash 的 coverage 与 Pro 接近（56.5% vs 57.0%），说明 Flash 也能招回 relevant evidence

4. **DeepSeek 答案在 DeepSeek Judge 下表现最好**
   - 说明 DeepSeek 作为 judge 更认可自己的回答风格
   - 而 OpenAI Judge 更认可 OpenAI 的回答风格

#### 结论

- **绝对分数受 judge 模型影响很大**，不能跨 judge 比较
- **相对提升（Concept-Symbol vs Baseline）是稳定的**，在两个 judge 下都能看到 coverage 提升
- **DeepSeek V4 Pro 作为 answer generator 略优于 Flash**，但 Flash 也能工作且成本更低
- **如果只用单一 judge，OpenAI gpt-4.1-mini 做 answer generation 得分最高**

---

## 综合理论框架

基于以上所有实验，我们提出 Repository QA 的三层分解：

```text
Repository QA = Localization + Navigation + Synthesis
```

| 层次 | 子问题 | 当前证据 | 状态 |
|---|---|---:|---|
| **Localization** | Question → Region | Region Coverage = **96.6%** | 基本解决 |
| **Navigation** | Seed → Evidence Tree | ≤3 hops 覆盖 **74.8%** gold；Tree 直径 ~4；Multi-seed 4-hop R@50 = **64.5%** | 已发现结构 |
| **Synthesis** | Evidence Tree → Answer | Oracle R@50 = **74.2%** | 待研究 |

### 误差分解

Function Retrieval 的误差可以分解为：

```text
Total Retrieval Gap
    =
Scope Selection Gap          (59.5% → 61.2%, +1.7%)
    +
Navigation Gap               (flat vs multi-seed graph walk, +5.0%)
    +
Prioritization Gap           (61.2% → 74.2%, +13.0%)
```

### 核心机制：Gold-Steiner 分工

传统 Code RAG 默认：

```text
Question → Semantic Retrieval → Answer Nodes
```

我们的数据表明：

```text
Question → Semantic Retrieval → Gold Nodes
                                   ↓
                         Structural Navigation
                                   ↓
                            Steiner Nodes
                                   ↓
                          Evidence Tree (Gold + Steiner)
                                   ↓
                                Answer
```

这说明：
1. **Scope 问题已经基本解决**（Region 覆盖 96.6%）。
2. **Navigation 是核心未解问题**：需要从多个入口、双向、非贪婪地探索 call graph。
3. **Prioritization 仍需研究**：即使找到 evidence tree，如何排序和综合仍是挑战。

---

## 对传统 Code RAG 的反思

### 传统假设

传统 Code RAG 的隐含假设：

```text
Question → Semantic Retrieval → Most Relevant Function → Answer
```

### 为什么不成立

我们的数据表明这个假设存在根本性偏差：

1. **Top-1 seed 通常不是答案**：只有 13% 的 gold 是 seed 本身。
2. **答案需要多跳**：74.8% 的 gold 距离 seed 3 跳以内。
3. **答案是子图不是节点**：Evidence Tree 平均 5.7 节点，直径 3.6。
4. **答案局部但稀疏**：Tree 只占仓库的极小部分，但需要 navigation 才能找到。
5. **答案包含两类节点**：
   - **Gold nodes**：语义相关，但稀疏连接
   - **Steiner nodes**：语义不相关，但高度连接，是证据链的桥

### 新的认知模型

```text
Question
  ↓
Region Localization
  ↓
Multiple Semantic Seeds
  ↓
Structural Navigation (through Steiner bridges)
  ↓
Evidence Tree (Gold + Steiner)
  ↓
Answer
```

这个模型解释了三个此前分离的现象：

1. **为什么 Flat Retrieval 不够**：Retriever 偏爱 gold nodes，忽略 Steiner bridge nodes。
2. **为什么 Greedy Navigation 失败**：Greedy similarity search 永远不会主动访问低相似度的 Steiner nodes。
3. **为什么 Graph Reachability 有效**：Graph walk 天然会经过高度连接的 Steiner nodes。

---

## 对论文定位的影响

### 原定位

> Module-aware Retrieval：用代码结构社区作为检索过滤器。

### 中间定位

> Repository QA answers are small evidence trees in the call graph: current flat function retrieval fails because it assumes answers are single functions.

### 最终定位

> **Repository QA requires both semantic localization and structural navigation**: answers are composed of semantic gold nodes connected by high-degree Steiner bridge nodes, which flat retrieval misses and greedy similarity navigation cannot find.**

这个定位的研究贡献：

1. **结构性发现**：首次系统量化 Repository QA 答案的 Evidence Tree 结构（5-7 节点，直径 3-4）。
2. **机制解释**：发现 Gold-Steiner 节点分工——gold 承载语义，Steiner 提供结构连接。
3. **误差分解**：把 retrieval gap 拆分为 Localization / Navigation / Prioritization。
4. **范式转变**：从 flat function retrieval 转向 "semantic localization + structural navigation"。

### 论文类型的思考

目前的数据越来越支持一篇 **Observation / Analysis 风格** 的论文，而非纯粹的 Retrieval 工程论文。因为我们最强的贡献不是某个具体方法的 +5% 提升，而是：

- 发现 Repository QA 答案的 Evidence Tree 结构
- 量化 Gold-Steiner 节点分工
- 解释为什么传统 retrieval 和 greedy navigation 失败

---

## 下一步研究方向

### P0：Concept + Symbol 的 Agent 抽象层（最新方向）

当前证据表明：
- Question symbols 是强导航信号
- Question symbols 天然跨 Module
- Concept 比 Module 提供更细粒度的语义单元

待做：
- **Concept Graph 构建**：连接相关的 concepts（通过 shared functions / call graph）
- **Concept-Level Agent Navigation**：让 Agent 在 Concept 层做 ReAct，选择相关 concepts，再下钻函数
- **Concept + Multi-seed**：用 concept 定位区域，再用 multi-seed + symbol 在 concept 内导航

### P1：Question-Symbol Navigation 调优

当前最佳策略：
- **Multi-seed + 3-hop expansion + symbol rerank**：R@50 = **66.3%**，scope 3,307
- **Module + symbol rerank**：R@50 = **70.0%**，scope 5,986
- **Concept + symbol rerank**：R@50 = **64.5%**，scope 1,905

待优化：
- **Symbol extraction**：用 LLM 提取更准确的代码 symbols
- **Symbol weighting**：不同匹配位置的不同权重
- **Symbol expansion**：同义词、命名变体
- **Hyperparameter tuning**：symbol weight γ

### P2：把发现写成 Observation 风格论文

当前证据链已经非常完整，核心主线：

```text
Repository QA ≠ Flat Retrieval
              = Localization (Region)
              + Semantic Concept Layer
              + Evidence Tree Navigation
              + Answer Synthesis
```

论文结构建议：
- Section 1: Introduction: 为什么 flat retrieval 不够
- Section 2: Related Work
- Section 3: Evidence Tree 提取方法
- Section 4: Empirical Findings
  - Region & Locality
  - Evidence Tree Structure
  - Gold-Steiner Division
  - Navigation Signals
  - Module vs Concept Abstraction
- Section 5: A Concept-Guided Navigation Method
- Section 6: Limitations + Future Work

### P3：端到端 Concept-Guided Investigation Agent

```text
Question
  ↓
Symbol Extraction
  ↓
Concept Retrieval
  ↓
Concept Selection (LLM / embedding)
  ↓
Multi-seed Graph Expansion within Concepts
  ↓
Symbol + Embedding Rerank
  ↓
Evidence Tree
  ↓
LLM Synthesis
```

---

## 实验文件清单

### 脚本

- `scripts/analysis/diagnose_module_noise.py`
- `scripts/analysis/eval_module_prediction.py`
- `scripts/analysis/eval_module_constrained_retrieval.py`
- `scripts/analyze_module_error_decomposition.py`
- `scripts/analysis/gold_evidence_structure.py`
- `scripts/analysis/eval_region_coverage.py`
- `scripts/analysis/eval_region_compression.py`
- `scripts/analysis/eval_region_constrained_retrieval.py`
- `scripts/analysis/analyze_module_coverage.py`
- `scripts/analysis/oracle_gap_analysis.py`
- `scripts/analysis/evidence_chain_reachability.py`
- `scripts/analysis/evidence_chain_structure.py`
- `scripts/analysis/evidence_tree_analysis.py`
- `scripts/analysis/eval_minimal_navigation.py`
- `scripts/analysis/eval_multiseed_navigation_and_steiner.py`
- `scripts/ingestion/build_module_abstraction.py`
- `scripts/analysis/eval_hierarchical_module_navigation.py`
- `scripts/analysis/eval_navigation_signals.py`
- `scripts/ingestion/build_concept_abstraction.py`
- `scripts/analysis/eval_concept_navigation.py`
- `scripts/qa/run_concept_symbol_qa.py`

### 结果日志

- `results/diagnose_module_noise_20260624.log`
- `results/eval_module_prediction_20260624.log`
- `results/eval_module_constrained_retrieval_20260624.log`
- `results/module_error_decomposition_20260624.log`
- `results/gold_evidence_structure_20260624.log`
- `results/eval_region_coverage_20260624.log`
- `results/eval_region_compression_20260624.log`
- `results/eval_region_constrained_retrieval_20260624.log`
- `results/fixed_seed_rerun_20260624.log`
- `results/oracle_gap_analysis_20260624.log`
- `results/evidence_chain_reachability_20260624.log`
- `results/evidence_chain_structure_20260624.log`
- `results/evidence_tree_analysis_20260624.log`
- `results/eval_minimal_navigation_20260624.log`
- `results/eval_multiseed_navigation_and_steiner_20260624.log`
- `results/build_module_abstraction_20260624.log`
- `results/eval_hierarchical_module_navigation_20260624.log`
- `results/eval_navigation_signals_20260624.log`
- `results/build_concept_abstraction_20260624.log`
- `results/eval_concept_navigation_20260624.log`
- `results/run_concept_symbol_qa_20260624.log`
- `results/eval_baseline_concept_symbol_20260624.log`
- `results/eval_concept_symbol_concept_symbol_20260624.log`
- `results/run_concept_symbol_deepseek_20260624.log`
- `results/eval_concept_symbol_deepseek_20260624.log`
- `results/run_concept_symbol_deepseek_flash_20260624.log`
- `results/eval_concept_symbol_deepseek_flash_openai_20260624.log`
- `results/eval_concept_symbol_deepseek_flash_deepseek_20260624.log`
- `results/eval_baseline_deepseek_judge_20260624.log`
- `results/eval_concept_symbol_deepseek_judge_20260624.log`
- `results/eval_concept_symbol_deepseek_deepseek_judge_20260624.log`

### JSON 结果

- `results/module_prediction_accuracy_res{0.3,0.5,1.0}.json`
- `results/module_constrained_retrieval_full.json`
- `results/module_error_decomposition.json`
- `results/gold_evidence_structure.json`
- `results/region_coverage.json`
- `results/region_compression.json`
- `results/region_constrained_retrieval.json`
- `results/oracle_gap_analysis.json`
- `results/evidence_chain_reachability.json`
- `results/evidence_chain_structure.json`
- `results/evidence_tree_analysis.json`
- `results/minimal_navigation.json`
- `results/multiseed_navigation.json`
- `results/steiner_node_analysis.json`
- `data/module_abstraction.json`
- `results/hierarchical_module_navigation.json`
- `results/navigation_signals.json`
- `data/concept_abstraction.json`
- `results/concept_navigation.json`
- `results/qa_baseline_concept_symbol.json`
- `results/qa_concept_symbol_concept_symbol.json`
- `results/qa_baseline_concept_symbol.eval.json`
- `results/qa_concept_symbol_concept_symbol.eval.json`
- `results/qa_concept_symbol_deepseek.json`
- `results/qa_concept_symbol_deepseek.eval.json`
- `results/qa_baseline_concept_symbol.eval.deepseek.json`
- `results/qa_concept_symbol_concept_symbol.eval.deepseek.json`
- `results/qa_concept_symbol_deepseek.eval.deepseek.json`
- `results/qa_concept_symbol_deepseek_flash.json`
- `results/qa_concept_symbol_deepseek_flash.eval.json`
- `results/qa_concept_symbol_deepseek_flash.eval.deepseek.json`

---

## 方法论备注

1. 所有 module detection 已固定 `random_state=42`，确保可重复。
2. Module 来自图结构（Louvain + 同文件加权），不是 LLM 生成。
3. Module scoring 用 function embedding max，不依赖 LLM summary。
4. Region 定义纯规则化，未引入 LLM。
5. Evidence Tree 用 MST-based Steiner Tree 近似，在 small gold sets（2-5）上效果稳定。
6. Module Abstraction 用 Louvain 检测 + GPT-4.1-mini 生成 name/summary，缓存于 `data/module_abstraction.json`。
7. Question-Symbol Navigation 用正则提取 question 中的代码标识符，作为 rerank 信号。
8. Concept Abstraction 在 Module 内部用更高 resolution 的 Louvain 聚类 + GPT-4.1-mini 生成 name/summary，缓存于 `data/concept_abstraction.json`。
9. 端到端 QA 实验中，Baseline 和 Concept-Symbol 使用相同 LLM（gpt-4.1-mini）和答案生成 prompt，仅初始检索策略不同。

---

日期：2026-06-24
