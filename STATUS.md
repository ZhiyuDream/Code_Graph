# Code_Graph 当前状态

> 更新时间: 2026-05-29

## 准确率

| Benchmark | 模型 | 准确率 | 平均分 |
|-----------|------|--------|--------|
| llamacpp_benchmark_v2 (50题) | DeepSeek V4 Pro | 46%-54% | 0.70-0.73 |

准确率波动范围 28%-54%，主要受 LLM 输出随机性影响。

## 建图质量

### 指标

| 指标 | 数值 |
|------|------|
| 文件数 | 806 |
| 函数数 | 18,143 |
| CALLS 边 | 75,297 |
| 建图耗时 | 37min (outgoing 22min + incoming 15min) |
| 证据覆盖率 | **94.8%** (163/172) |

### 证据覆盖率详情

- 函数存在类证据（定义、代码片段、参数流等）: **100%** (49/49)
- 调用关系类证据（call_site、usage_site 等）: **92.7%** (114/123)

### 未覆盖的 9 条证据

| QA ID | 证据 | 原因 | 类型 |
|-------|------|------|------|
| audit_003/013/019 | E4 `src/llama.cpp:184` -> `llama_model_default_params` | 行号已过期，184行实际是 `n_sources_defined++` | 数据问题 |
| audit_046 | E9 `common/common.h:66` -> `cpu_get_num_physical_cores` | 函数名缺 `common_` 前缀 | 数据问题 |
| audit_010 | E1 `ggml-backend-reg.cpp` -> `ggml_backend_cann_reg` | `#ifdef GGML_USE_CANN` 条件编译，CANN=OFF | clangd 限制 |
| audit_028 | E2 `common/arg.cpp` -> `common_list_cached_models` | lambda 内调用 | 可修复 |
| audit_033 | E3 `ggml-rpc.cpp` -> `ggml_backend_free` | 编译单元隔离 | 可修复 |
| audit_040/041 | E2/E3 `ggml-sycl.cpp` -> `ggml_sycl_count_equal/op_set` | switch-case 内 + 宏展开，全局 0 caller | clangd 限制 |

## 已修复的问题

### QA Pipeline (6个)

1. `source` 字段缺失 → embedding 结果被 `build_context` 过滤
2. 代码截断 → LLM 看不到完整函数实现
3. ReAct 只支持 2 种 action → 扩展到全部 7 种
4. `.h` 声明找不到 `.cpp` 实现 → 搜索同目录所有 `.cpp`
5. `_extract_function_by_name` 匹配调用站点 → 优先匹配 `::funcName(` 定义
6. `build_context` 不识别新 source 类型 + 重复条目

### 建图 Pipeline (3个)

1. 目录扫描只收集头文件 → 收集所有 `.cpp` (+233 文件)
2. `prepareCallHierarchy` 光标在行首 → 定位到函数名 (CALLS 4x)
3. `outgoingCalls` 不穿透 lambda → 用 `incomingCalls` 补充 (+3,658 边)

### 基础设施

- 项目结构重组：`tools/core/` → `src/core/`，`tools/search/` → `src/search/`
- 组件级 pytest 测试覆盖（answer_generator, code_reader, semantic_search）
- 实验日志记录每步召回的函数详情

## 待解决的问题

### 高优先级

1. **LLM 输出随机性**：同一配置不同跑结果差异 10%+，需要多次采样或 temperature=0
2. **生成质量问题**：LLM 分析详细但不给明确结论，judge 常说"缺乏明确结论"
3. **lambda 内调用仍有遗漏**：`incomingCalls` 补充了大部分，但 2 条证据仍未覆盖（可用 `references` API 进一步补充）

### 中优先级

4. **`references` API 替代 `outgoingCalls`**：`textDocument/references` 能找到所有引用点（包括 lambda 内），但需要额外处理来区分调用和其他引用
5. **建图性能优化**：37min 建图时间较长，incomingCalls 阶段可以只对 outgoingCalls 为 0 的函数执行
6. **Neo4j 图中仍有 47,052 条 ambiguous CALLS 边**：需要改进 call_resolver 的歧义消解策略

### 低优先级

7. **证据数据修正**：audit_003/013/019 的行号过期，audit_046 的函数名不完整
8. **条件编译处理**：`#ifdef` 保护的调用关系无法通过 clangd 捕获（需要多配置编译）
9. **RAG index 更新**：`data/classic_rag_index.json` 需要随图更新重新生成

## 项目结构

```
config.py               # 配置加载（.env）
src/
  pipeline/             # Stage 1 建图（clangd LSP）
  core/                 # 基础设施：neo4j_client, llm_client, prompt_loader, answer_generator
  search/               # 检索：semantic_search, call_chain, grep_search_v2, code_reader
  qa/                   # QA agent (agent.py) 和 classic RAG
  workflow/             # 工作流发现（entry_candidates）
scripts/                # CLI 入口
experiments/            # 实验脚本和结果
prompts/                # LLM prompt 模板
datasets/               # QA benchmark 数据
tests/                  # 组件测试
data/                   # RAG 索引（gitignored）
```
