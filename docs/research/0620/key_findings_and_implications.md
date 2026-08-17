# 核心发现与研究意义

## 发现一：当前 Agent 的主要损失来自探索不足

最直接证据：

```text
Naive ReAct (10 步，平均读 3.3 文件) = 22% 完整覆盖率
机械读取 Top5 文件                      = 22% 完整覆盖率
```

ReAct 的实际搜索效率只相当于读 5 个文件。这说明：

> 当前 LLM Agent 并不缺少代码理解能力，而是缺少系统性的调查策略。

## 发现二：搜索预算曲线是单调上升的

| 预算 | 完整覆盖率 |
|---:|---:|
| 5 | 22% |
| 10 | 40% |
| 20 | 60% |
| 50 | 78% |
| 100 | 86% |
| 200 | 94%（实际平均读 135 个文件） |

注：真正的文件级 Top-200 天花板单独计算为 **98%**（49/50）。上面的 94% 是因为函数块级检索去重后凑不够 200 个文件。

覆盖率随文件读取预算平滑上升，没有出现 LLM 突然"顿悟"并大幅超越机械读取的点。

**意义：** 在仓库级代码 QA 中，**覆盖搜索空间本身比精巧推理更重要**。

## 发现三：确定性 Worklist 超越 LLM Agent

| 方法 | 完整覆盖率 |
|------|-----------:|
| Naive ReAct | 22% |
| Exploration-Aware Agent | 62% |
| 确定性 Worklist | 86% |
| 符号图 Worklist | 90% |

一个完全不使用 LLM 做导航的确定性 worklist，覆盖率是 LLM ReAct 的 4 倍。

**意义：** 当前 LLM 在 unconstrained action space 中做战术执行是不稳定的。系统性的确定性遍历更可靠。

## 发现四：目录扩展是最有效的单一操作

Top20 + 目录扩展（每目录 top 2）达到 78%，而 Top20 单独只有 60%。

在 20 个 under-covered 问题中，17 个可以通过读取同目录其他文件恢复。

**意义：** Gold 证据具有强烈的**目录局部性**。扩展已访问目录是性价比最高的操作。

## 发现五：LLM 在"识别"和"选择"上能力不同

- 成对比较（gold vs 非 gold）：89.7% 准确率。
- 从 Top20 池中选择 gold：52-60%。
- 从 100 个候选中选择：进一步下降到 74%。

**意义：** LLM 能识别证据，但不擅长从大量候选中筛选。这支持了"需要结构化收窄过程"的观点。

## 发现六：LLM 的价值在于查询扩展而非目录选择

查询扩展 worklist 达到 88%，比确定性 worklist（86%）略高。

它有效的地方是：把中文抽象问题（如"设备切换"）翻译成英文技术查询（"sycl device switch"），从而召回 embedding 全局排名很低的文件。

**意义：** LLM 的语义翻译能力有价值，但应该用于**生成查询**而非**直接选择文件或目录**。

## 研究叙事演进

```text
最初：Retrieval 不行
      ↓
发现：File 找到了，Function 没找到
      ↓
发现：Function 也找到了，LLM 选不出来
      ↓
发现：LLM 其实能识别，只是不会从 100 个里找
      ↓
现在：LLM 其实连"去哪找"都不稳定
      ↓
结论：核心瓶颈从 Selection Problem 转向 Exploration Problem
```

## 对论文的意义

### 可以主张的论点

1. **Repository QA 的核心瓶颈是探索策略，而非代码理解。**
   - 证据：确定性 worklist 90% vs ReAct 22%。

2. **当前 LLM Agent 存在过早停止问题。**
   - 证据：ReAct 平均只读 3.3 个文件；搜索预算曲线显示预算增加直接提升 coverage。

3. **系统性扩展已访问目录是高性价比策略。**
   - 证据：目录扩展从 60% 推到 78%。

4. **LLM 应作为检索查询生成器/诊断器，而非文件选择器。**
   - 证据：查询扩展有效，而 LLM 目录选择失败。

### 建议的论文结构

1. **Introduction**: 仓库级 QA 的挑战。
2. **Task & Metrics**: hard benchmark 50 题，文件级 coverage。
3. **Selection Decomposition**: 池召回 76%，选择 52-60%，成对识别 89.7%。
4. **Exploration Failure**: Search Budget Curve + ReAct vs Read-all。
5. **Deterministic Worklist**: 目录扩展 → 86%，符号图 → 90%。
6. **LLM-Guided Exploration Attempt**: Exploration-Aware Agent 62%，分析原因。
7. **Implications**: LLM 应做战略规划，确定性 traversal 做执行。
8. **Conclusion**。

## 下一步建议

### 高优先级

1. **把 Search Budget Curve 放进论文核心图。**
   它是最简洁有力的证据。

2. **做 Hybrid Agent。**
   确定性 worklist 到 90% → LLM 诊断剩余 gap → targeted expansion。
   这是目前最有希望接近 94-98% 天花板的方案。

3. **复现性验证。**
   如果同样的 Exploration Failure 现象能在其他仓库复现，结论会更有力。

### 中优先级

4. **成本分析。**
   比较确定性 worklist（几乎无 LLM 成本）与 LLM ReAct（每题多步 LLM 调用）的 token/时间开销。

5. **错误分析。**
   深入分析剩余 5-10% 问题的结构特征（如 common.cpp 类大文件、抽象中文问题等）。

### 低优先级

6. **继续优化 unconstrained ReAct prompt。**
   实验已表明这不是主要问题，收益有限。
