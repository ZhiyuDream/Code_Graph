# 探索感知 Agent（Exploration-Aware Agent）

这是一个尝试：让 LLM 从"选择下一步行动"转变为"识别遗漏证据"，从而指导系统性探索。

## 设计动机

之前所有 ReAct prompt 都在问 LLM："下一步做什么？"这导致：
- 读一个文件就开始脑补；
- 形成假设后反复验证自己；
- 重复 search；
- 提前 finish。

我们希望 LLM 只负责**战略规划**（选哪个目录/模块可能包含缺失证据），具体文件读取由确定性 worklist 执行。

## 完整 Pipeline

### Phase 1：确定性扫描候选池

1. 用原问题做 embedding retrieval，取 Top20 文件；
2. 去重，得到 unique candidate files；
3. 每个文件读前 100 行；
4. 记录 `visited_files` 和 `visited_dirs`。

### Phase 2：多轮 LLM 引导探索

每轮执行：

**Step 1：Missing Evidence Detector（LLM）**

输入：原问题 + 已访问文件摘要 + 已访问目录 + 未访问目录。

输出：
```text
SUPPORTED_HYPOTHESIS: 当前最可能成立的解释
MISSING_EVIDENCE: 还缺什么证据
UNEXPLORED_REGIONS: 哪些未访问区域可能包含证据
FALSIFICATION_TARGET: 什么能推翻当前解释
SUGGESTED_SEARCH_QUERIES: 3-5 个英文搜索查询
```

**Step 2：Query Grounding（确定性）**

对每个 `SUGGESTED_SEARCH_QUERIES` 和原问题做 embedding retrieval，收集返回文件所在的目录，作为候选目录。

**Step 3：Directory Planner（LLM）**

输入：原问题 + 候选目录（附带 top 文件名和文件数）+ 当前发现 + 缺失证据。

输出：
```json
{
  "directory": "ggml/src/ggml-sycl",
  "reason": "设备切换核心实现目录"
}
```

**Step 4：确定性目录读取**

在选中目录内，用原问题 embedding 取 top 15 个文件，读前 100 行。

重复 5 轮。

### Phase 3：计算 Coverage

将 `visited_files` 与 gold evidence 对比。

**注意：** 这里的 coverage 是**文件级检索召回覆盖率**（系统访问的文件是否覆盖 gold evidence），不是答案生成后引用 gold 文件的完整率。我们没有对这个 agent 跑答案生成 + 引用评估。

## 全量 Benchmark 结果

| 指标 | 数值 |
|------|-----:|
| 完整覆盖率（检索召回） | 31/50 = **62%** |
| 平均覆盖率（检索召回） | 80.8% |
| 平均读取文件数 | 36.0 |
| 平均访问目录数 | 12.2 |

**注意：** 以上 coverage 均为文件级检索召回覆盖率，不是答案引用完整率。

## 与确定性方法对比

| 方法 | 完整覆盖率（检索召回） | 平均覆盖率 | 平均读取文件数 |
|------|-----------:|----------:|-------------:|
| Exploration-Aware Agent | 62% | 80.8% | 36.0 |
| 确定性 Worklist | 86% | 91.0% | 50.8 |
| 符号图 Worklist | 90% | 95.0% | 62.0 |

**结论：** 虽然比 Naive ReAct（22%）好，但显著低于确定性 worklist（86%）。

## 失败案例分析

### posthoc_public_001（设备切换）

Gold files：`ggml/src/ggml-sycl/common.cpp`, `cpy.cpp`, `element_wise.cpp`

5 轮目录选择：
```text
round1: ggml/src/ggml-virtgpu/backend
round2: ggml/src/ggml-virtgpu
round3: ggml/src/ggml-sycl      ← 方向对了
round4: src
round5: ggml/src/ggml-hexagon/htp
```

虽然 round3 进入了 `ggml-sycl`，但只读了 4 个文件（`ggml-sycl.cpp`, `element_wise.cpp` 等），没有读到 `common.cpp` 和 `cpy.cpp`。

Coverage：33%。

### posthoc_public_002（聊天模板）

Gold files：`common/common.cpp`, `common/chat.cpp`

5 轮目录选择：
```text
round1: common/jinja
round2: ggml/src/ggml-sycl/dpct
round3: examples/llama.android/lib/src/main/cpp
round4: examples/simple-chat
round5: examples/lookup
```

LLM 一看到"模板"就往 `common/jinja` 跑，然后在 `examples/` 里打转，从未进入 `common/common.cpp`。

Coverage：33%。

## 失败原因分析

### 1. LLM 不真正懂仓库结构

`ggml/src` 下有十几个后端目录，只看目录名，LLM 经常猜错。设备切换问题先选了 `virtgpu`，聊天模板问题选 `jinja`。

### 2. 进了对目录也读不全

即使进入 `ggml-sycl`，由于每目录只读 top 15 文件，而 gold 文件在该目录内排名不高，仍然遗漏。

### 3. 探索预算分配不合理

5 轮中往往只有 1-2 轮进对方向，其他轮数浪费在无关目录。

### 4. 缺少"邻近目录"偏好

如果 `common/chat.cpp` 已访问，agent 没有优先探索 `common/` 内其他文件，而是跳到了 `examples/`。

## 改进方向

### 方向 A：Hybrid（确定性 worklist + LLM 诊断）

先用确定性 worklist 跑到 90%，然后让 LLM 针对剩余 gap 做一次 targeted expansion。此时 LLM 已经知道"缺什么"，决策难度大幅降低。

### 方向 B：Query-Grounded Directory Planner

LLM 不直接选目录，而是：
1. 输出 search queries；
2. 系统把 queries 映射到具体文件/目录；
3. LLM 在这些 grounded candidates 里选择。

这样可以避免 LLM 凭空猜目录。

### 方向 C：约束 LLM 只选相邻目录

限制 LLM 只能从已访问目录的同层或父目录中选择，减少乱跳。

## 相关代码

- `src/qa/investigation/exploration_aware.py`
- `prompts/exploration_missing_evidence.txt`
- `prompts/exploration_directory_planner.txt`
- `prompts/exploration_counter_hypothesis.txt`
- `experiments/run_exploration_aware_investigator.py`
- `results/exploration_aware_hard_S20_R5_F15_L100.json`
