# Code Graph QA 系统状态报告（2026-06-09）

## 1. 概述

本报告汇总了 Code Graph v2 QA 系统截至 2026-06-09 的实验结果、当前 Pipeline 架构和已完成的改进。

**核心结论**：
- Easy Benchmark 检索阶段表现优秀（96% 全召回），瓶颈在**答案生成阶段**（LLM 主动忽略已检索证据）
- Hard Benchmark 检索和引用阶段双双崩塌，瓶颈在**检索层**（自然语言问题无法映射到具体符号）
- 当前系统已从 Flat Retrieval 范式演进到 **Repository Investigation 范式**，采用 Symbol Fast Path + ReAct 自主决策

---

## 2. 当前 Pipeline 架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                        QAPipeline (src/qa/pipeline.py)              │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  1. Query Analysis                                                  │
│     └── QueryAnalyzer.analyze(question)                            │
│         └── 判断 query_type: symbol_centric | component_centric    │
│             | architecture_centric | unknown                        │
│         └── 提取 symbols（反引号函数名）、components（模块名）      │
│                                                                     │
│  2. Initial Search                                                  │
│     ├── IF symbol_centric AND symbols exist:                        │
│     │   └── Symbol Fast Path: grep_symbol_files(symbol)            │
│     │       └── rg -w symbol → 返回包含该符号的所有文件            │
│     │       └── 限制 grep retriever 只在这些文件内搜索             │
│     │       └── embedding 不再硬调，留给 ReAct 自主决定            │
│     │                                                               │
│     └── ELSE:                                                       │
│         └── 空状态进入 ReAct，由 LLM 自主决定工具                 │
│                                                                     │
│  3. ReAct Loop (max 5 steps, src/qa/agent_loop.py)                 │
│     ├── decide(): LLM 选择下一步 action                            │
│     │   └── 可用 actions:                                          │
│     │       - grep_search（精确关键词搜索）                        │
│     │       - semantic_search（embedding 语义搜索）               │
│     │       - expand_callers（扩展调用者/上游）                    │
│     │       - expand_callees（扩展被调用者/下游）                  │
│     │       - read_class（读取完整类实现）                         │
│     │       - sufficient（信息足够，停止循环）                     │
│     │                                                               │
│     ├── execute(): 执行 action，获取新函数/证据                    │
│     └── 早期停止: 连续 2 步新增 < 2                                │
│                                                                     │
│  4. Expansion (src/qa/expansion.py)                                │
│     ├── LLM 判断哪些函数值得展开（签名 → 完整实现）               │
│     └── Fallback: 无条件展开 top 10                                │
│                                                                     │
│  5. Answer Generation (src/core/answer_generator.py)               │
│     ├── build_full_context(): 构建上下文（预算 100K 字符）         │
│     │   └── 优先展示问题中提到的核心函数                           │
│     └── call_llm(): 生成答案（max_tokens=8000）                    │
│         └── Prompt: 聚焦核心函数、标注文件路径、列出参考清单        │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.1 核心组件

| 组件 | 文件 | 职责 |
|------|------|------|
| QueryAnalyzer | `src/qa/query_analyzer.py` | 分析问题类型，提取 symbols/components |
| GrepRetriever | `src/qa/retrievers/grep.py` | ripgrep 关键词搜索，提取函数边界 |
| EmbeddingRetriever | `src/qa/retrievers/embedding.py` | 语义搜索（向量索引） |
| GraphRetriever | `src/qa/retrievers/graph.py` | Neo4j 图遍历（callers/callees） |
| IssueRetriever | `src/qa/retrievers/issue.py` | GitHub Issue/PR 检索 |
| CodeExpander | `src/qa/expansion.py` | 渐进式代码展开（签名→实现→类→文件） |
| ReActLoop | `src/qa/agent_loop.py` | LLM 驱动的多轮决策循环 |
| TraceRecorder | `src/qa/trace.py` | 全程记录每步召回、时延、token |

### 2.2 Prompt 体系

| Prompt | 用途 | 关键约束 |
|--------|------|----------|
| `query_analysis.txt` | 分析查询类型 | 分类为 symbol/component/architecture/unknown |
| `react_decide_flexible.txt` | ReAct 决策 | 信息不足时继续检索，5+ 相关函数可 sufficient |
| `answer_generation.txt` | 答案生成 | 聚焦核心函数、标注 `file.cpp:start-end`、列参考清单 |
| `expansion_decide.txt` | 展开判断 | LLM 选择值得展开的函数 |

---

## 3. 实验结果总览

### 3.1 Easy Benchmark（`posthoc_audit_benchmark_v2.json` 前 50 题）

结果文件：`benchmark_symbol_fastpath_20260607_131010.json`

| 指标 | 数值 |
|------|------|
| 总题数 | 50 |
| 检索全召回 | **48/50 (96%)** |
| 引用全（人工） | **44/50 (88%)** |
| 引用全（LLM） | **46/50 (92%)** |
| 引用部分（人工） | 6/50 |
| 引用部分（LLM） | 3/50 |
| 平均检索覆盖 | ~85% |
| 平均引用覆盖（人工） | ~88% |
| 平均引用覆盖（LLM） | **95.0%** |
| 整体噪声 | **63.6%** (189/297) |
| 平均 latency | ~43s |

**引用不全（人工评估 6 题）**：
- 检索失败：1 题（audit_020 arg.cpp）
- 搜到未引：5 题（audit_011 speculative.cpp、audit_012 common.cpp、audit_014 common.cpp、audit_016 preset.cpp、audit_037 arg.cpp）

**引用不全（LLM 评估 4 题）**：
- audit_011：0%（LLM 认为答案未引用任何 gold 文件）
- audit_035：50%（chat-diff-analyzer.cpp 未引用）
- audit_037：50%（arg.cpp 未引用）
- audit_045：50%（arg.cpp 搜到未引）

### 3.2 Hard Benchmark（`benchmark_hard.json` 50 题）

结果文件：`benchmark_hard_20260607_200601.json`

| 指标 | 数值 |
|------|------|
| 总题数 | 50 |
| 检索全召回 | **29/50 (58%)** |
| 引用全 | **8/50 (16%)** |
| 引用部分 | 42/50 |
| 平均检索覆盖 | ~71% |
| 平均引用覆盖 | ~42% |
| 整体噪声 | **85.1%** (467/549) |
| Binary Judge 正确率 | **46/50 (92%)** |

**引用不全的 42 题分类**：
- 检索失败：21 题
- 搜到未引：29 题（含部分覆盖+搜到未引）

### 3.3 Easy vs Hard 对比

| 维度 | Easy | Hard | 差距 |
|------|------|------|------|
| 检索全召回 | 96% | 58% | **-38pp** |
| 引用全（人工） | 88% | 16% | **-72pp** |
| 引用全（LLM） | 92% | — | — |
| 噪声 | 63.6% | 85.1% | **+21.5pp** |
| 问题特征 | 100% 有反引号函数名 | 自然语言描述，无显式符号 | 关键差异 |

---

## 4. 关键发现

### 4.1 检索层问题

**Easy benchmark 检索优秀的原因**：
- 每题都有反引号函数名（如 `` `ggml_sycl_set_device` ``）
- Symbol Fast Path 直接 `grep -w symbol` 就能定位到包含该符号的所有文件
- 搜索空间从全仓几千个文件压缩到几十个相关文件

**Hard benchmark 检索失败的原因**：

| 根因 | 题数 | 说明 |
|------|------|------|
| 问题无显式符号名 | 21/21 | 自然语言描述场景，没有反引号函数名 |
| 模块完全跑偏（0%覆盖） | 5 | SYCL→CANN、backend→hexagon/virtgpu |
| 字符串 helper 定位困难 | 4 | trim/strip/whitespace → chat-auto-parser-helpers.cpp |
| common/arg.cpp 系统性遗漏 | 5 | 问题不提"参数解析"，arg.cpp 检索不到 |
| 跨目录配对遗漏 | 7 | 同一功能分散在 common/ + src/ + ggml/ |

### 4.2 引用层问题

**搜到未引的本质**：LLM 在答案生成阶段**主动 cherry-pick**，而非噪声干扰。

最有力的证据：
- Easy benchmark 中 audit_016：检索 0% 噪声，preset.cpp 搜到了但仍被忽略
- Hard benchmark 中 audit_029：检索到了 arg.cpp 和 preset.cpp，但答案引用了 `vendor/cpp-httplib/httplib.cpp`（完全无关的第三方库）

**漏引集中在三类文件**：

| 文件 | 漏引次数 | 原因 |
|------|----------|------|
| `common/arg.cpp` | 5 | 问题未直接提及"参数解析" |
| `common/chat-auto-parser-helpers.cpp` | 4 | helper 实现文件，LLM 聚焦调用方 |
| `common/common.cpp` | 4 | 被当作"太泛"的基础设施忽略 |

**LLM 主动引用噪声的根因**：
- examples/ → 被当作"调用方证据"
- tools/ → 被当作"实际应用场景"
- vendor/ → 纯幻觉（httplib.cpp、miniaudio.h）

### 4.3 噪声与引用的关系

| 题目类别 | 平均噪声 |
|----------|----------|
| Easy 引用全 | 47.7% |
| Easy 引用不全 | 56.0% |
| Hard 引用全 | ~70% |
| Hard 引用不全 | ~88% |

**结论**：噪声差异不是引用不全的决定性因素。audit_044（Easy，96% 噪声但引用全）vs audit_011（Easy，82% 噪声但引用不全）证明：**噪声的伪装性比噪声比例更重要**。

### 4.4 Binary Judge 的反差

Hard benchmark Binary Judge 正确率 92%（46/50），但引用覆盖率仅 42%。

这意味着：**LLM 在大量题目中没有引用 gold evidence，但结论碰巧对了**。这是危险的——LLM 可能在"蒙对"，如果 gold 中有反面证据，没引用就意味着没发现。

---

## 5. 改进历程

### 5.1 0604 之前：Flat Retrieval 范式

**问题**：
- 全局 embedding 搜索召回大量噪声（72%）
- CALLS 边增加 +82.9%，准确率几乎不变
- Module 节点（Louvain 聚类）反而 -0.9pp

**结论**：不是图不够准，而是检索路径不对。

### 5.2 0604：理论觉醒 + AST 解析修复

**改进**：
1. **提出 Repository Investigation 范式**：从 `Question → Retrieval → Answer` 升级为 `Question → Repository Cognition → Investigation Planning → Navigation → Evidence Collection → Verification → Answer`
2. **修复 AST Parser 缺陷**：C++ 类成员函数调用 free function 未被记录到 Neo4j（22 处遗漏中的 54.5% 根因）
3. **50 题 benchmark 跑通**：Coverage 67.8%（mechanical）/ 85.3%（human-eval）

### 5.3 0605：文档路由失败，符号定位成功

**尝试 1：文档路由（失败）**
- 假设：README/docs section 可以作为"认知路由层"
- 实现：markdown section 切分 → BM25 索引 → 文档匹配 → 提取文件引用
- 结果：File Hit Rate 仅 13.8%，Zero Hit 68%
- 失败根因：llama.cpp 文档不引用实现文件；BM25 假阳性高

**尝试 2：符号直接定位（成功）**
- 假设：问题中的函数名本身就是最强路由信号
- 实现：`grep -w symbol` 搜索 → 返回包含该符号的所有文件
- 结果：File Hit Rate **100%**，Perfect Hit **50/50**
- 提出 Query Type 分类：symbol_centric / component_centric / architecture_centric

### 5.4 0606：Symbol Fast Path 工程化 + 去 Embedding 硬编码

**改进清单**：

| 改动 | 文件 | 效果 |
|------|------|------|
| Symbol Fast Path | `pipeline.py` | 检索函数数 36→14.5（-60%），Gold 覆盖率 76%→91.2% |
| 移除 embedding 硬编码 | `pipeline.py` | 延迟 88.4s→43.2s（-51%），Coverage 持平 |
| 分层 top_k | `grep.py` | Symbol Fast Path `top_k=0` 不截断；ReAct `top_k=5` |
| 目录优先级排序 | `call_chain.py` | `common/` > `src/` > `ggml/` > `tests/`/`examples` |
| 修复 grep 解析 bug | `grep.py` | 大函数>500行丢弃、初始化列表`{}`干扰、`} catch`误识别 |
| 删除静默 fallback | `agent_loop.py` | 非法 action 时强制 sufficient |
| Grep V2 工具 | `grep_search_v2.py` | `rg --json` 替代脆弱正则、支持 `-A/-B/-C`、max-columns 截断 |

**去 Embedding 效果**：
- 旧（emb+top_k）：Coverage 70.3%，Avg Latency 88.4s
- 新（分层 top_k）：Coverage 69.5%，Avg Latency **43.2s**
- LLM 在需要时主动调用 embedding（平均 4 次/题）

### 5.5 0607：精细化分析

**产出**：
1. Easy benchmark 逐题噪声分析 — 发现噪声不是引用不全的根因
2. Hard benchmark Binary Judge — 92% 正确率但 42% 引用覆盖
3. 4 道错题分析 — 全部是"共享 helper 复用影响"维度
4. 覆盖率 0% 但答对的反直觉发现

---

## 6. 当前瓶颈与下一步

### 6.1 瓶颈分层

```
┌─────────────────────────────────────────────────────┐
│  答案生成层 (Answer Generation)                      │
│  ├─ Easy: LLM 主动忽略已检索证据 (19/50 搜到未引)    │
│  └─ Hard: 同上，但更严重 (29/50 搜到未引)            │
├─────────────────────────────────────────────────────┤
│  检索层 (Retrieval)                                  │
│  ├─ Easy: 几乎解决 (48/50 全召回)                    │
│  └─ Hard: 严重问题 (29/50 全召回)                    │
│      ├─ 自然语言→符号映射失败                        │
│      ├─ 模块跑偏 (SYCL→CANN)                         │
│      └─ 跨目录配对遗漏                               │
├─────────────────────────────────────────────────────┤
│  图/索引层 (Graph/Index)                             │
│  └─ 基本稳定，AST 解析缺陷已修复                     │
└─────────────────────────────────────────────────────┘
```

### 6.2 下一步优先级

| 优先级 | 方向 | 具体措施 | 预期收益 |
|--------|------|----------|----------|
| P0 | 答案生成 prompt | 强制要求"分析并引用所有检索到的证据文件" | Easy 引用全 88%→95%+ |
| P0 | 自然语言→符号映射 | 让 LLM 先从问题中推断目标符号，再进入 Symbol Fast Path | Hard 检索全 58%→80%+ |
| P1 | 检索噪声过滤 | 降低 examples/tests/tools 过度召回 | 噪声 85%→60% |
| P1 | Binary Judge 校验 | 对比"有引用"vs"无引用"题目的答案质量 | 识别"蒙对"题目 |
| P2 | Reranker | 对检索结果做相关性重排序 | 降低 LLM 注意力分散 |

---

## 7. 附录

### 7.1 实验文件索引

| 文件 | 说明 |
|------|------|
| `results/benchmark_symbol_fastpath_20260607_131010.json` | Easy benchmark 最佳结果 |
| `results/benchmark_hard_20260607_200601.json` | Hard benchmark 最佳结果 |
| `datasets/posthoc_audit_benchmark_v2.json` | Easy benchmark 数据集 |
| `datasets/benchmark_hard.json` | Hard benchmark 数据集 |
| `docs/research/0604/benchmark_report.md` | DeepSeek v4-pro 评估报告 |
| `docs/research/0604/root_cause_analysis.md` | AST 解析缺陷根因分析 |
| `docs/research/0605/scope_routing_experiment_report.md` | 文档路由实验报告 |
| `docs/research/0606/research_status_summary.md` | 研究状态总结 |
| `docs/research/0606/experiment_report_no_embedding_hardcode.md` | 去 embedding 硬编码实验 |
| `docs/research/0607/hard_benchmark_retrieval_insights.md` | Hard benchmark 检索洞察 |
| `docs/research/0610/hard_benchmark_retrieval_failure_analysis.md` | 检索失败逐题分析 |
| `docs/research/0610/hard_benchmark_citation_omission_analysis.md` | 搜到未引逐题分析 |
| `docs/research/navigation_pipeline_implementation_plan.md` | 导航 Pipeline 实现计划 |

### 7.2 关键代码文件

| 文件 | 说明 |
|------|------|
| `src/qa/pipeline.py` | 主 Pipeline 编排 |
| `src/qa/agent_loop.py` | ReAct 决策循环 |
| `src/qa/query_analyzer.py` | 查询类型分析 |
| `src/qa/retrievers/grep.py` | Grep 关键词检索 |
| `src/qa/retrievers/embedding.py` | Embedding 语义检索 |
| `src/qa/expansion.py` | 渐进式代码展开 |
| `src/search/grep_search_v2.py` | Grep V2 工具（Claude Code 风格） |
| `src/core/answer_generator.py` | 答案生成 |
| `src/core/llm_client.py` | LLM 调用客户端 |
