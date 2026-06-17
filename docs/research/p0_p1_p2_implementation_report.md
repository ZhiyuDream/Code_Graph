# P0/P1/P2 改进实现报告

**日期**: 2026-05-11 | **项目**: Code_Graph v5-hybrid-rag-best-performance

---

## 实验配置

| 配置项 | 值 |
|---|---|
| **目标仓库** | `/root/data/zzy/llama.cpp` (609 源文件) |
| **Benchmark 数据集** | `datasets/llama_cpp_QA_cleaned.json` (324 题) |
| **QA 实验脚本** | `experiments/module_expansion/run_qa_v8_react_ablation.py` |
| **评估脚本** | `tools/eval_benchmark.py` |
| **建图脚本** | `scripts/run_stage1.py` |
| **模型** | DeepSeek `deepseek-v4-pro` (max_tokens=8192) |
| **Provider** | `deepseek` (API: `https://api.deepseek.com/v1`) |
| **并行 Workers** | 20 |
| **检索配置** | `emb+grep+graph` |
| **ReAct Actions** | expand_callers, expand_callees, expand_same_file, expand_same_class, sufficient |
| **Neo4j 版本** | 5.26.25 Community |
| **LLM Timeout** | 600s |
| **clangd 版本** | clangd-20

---

## 一、已完成的改进

### P0: 数据清洗（已完成 ✅）

| 改进项 | 文件 | 说明 |
|---|---|---|
| **Vendor 过滤 outgoing calls** | `symbol_extractor.py` | `skip_vendor_calls=True` 时跳过 `vendor/` 目录函数的 outgoingCalls，减少噪声 |
| **声明/定义区分** | `symbol_extractor.py`, `models.py` | `FunctionSymbol` 新增 `is_definition` 字段，行数≥3 为定义，否则为声明 |
| **同名同文件去重** | `graph_assembler.py` | 按 `(file_path, name)` 分组，保留 body 最长的定义；去重后 ambiguous 自动升级为 resolved |

**效果**:
- Function 节点: 17,975 → **15,143** (-15.8%)
- 孤立函数比例: 22.5% → **0.0%**

### P1: 调用解析增强（已完成 ✅）

| 改进项 | 文件 | 说明 |
|---|---|---|
| **全局名称匹配** | `call_resolver.py` | `callee_file_path` 匹配失败时 fallback 到全局 `by_name_global`；优先同目录，其次全局唯一 |
| **宏调用提取** | `symbol_extractor.py` | 基于正则 `_MACRO_CALL_RE` 提取 `identifier(` 模式，排除关键字和已知函数名 |
| **caller_index Bug 修复** | `stage1_clangd.py` | `RawCall.caller_index` 是文件内局部索引，追加到全局列表前转换为全局索引 |

**效果**:
- CALLS 边: 9,835 → **17,992** (+82.9%)
- 有 CALLS 的函数比例: 3.7% → **30.5%**
- global_match 贡献: 15,866 条 CALLS 边 (88.2%)

### P2: Module 节点（已完成 ✅）

| 改进项 | 文件 | 说明 |
|---|---|---|
| **Louvain 社区发现** | `graph_assembler.py` | 基于 CALLS 边 + 文件共现（权重 0.5）构建加权图，`resolution=0.3` 产生大社区 |
| **小社区合并** | `graph_assembler.py` | < 10 个函数的小社区合并到连接最多的大社区 |
| **Module 节点写入** | `neo4j_batch_writer.py` | 支持 `Module` 节点、`BELONGS_TO`、`MODULE_CALLS` 边 |
| **Module 检索扩展** | `graph_retriever.py` | `_expand_module()` 召回命中函数所属 Module 的 Top 5 函数（按度数） |

**效果**:
- Module 节点: **222 个**（平均 68 函数/模块）
- MODULE_CALLS 边: **1,396 条**
- 最大模块: `mod_miniaudio` (1,525 funcs), `mod_cpp-httplib` (680 funcs), `mod_ggml` (605 funcs)

---

## 二、QA 验证结果

### 全量 324 题对比（DeepSeek emb+grep+graph）

| 配置 | 正确率 | 0-1 平均分 | 平均耗时 |
|---|---|---|---|
| 基线（改进前） | **83.0%** | — | 141.8s |
| P0/P1（无 Module） | **83.3%** | 0.795 | 146.3s |
| P2（有 Module） | **82.4%** | 0.803 | 160.9s |
| **P2 + 关键词过滤 + 高频降权** | **82.1%** | 0.761 | — |

### GPT-4.1-mini 全量验证（P0/P1/P2, emb+grep+graph+issue）

| 指标 | 数值 |
|---|---|
| 样本量 | 324/324 |
| 二元正确率 | **80.2%**（260/324）|
| 生成模型 | gpt-4.1-mini |
| 评判模型 | gpt-4.1-mini |

**分段正确率**：
- 索引 0-239: 80.4%（193/240）
- 索引 240-299: 85.0%（51/60）
- 索引 300-323: 66.7%（16/24）

**结论**：GPT-4.1-mini 在 P0/P1/P2 上的正确率为 80.2%，未超过其之前最佳成绩（82.4%），也未超过 DeepSeek 基线（83.0%）。P0/P1/P2 的 CALLS 边增加对 4.1-mini 同样没有显著帮助。

---

### 高频函数降权验证（P2 优化）

| 配置 | 正确率 | 说明 |
|---|---|---|
| P2（Module 扩展） | **82.4%** | 无关键词过滤、无降权 |
| **P2 + 关键词过滤 + 高频降权** | **82.1%** | 同模块召回增加关键词相关性过滤 + embedding/graph 高频函数降权 |

**结论**：高频函数降权效果 **-0.3pp**（在噪声范围内），未带来预期提升。DeepSeek 的 ~83% 天花板不是由"噪声函数"造成的。

### 关键结论

1. **P0/P1 对 DeepSeek 影响极小**: +0.3pp，在统计噪声范围内
   - DeepSeek 是"检索不敏感"模型，embedding + grep 已覆盖大部分信息需求
   - CALLS 边 +82.9% 没有转化为准确率提升

2. **P2 Module 扩展对 DeepSeek 有轻微负效果**: -0.9pp
   - 新增 26 道正确题，丢失 29 道正确题，净变化 -3 题
   - 原因: Module 扩展引入了噪声函数（同模块但无关），干扰了模型判断
   - 截断逻辑（top_k=10）导致 Module 扩展函数挤占原始命中函数配额

3. **REFERENCES_VAR 下降未影响准确率**: -24.4%，但 QA 结果稳定，说明去重过滤掉了无效引用

---

## 三、已发现的深层问题

### 1. Louvain 在稀疏图上的局限

CALLS 边密度仍然较低（1.19 条/函数），导致：
- 即使 resolution=0.3 + 文件共现辅助，社区质量仍有限
- 模块间调用边仅 1,396 条（跨模块比例低）
- vendor 库函数（如 miniaudio 1,525 个）聚类为超大模块，但无实际调用关系

### 2. Module 扩展的噪声问题

`_expand_module()` 召回同模块函数时未做相关性过滤：
- 召回的是"度数最高"的函数，而非"与问题最相关"的函数
- 导致上下文膨胀，增加了 LLM 的干扰信息

### 3. DeepSeek 的检索天花板

多次实验验证 DeepSeek 对检索增强不敏感：
- emb only: 82.4%
- emb+grep+graph: 83.0% / 83.3% / 82.4%
- CALLS 边 +82.9% 后: 仍 ~83%

**结论**: 在 DeepSeek 上，检索质量已接近天花板，进一步提升需要**模型本身**或**检索策略**的改进，而非单纯增加 CALLS 边。

---

## 四、准备做的改进

### 短期（1-2 天）

| 优先级 | 改进项 | 预期效果 | 说明 |
|---|---|---|---|
| 🔴 | **GPT-4.1-mini 全量验证** | 确认 P0/P1/P2 对 4.1-mini 的效果 | 4.1-mini 之前对 graph 更敏感，可能从 CALLS 边增加中受益 |
| 🔴 | **P2 Module 扩展优化** | +1-2% | 为 Module 扩展增加相关性过滤（只召回与问题关键词相关的同模块函数） |
| 🟡 | **高频函数降权** | +0.5-1% | 基于 PageRank 为函数标注权重，检索时降权高频基础函数（如 `ggml_malloc`、`ggml_free`） |

### 中期（1-2 周）

| 优先级 | 改进项 | 预期效果 | 说明 |
|---|---|---|---|
| 🟡 | **检索策略优化** | +1-3% | 初始 embedding TopK 从 3→5；ReAct 步数从 5→7；改进 expand 排序策略 |
| 🟡 | **Grep V3 改进** | +1-2% | 支持跨行匹配、模糊匹配、正则表达式搜索 |
| 🟢 | **错题归因分析** | 诊断性 | 分析 54 道错题中 Module 扩展的影响，确定哪些题类型受益于模块级检索 |

### 长期（2-4 周）

| 优先级 | 改进项 | 预期效果 | 说明 |
|---|---|---|---|
| 🟢 | **P3 AST 精确调用图** | +3-5% | 用 libclang 遍历 AST 提取 `CallExpr`，替代 clangd callHierarchy 的局限 |
| 🟢 | **向量索引重建** | +1-2% | 基于清洗后的函数节点（去重 + 过滤 vendor）重建 embedding 索引 |
| 🟢 | **多跳推理增强** | +2-3% | ReAct Agent 支持 3-4 跳调用链推理（当前仅 1-2 跳） |

---

## 五、代码变更清单

```
src/pipeline/models.py                    + is_definition 字段
src/pipeline/symbol_extractor.py          + vendor 过滤、宏提取、is_definition
src/pipeline/call_resolver.py             + 全局名称匹配、同目录优先
src/pipeline/graph_assembler.py           + 去重、Louvain 社区发现、Module 节点
src/pipeline/neo4j_batch_writer.py        + Module 节点/边支持
src/pipeline/stage1_clangd.py             + caller_index 全局转换
src/qa_framework/retrievers/graph_retriever.py  + Module 扩展检索
tools/eval_benchmark.py                   + 字段名兼容性修复 (question/reference/generated)
docs/research/p0_p1_p2_implementation_report.md  (本文档)
```

---

## 六、建议下一步

**最优先**: 跑一轮 **GPT-4.1-mini** 全量验证（emb+grep+graph），确认 P0/P1/P2 对 4.1-mini 是否有提升。

**理由**: 
- 4.1-mini 之前对 graph 更敏感（graph 净贡献 +9，DeepSeek 仅 +9 但总体不敏感）
- 4.1-mini 之前最佳成绩 82.4%，如果 P0/P1 的 CALLS 边 +82.9% 能提升其 graph 召回，可能突破 83%
- 成本可控（4.1-mini 比 DeepSeek 便宜）

---

*报告生成时间: 2026-05-11*
