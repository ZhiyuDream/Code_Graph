# Repository Investigation 研究现状总结

> **日期**: 2026-06-06  
> **分支**: `feat/navigation-architecture`  
> **目标仓库**: llama.cpp  
> **Benchmark**: `datasets/posthoc_audit_benchmark_v2.json` (50 题，post-hoc audit QA)

---

## 一、项目背景

### 核心主张

> **Code QA should not retrieve code. Code QA should investigate repositories.**

传统 RAG 范式把代码仓当成"一袋无序的代码片段"，通过语义相似度全局搜索。我们的主张是：

1. **Query Understanding 优先**：先理解用户问题的类型和关键信号
2. **Adaptive Navigation**：根据问题类型选择不同的搜索策略
3. **缩小搜索空间**：把搜索范围从"全仓几千个文件"压缩到"几十个相关文件"

### 现有系统架构

```
Question
  ↓
QueryAnalyzer (LLM) → query_type + symbols + components
  ↓
├─ symbol_centric → Symbol Fast Path (grep 定位)
├─ component_centric → Scope Routing (文档 BM25)
└─ architecture_centric → Global Search
  ↓
ReAct Loop → Answer
```

---

## 二、0604 成果

### 2.1 DeepSeek JSON 修复

**问题**: `call_llm_json` 频繁解析失败，导致 ReAct loop 强制终止。  
**根因**: DeepSeek thinking 模式下 `max_tokens=400`，reasoning_content 消耗 ~3300 tokens，留给 content 的空间不足，JSON 被截断。  
**修复**:
- `call_llm_json` 自动将 DeepSeek 的 `max_tokens` 提升到 4000
- 新增 `_no_reasoning_fallback` 参数，防止 reasoning_content 覆盖 content
- 三层 JSON 解析：code block → json.loads → json_repair

### 2.2 50 题 Benchmark 跑通

| 指标 | 数值 |
|------|------|
| 完成题数 | 50/50 |
| 0 JSON 失败 | 是 |
| 平均 ReAct 步数 | 6.0 |
| 平均 reasoning tokens | 3296 |
| Coverage (mechanical) | 76.3% |
| Coverage (human-eval) | 85.3% |

### 2.3 根因分析

22 个真正遗漏的证据分为两类：

| 根因 | 占比 | 说明 |
|------|------|------|
| **AST Parser 缺陷** | 54.5% (12/22) | C++ class member function 调用 free function 未被记录到 Neo4j |
| **Expansion Selector  overwhelmed** | 45.5% (10/22) | `llama_model_chat_template` 有 73 个 callers，selector max 10 无法选出 `common/common.cpp` |

### 2.4 语义搜索 Bug 修复

- `semantic_search.py`: index 文件名 `classic_rag_index.json` → `qa_embedding_index.json`
- `semantic_search.py`: field `meta.get('file')` → `meta.get('file_path')`
- `code_reader.py`: 统一字段访问 `func.get('file_path') or func.get('file')`
- 移除 `len(text)>200` 短路逻辑（曾掩盖 field name bug）

---

## 三、0605 探索：从文档路由到符号定位

### 3.1 文档路由实验（失败）

**假设**: README/docs 的 section 结构可以作为认知路由层，缩小搜索空间。  
**实现**: markdown section 切分 → BM25 索引 → 文档匹配 → 文件提取  
**结果**:

| 指标 | 数值 |
|------|------|
| File Hit Rate | **13.8%** |
| Perfect Hit | 2% (1/50) |
| Zero Hit | 68% (34/50) |

**失败根因**:
1. **文档不引用实现文件**：llama.cpp 的文档是用户指南，不会说"函数X在文件Y里"
2. **BM25 假阳性**：通用词（init, model, diff）匹配到无关 section
3. **路径匹配太宽泛**："Build" → `build/CMakeFiles/...`
4. **build/ 目录污染**：58% 的 scope 包含编译生成文件

**核心洞察**: 对于审计类问题，README/docs **结构性缺失** → 实现文件的映射。

### 3.2 符号直接定位实验（成功）

**假设**: 问题中的函数名本身就是最强的路由信号。  
**实现**: 提取反引号函数名 → `rg` 搜索 → 返回包含该函数的所有文件  
**结果**:

| 指标 | 数值 |
|------|------|
| File Hit Rate | **100%** |
| Perfect Hit | 100% (50/50) |
| Zero Hit | 0% (0/50) |

**对比**:

| 方法 | Avg Hit Rate |
|------|-------------|
| Document Routing | 13.8% |
| Symbol + Embedding Index | 61.7% |
| **Symbol + Grep** | **100%** |

### 3.3 Adaptive Navigation 洞察

**核心发现**: "缩小搜索空间"不应该是一个**必须执行**的动作。

```
Question
  ↓
Signal Analysis
  ↓
判断是否需要 Routing
  ↓
Search
```

Benchmark 50 题 100% 是 **symbol-centric**（每题都有反引号函数名）。对这类问题：
- ❌ 不需要 Repository Cognition
- ❌ 不需要 Topic Routing
- ❌ 不需要 Document Graph
- ✅ 只需要提取函数名 + grep

**Query Type 分类**:

| 类型 | 占比 (当前 benchmark) | 策略 |
|------|---------------------|------|
| symbol_centric | 100% | Direct Jump |
| component_centric | 0% | Scope Routing |
| architecture_centric | 0% | Document Navigation |

---

## 四、当前状态（0606）：Symbol Fast Path

### 4.1 已实现代码

| 文件 | 说明 |
|------|------|
| `src/qa/query_analyzer.py` | LLM-based Query Understanding（prompt 在 `prompts/query_analysis.txt`） |
| `src/qa/symbol_search.py` | `grep_symbol_files()` 工具函数 |
| `src/qa/scope_planner.py` | Document-based Scope Routing（V0，当前未命中 symbol-centric 问题） |
| `src/qa/document_index.py` | Markdown section 切分 + BM25 索引 |
| `src/qa/retrievers/embedding.py` | 新增 `file_filter` 参数 |
| `src/qa/retrievers/grep.py` | 新增 `file_filter` 参数 |
| `src/qa/pipeline.py` | 集成 QueryAnalyzer + Symbol Fast Path + fallback |
| `prompts/query_analysis.txt` | Query Analysis LLM prompt（供审核） |

### 4.2 Symbol Fast Path 完整 50 题结果

**结果文件**: `results/benchmark_symbol_fastpath_20260605_203801.json`

| 指标 | Symbol Fast Path | 旧系统 (全局搜索) | 变化 |
|------|-----------------|------------------|------|
| 完成题数 | 50/50 | 50/50 | - |
| 检索函数数 | **14.5** | ~36 | **-60%** |
| ReAct 步数 | **5.4** | 6.0 | **-10%** |
| Gold 文件覆盖率 | **91.2%** | ~76% (mechanical) | **+15pp** |
| Perfect hit | **76%** (38/50) | - | - |
| Partial hit | 24% (12/50) | - | - |
| Zero hit | **0%** (0/50) | - | - |
| Latency (avg) | 88s | - | - |
| 垃圾文件 (build/) | **0** | 有 | **消除** |

### 4.3 遗漏分析（12/50 partial hit）

**规律**: 遗漏的**全是调用方文件**。

| 符号 | 命中 | 遗漏 |
|------|------|------|
| `ggml_sycl_set_device` | `common.hpp` | `common.cpp`, `cpy.cpp`, `element_wise.cpp` |
| `trim_whitespace` | `helpers.cpp/.h` | `chat-diff-analyzer.cpp` |
| `llama_model_chat_template` | `chat.cpp`, `llama-model.cpp` | `common.cpp` |

**根因**: `grep_symbol_files()` 已找到调用方文件（如 `cpy.cpp`），但 embedding retriever 只返回 top-10。调用方代码只是简单一句 `ggml_sycl_set_device(ctx.device)`，和问题的语义相似度低，排不进 top-10。

**待解决**: 需要一种机制确保定义文件和调用方文件都被纳入检索结果，而不仅仅依赖 embedding 排序。

---

## 五、实验操作规范与记录要求

> 本章节定义 benchmark 运行的标准操作流程，确保实验可复现、可移交。

### 5.1 环境前提

```bash
# 当前工作目录
REPO_ROOT=/data/users/zzy/RUC/Code_Graph
TARGET_REPO=/data/users/zzy/RUC/llama.cpp

# 必要依赖
pip install rank-bm25  # 已安装

# Neo4j（如需要图扩展）
# 当前配置：/tmp/neo4j-conf, auth disabled
```

### 5.2 Benchmark 运行命令

**Symbol Fast Path（当前主实验）**：
```bash
cd /data/users/zzy/RUC/Code_Graph
python3 scripts/run_benchmark_symbol_fastpath.py \
  --config symbol_fastpath \
  --workers 20 \
  --output results/benchmark_symbol_fastpath_$(date +%Y%m%d_%H%M%S).json
```

**Baseline（禁用 Symbol Fast Path，做 A/B 对比）**：
```bash
cd /data/users/zzy/RUC/Code_Graph
python3 scripts/run_benchmark_symbol_fastpath.py \
  --config baseline \
  --workers 20 \
  --output results/benchmark_baseline_$(date +%Y%m%d_%H%M%S).json
```

**只跑前 N 题调试**：
```bash
python3 scripts/run_benchmark_symbol_fastpath.py \
  --config symbol_fastpath \
  --workers 20 \
  --limit 5
```

**从指定 offset 开始跑**：
```bash
python3 scripts/run_benchmark_symbol_fastpath.py \
  --config symbol_fastpath \
  --workers 10 \
  --offset 40 \
  --limit 10
```

### 5.3 并行要求

- **默认 workers = 20**（用户明确要求，减少等待时间）
- 每个 worker 独立调用 LLM，互不干扰
- 注意：API 并发过高可能导致 rate limit，如出现大量 429 错误，适当降低 workers 到 10

### 5.4 轨迹记录规范

每次 benchmark 必须记录：

| 记录项 | 要求 | 保存位置 |
|--------|------|---------|
| **结果文件** | JSON 格式，含每题的 question/answer/retrieved_functions/steps/latency | `results/benchmark_{config}_{timestamp}.json` |
| **完整日志** | 保留所有 HTTP 请求、LLM 调用、检索过程 | 脚本 stdout/stderr，建议重定向到 `logs/benchmark_{config}_{timestamp}.log` |
| **执行轨迹** | 每题的 ReAct steps（action/query/retrieved） | 已包含在结果 JSON 的 `steps` 字段中 |
| **代码版本** | git commit hash 或 branch | 实验前执行 `git log --oneline -1` 并记录 |
| **环境信息** | Python 版本、关键依赖版本 | 实验前执行 `pip freeze \| grep -E "(openai|rank-bm25|neo4j)"` |

### 5.5 日志重定向建议

```bash
# 推荐做法：同时保存日志和看 tail
python3 scripts/run_benchmark_symbol_fastpath.py \
  --config symbol_fastpath \
  --workers 20 \
  --output results/benchmark_symbol_fastpath_$(date +%Y%m%d_%H%M%S).json \
  2>&1 | tee logs/benchmark_symbol_fastpath_$(date +%Y%m%d_%H%M%S).log
```

### 5.6 已有的实验记录

| 时间 | 配置 | 题数 | 结果文件 | 关键指标 | 备注 |
|------|------|------|---------|---------|------|
| 2026-06-05 20:23 | symbol_fastpath | 5 | `results/benchmark_symbol_fastpath_{ts}.json` (被覆盖) | - | 预测试 |
| 2026-06-05 20:28 | symbol_fastpath | 40 | `results/benchmark_symbol_fastpath_{ts}.json` (被覆盖) | avg 14.5 funcs, 5.1 steps | 主任务因 heartbeat 中断 |
| 2026-06-05 20:36 | symbol_fastpath | 10 | `results/benchmark_symbol_fastpath_{ts}.json` | - | offset 40-50 |
| **2026-06-05 20:45** | **symbol_fastpath** | **50** | **`results/benchmark_symbol_fastpath_20260605_203801.json`** | **91.2% coverage, 14.5 funcs, 5.4 steps** | **当前主要结果** |
| 2026-06-05 | baseline | 0 | - | - | 尚未跑 |

**注意**：早期实验因脚本 bug（`{ts}` 未替换）导致结果文件被覆盖，已修复。后续实验必须使用带时间戳的文件名。

### 5.7 分析命令

```bash
# 查看结果概览
cd /data/users/zzy/RUC/Code_Graph
python3 -c "
import json
with open('results/benchmark_symbol_fastpath_20260605_203801.json') as f:
    data = json.load(f)
print(f'Items: {len(data)}')
print(f'Errors: {sum(1 for d in data if d.get(\"error\"))}')
funcs = [len(d.get('retrieved_functions', [])) for d in data]
print(f'Avg funcs: {sum(funcs)/len(funcs):.1f}')
steps = [len(d.get('steps', [])) for d in data]
print(f'Avg steps: {sum(steps)/len(steps):.1f}')
"

# 对比 gold evidence 覆盖率
python3 scripts/eval_scope_routing.py --with-noise --workers 20
```

### 5.8 移交 checklist

当其他人接手时，必须提供：

- [ ] 当前分支：`feat/navigation-architecture`
- [ ] 最新的 benchmark 结果文件路径
- [ ] 运行 benchmark 的命令（见 5.2）
- [ ] 当前未解决的 bug/issue 列表（见第七章）
- [ ] 核心设计文档：`docs/research/navigation_pipeline_implementation_plan.md`
- [ ] 当前状态总结：本文件
- [ ] 访问权限：Neo4j（`/tmp/neo4j-conf`，auth disabled）、DeepSeek API key（已配置在 `.env`）

---

## 六、核心研究洞察

### 5.1 文档路由对 symbol-level 审计问题无效

实验证明了：llama.cpp 的文档（README + docs）**几乎不引用具体实现文件**。文档是用户指南，不是 API 文档。

> 文档 routing 的失败不是算法问题，是**文档性质问题**。

### 5.2 Query Understanding 比 Repository Cognition 更重要

正确的第一层认知不是：
```
Question → Document → File
```

而是：
```
Question → Extract Symbol → Direct Jump to Definition
```

### 5.3 符号名是最强的路由信号

对于 symbol-centric 问题，函数名本身就是精确坐标：
- 不需要 embedding
- 不需要文档解析
- 不需要 BM25
- 一行 `rg` 命令即可 100% 定位

### 5.4 缩小搜索空间的故事成立，但路径不是文档

核心主张"缩小搜索空间"是正确的，但实现路径应该是：
- **Symbol → Grep → File List**（对 symbol-centric 问题）
- **Component → Scope Routing**（对 component-centric 问题，待验证）
- **Architecture → Document Navigation**（对架构问题，待验证）

---

## 七、待解决问题

### 6.1 P0: 调用方文件遗漏

**问题**: Symbol Fast Path 找到定义文件，但调用方文件在 embedding top-10 中被挤出。  
**方案 A（推荐）**: 在 Symbol Fast Path 中，不依赖 embedding 排序，直接读取所有 `file_filter` 中包含 symbol 的函数。  
**预期效果**: 覆盖率从 91.2% → ~100%。

### 6.2 P1: Baseline A/B 对比

**问题**: 缺少禁用 Symbol Fast Path 的全局搜索 baseline 数据。  
**方案**: 跑 `enable_symbol_fastpath=False` 的 50 题 benchmark。  
**预期耗时**: 7-10 分钟。  
**预期对比指标**: 覆盖率、噪音文件数、token 消耗、latency。

### 6.3 P2: AST Parser Bug（数据质量）

**问题**: C++ class member function 调用 free function 未被记录到 Neo4j。  
**影响**: 12/22 真实遗漏由该 bug 导致。  
**方案**: 修复 AST parser 的 call graph 构建逻辑。  
**优先级**: 高（影响数据质量），但不阻塞当前导航架构实验。

### 6.4 P3: Query Type 分类器验证

**问题**: 当前 benchmark 100% 是 symbol-centric，无法验证 component/architecture 路径。  
**方案**: 需要构建或收集包含 component-centric 和 architecture-centric 问题的 benchmark。  
**优先级**: 中（当前系统对现有 benchmark 已足够）。

### 6.5 P4: 非 symbol-centric 问题的 fallback

**问题**: 当前系统对无函数名的问题（如 "backend scheduler 怎么工作？"）会 fallback 到全局搜索。  
**方案**: 实现 component-centric 的 Scope Routing（文档 + 目录结构）。  
**优先级**: 中（取决于实际使用场景）。

---

## 八、下一步行动清单

| 优先级 | 任务 | 预期产出 | 预估时间 |
|--------|------|---------|---------|
| **P0** | 修复调用方遗漏（方案 A） | 覆盖率 91% → 100% | 1-2 小时 |
| **P1** | 跑 baseline A/B 对比 | 量化 Symbol Fast Path 收益 | 10 分钟 |
| **P2** | 修复 AST Parser Bug | 数据质量提升 | 4-8 小时 |
| **P3** | 收集多样化 benchmark | 验证 component/architecture 路径 | 待定 |
| **P4** | 实现 component-centric routing | 覆盖无函数名问题 | 2-4 小时 |

---

## 九、相关文档索引

| 文档 | 路径 | 说明 |
|------|------|------|
| 0604 Benchmark 报告 | `docs/research/0604/benchmark_report.md` | 50 题 benchmark 详细结果 |
| 0604 根因分析 | `docs/research/0604/root_cause_analysis.md` | AST parser bug + selector 问题 |
| 0605 文档路由实验报告 | `docs/research/0605/scope_routing_experiment_report.md` | Document Routing 失败分析 |
| 0605 导航架构设计 | `docs/research/navigation_pipeline_implementation_plan.md` | 四层认知图设计（Document→Topic→Architecture→Code） |
| 0606 现状总结 | `docs/research/0606/research_status_summary.md` | 本文件 |

---

## 十、关键代码文件索引

| 文件 | 说明 |
|------|------|
| `src/qa/query_analyzer.py` | LLM Query Understanding |
| `src/qa/symbol_search.py` | Grep-based symbol file lookup |
| `src/qa/scope_planner.py` | Document-based scope routing |
| `src/qa/document_index.py` | Markdown section splitting + BM25 |
| `src/qa/pipeline.py` | 主 pipeline（集成 QueryAnalyzer + Symbol Fast Path） |
| `prompts/query_analysis.txt` | Query Analysis LLM prompt |
| `scripts/eval_scope_routing.py` | Scope Routing 评估脚本 |
| `scripts/run_benchmark_symbol_fastpath.py` | Benchmark runner |
| `results/benchmark_symbol_fastpath_20260605_203801.json` | 最新 50 题结果 |
