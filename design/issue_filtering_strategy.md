# Issue 过滤策略设计方案

## 1. 背景与目标

### 1.1 问题背景

llama.cpp 代码智能问答系统中，Issue 节点是回答 Bug 排查、性能问题、Feature 需求等类型问题的核心证据来源。llama.cpp 全量 Issue 超 5500+ 条（截至 2026Q1），其中大量是不适合作为代码问答证据的纯讨论、文档修改、无效 Issue。

### 1.2 设计目标

1. **入库前过滤**：从 5500+ 条 Issue 中筛选出适合代码问答的高价值 Issue，目标入库 500~1000 条
2. **搜索时排序**：对于已在库中的 Issue，根据标签、时间、代码引用质量动态调整排序权重
3. **可解释性**：每条 Issue 的过滤/排序决策有明确规则可追溯

### 1.3 核心价值判断标准

**一条 Issue 适不适合作为代码问答证据？**

核心标准：Issue 描述中是否包含"代码上下文证据"（函数名、文件路径、commit SHA、错误日志片段），使得问答系统能据此给出有根有据的答案。

反例：无代码引用的 `+1` 式 issue、只有"我也有这个问题"的跟帖、长期无下文的 stale issue。

---

## 2. llama.cpp Issue 标签体系

### 2.1 真实标签统计

llama.cpp 共有 93 个标签（截至 2026Q1），按技术维度分类如下：

**按数量规模（估算）**：

| 标签类别 | 数量级别 | 代表标签 |
|---------|---------|---------|
| 功能请求 | 1000+ | `enhancement` |
| Stale | 3000+ | `stale` |
| Bug 相关 | ~600 | `bug` `bug-unconfirmed` `bugfix` |
| 硬件相关 | ~500 | `CUDA` `AMD GPU` `Intel GPU` `Nvidia GPU` `Vulkan` `SYCL` `RoCM` `WebGPU` |
| 服务器/API | ~300 | `server` `server/api` `server/webui` |
| 性能 | ~70 | `performance` |
| 退化/回归 | ~35 | `regression` |
| 严重性 | ~30 | `critical severity` `high severity` `high priority` |
| 其他 | 分散 | `build` `documentation` `question` `grammar` `tool calling` 等 |

### 2.2 标签价值分类

#### P0 — 核心证据型（必须入库/最高权重）

**这类 Issue 通常包含具体错误信息、代码路径、commit 引用，是代码问答的最佳证据。**

| 标签 | 理由 |
|------|------|
| `bug` | 真实错误报告，多数有复现步骤和代码上下文 |
| `bugfix` | 已修复，直接关联 PR，证据链最完整（number → body → FIXES PR）|
| `regression` | 特定版本退化，有 commit 可 bisect，根因明确 |
| `performance` | 性能问题，通常有量化数据（延迟/吞吐/显存）|
| `critical severity` / `high severity` | 影响面大，描述详细，优先级高 |
| `high priority` | 标注 priority，团队认可的问题 |

#### P1 — 模块证据型（有条件入库）

**这类 Issue 有明确模块指向，但描述质量参差，需要代码引用二次确认。**

| 标签 | 入库条件 |
|------|---------|
| `CUDA` / `Nvidia GPU` | 须含 commit 或 file path |
| `AMD GPU` / `AMD ZenDNN` / `Intel GPU` / `Vulkan` / `SYCL` / `WebGPU` / `RoCM` / `OpenCL` / `Kompute` | 须含 commit 或 file path |
| `Qualcomm NPU` / `Qualcomm QNN` / `Hexagon` / `Ascend NPU` / `OpenVINO` | 须含 commit 或 file path |
| `android` / `Apple Metal` / `Riscv` | 须含 commit 或 file path |
| `server` / `server/api` / `server/webui` | 须含 API 路径或文件路径 |
| `ggml` | 须含函数名或 file path |
| `llava` / `model` | 须含模型名和代码路径 |
| `build` / `CI / packaging` | 须含错误日志片段 |
| `grammar` / `tool calling` | 须含相关代码片段 |
| `regression` | 已是 P0，无需二次确认 |

#### P2 — 低权重参考型

**可以入库，但在搜索排序时权重较低；或需要高质量描述才入库。**

| 标签 | 理由 |
|------|------|
| `scheduler` | 调度/内存分配 bug，有具体代码逻辑 |
| `threading` | 并发问题，常有代码证据 |
| `chat parser` / `jinja parser` | 解析器问题，有代码片段 |
| `low severity` / `medium severity` | 低/中 severity bug，部分有代码上下文 |
| `breaking change` | API 变更，有文件路径证据 |
| `cli` | 命令行问题，部分有日志证据 |
| `enhancement` | 功能请求，多数无代码引用，属于 design 讨论 |
| `documentation` | 文档修改，无代码逻辑变化 |
| `question` | 纯讨论，多数无代码答案 |
| `embeddings` | 模型相关但描述质量参差 |
| `hardware` | 泛硬件问题，无具体后端指向 |

#### P3 — 排除型（不入库）

**这类 Issue 或无代码上下文，或生命周期已结束，或 Meta 类标签，不适合作为代码证据。**

| 标签 | 排除理由 |
|------|---------|
| `stale` | 14天无响应，无下文，代码可能已重构 |
| `duplicate` | 重复内容，以原 Issue 为准 |
| `invalid` | 无效 Issue |
| `wontfix` | 明确不修复，无实用价值 |
| `need more info` | 等待用户补充，当前无完整信息 |
| `need feedback` | 等待反馈，信息不完整 |
| `good first issue` | 入门级 issue，无复杂技术细节 |
| `help wanted` | 协作请求，非问题报告 |
| `demo` | 演示性内容，无实用价值 |
| `vibe-coded` | LLM 生成内容过多，需人工验证 |
| `obsolete?` / `merge ready` / `roadmap` | Meta 类，不反映代码问题 |

---

## 3. 代码引用信号

### 3.1 信号类型与权重

Issue body 中的代码上下文是判断是否入库的核心依据。信号分为三个层级：

| 信号类型 | 正则模式 | 权重 | 说明 |
|---------|---------|------|------|
| **Commit SHA** | `[0-9a-f]{7,40}` | 最高 | 直接指向特定代码版本，证据链完整 |
| **文件路径+行号** | `\w+\.(c\|cpp\|h)(:\d+)?` | 高 | 说明涉及具体源码 |
| **函数调用** | `\w+\(\)` | 中 | 提及具体函数，需配合其他信号 |
| **Issue 引用** | `#\d{5,}` | 低 | 关联其他 issue，说明是已知问题链 |

### 3.2 各标签的代码引用比例（经验估算）

| 标签 | Commit SHA | 文件路径 | 函数调用 |
|------|-----------|---------|---------|
| `bug` | 高 | 高 | 中 |
| `regression` | 很高 | 高 | 低 |
| `performance` | 中 | 高 | 中 |
| `enhancement` | 低 | 高 | 低 |
| `stale` | 低~中 | 高 | 低 |

**结论**：`bug` / `regression` + commit SHA 是最强信号组合，`file path` 过于通用不能单独作为锚点。

### 3.3 代码引用阈值

```
入库最低要求（满足其一）：
  ✓ commit SHA 存在
  ✓ (file path + line) 且 label ∈ P0

加分项（达到则提升排序权重）：
  ✓ commit SHA 存在：+0.3
  ✓ 函数调用存在：+0.1
  ✓ Issue 引用数量 >= 2：+0.1
```

---

## 4. 时间衰减策略

### 4.1 时间衰减背景

**2024Q2 前后是 llama.cpp 的大重构期**（GGUF 格式变更、server API 重构、GGML 整理），此前的 Issue 描述的代码可能已面目全非，需时间衰减。

### 4.2 时间衰减规则

```
age_score = 1.0                          # 2024+
age_score = 0.7                          # 2023
age_score = 0.3                          # 2022 及以前

staleness_penalty:
  labeled stale:   0.2                   # 极低权重
  not stale:       1.0
```

**stale 判断**：GitHub Issue 标签含 `stale`（14天无响应自动打标）。

---

## 5. 入库过滤规则（Ingestion Filter）

### 5.1 完整过滤流程

```
Step 1: 基础过滤
  ✗ skip if: state = open          # 只入库 closed issues
  ✗ skip if: label ∈ P3           # stale/duplicate/invalid/wontfix
  ✗ skip if: created < 2022-01-01  # 过于陈旧，代码已重构
  ✓ proceed if: label ∈ P0 且 (commit SHA OR file path)

Step 2: P1 条件确认
  If label ∈ P1:
    ✓ proceed if: (commit SHA OR file path) AND comments >= 1
    ✗ skip otherwise

Step 3: 重复检测
  If 标题与已有 Issue 相似度 > 0.85 (TF-IDF):
    ✗ skip (标记为潜在 duplicate)

Step 4: 最终入库
  所有通过的 Issue，MERGE 入 Neo4j（number 为唯一键）
```

### 5.2 预期入库规模

| 来源 | 过滤后预估 |
|------|---------|
| `bug` (closed) | 200~300 |
| `regression` | 25~30 |
| `performance` | 40~50 |
| `bugfix` (含 FIXES PR) | 50~80 |
| P1 标签（满足条件） | 100~150 |
| **合计** | **~500~700** |

---

## 6. 搜索排序规则（Search Ranking）

对于已在库中的 Issue（无论新旧），搜索结果按以下公式排序：

```
final_score = relevance_score × label_weight × age_penalty × staleness_penalty

其中：
  relevance_score = 标题匹配度 × 0.6 + body_关键词匹配度 × 0.4

label_weight:
  bug / bugfix / regression:           1.0
  performance / critical / high:        0.9
  P1 标签 (server/ggml/CUDA/...):      0.7
  P2 标签 (enhancement/question/...):   0.3

age_penalty:
  2024+:          1.0
  2023:           0.7
  2022及以前:     0.3

staleness_penalty:
  stale:          0.2
  not stale:      1.0
```

### 6.1 搜索结果示例

查询 `ROCm illegal memory access` 时：

| Issue # | 标签 | label_weight | age | stale | final_score 排序 |
|---------|------|-------------|-----|-------|-----------------|
| #20597 | bug + ROCm | 1.0 × 0.7 = 0.7 | 2025 | ✗ | **高** |
| #19900 | bug + ROCm | 1.0 × 0.7 = 0.7 | 2024 | ✗ | 中 |
| #18765 | bug + ROCm | 1.0 × 0.7 = 0.7 | 2023 | ✓ | **低** |
| #18002 | stale | 0.2 | 2022 | ✓ | **极低** |

---

## 7. 实施计划

### Phase 1: 入库脚本增强（immediate）
- 修改 `fetch_github_data.py` 或 `import_github_to_graph.py`
- 加入标签过滤逻辑（跳过 P3、open issues、过于陈旧）
- 加入代码引用检测（commit SHA / file path 二次确认）
- 目标：按本策略入库 500 条高质量 Issue

### Phase 2: 搜索排序增强（short-term）
- 在 `tool_search_issues()` 中加入 `final_score` 计算
- 返回结果时标注排序分和命中原因
- 目标：让最有价值的 Issue 排在前面

### Phase 3: 持续更新机制（medium-term）
- 定期增量抓取新 closed issues（每周一次）
- 增量入库 + 重排

---

## 8. 已知局限

1. **Commit SHA 无对应代码版本**：llama.cpp 仓库未打 tag 锚定历史 commit，SHA 只能说明涉及某次提交，但不保证能直接在当前代码库找到
2. **代码引用可能断裂**：Issue 描述的代码可能在后续重构中被移动或删除
3. **benchmark 覆盖度问题**：当前 benchmark 引用的 Issue 范围需要确认在库中已有对应数据

---

## 9. 参考

- GitHub API: `GET /repos/ggml-org/llama.cpp/issues`
- llama.cpp 真实标签: `https://api.github.com/repos/ggml-org/llama.cpp/labels`
- Neo4j Issue 节点 schema: `{number, title, body, labels, comments, state_reason, user}`
