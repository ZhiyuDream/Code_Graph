# Hard Benchmark 搜到未引根因分析

**结果文件**: `benchmark_hard_20260607_200601.json`
**分析口径**: 排除 `.h` / `.hpp` 头文件

## 总览

- 搜到未引的题目: **32 / 50**
- 这些题总共检索到的 gold 文件: 61
- 其中被漏引的 gold 文件: 42
- 漏引率: 68.9%

### 按维度分布

| 维度 | 题数 | 检索到的gold | 漏引数 | 漏引率 |
|------|------|-------------|--------|--------|
| 配置/参数传播一致性 | 9 | 21 | 12 | 57% |
| 共享 helper 复用影响 | 8 | 12 | 12 | 100% |
| 调用方契约兼容性 | 7 | 16 | 10 | 62% |
| 失败返回语义 | 2 | 3 | 2 | 67% |
| 状态一致性 | 2 | 3 | 2 | 67% |
| 并发/异步安全 | 2 | 3 | 2 | 67% |
| 初始化/外部 API 契约 | 1 | 1 | 1 | 100% |
| 资源生命周期/清理边界 | 1 | 2 | 1 | 50% |

## 逐题根因分析

### posthoc_public_002 (调用方契约兼容性)
**问题**: AI 生成了聊天模板选择逻辑，我担心空模板、缺失模板和调用方 fallback 判断被混在一起。帮我看返回值语义是否和现有调用方的判断方式一致？
**Gold 文件**: `common/chat.cpp`, `common/common.cpp`, `src/llama-model.cpp`
**检索到的文件**: `common/chat-auto-parser-helpers.cpp`, `common/chat-auto-parser-helpers.h`, `common/chat-auto-parser.h`, `common/chat-diff-analyzer.cpp`, `common/chat.cpp`, `common/chat.h`, `common/common.cpp`, `src/llama-chat.cpp`, `src/llama-model.cpp`, `tests/test-chat-auto-parser.cpp`, `tests/test-chat-template.cpp`, `tests/test-quant-type-selection.cpp`, `tools/cli/cli.cpp`, `tools/parser/template-analysis.cpp`, `vendor/cpp-httplib/httplib.cpp`
**引用的文件**: `common/chat.cpp`, `src/llama-model.cpp`, `src/llama.cpp`
**漏引的文件**: `common/common.cpp`
**噪声文件**: `common/chat-auto-parser-helpers.cpp`, `common/chat-auto-parser-helpers.h`, `common/chat-auto-parser.h`, `common/chat-diff-analyzer.cpp`, `common/chat.h`, `src/llama-chat.cpp`, `tests/test-chat-auto-parser.cpp`, `tests/test-chat-template.cpp`, `tests/test-quant-type-selection.cpp`, `tools/cli/cli.cpp`, `tools/parser/template-analysis.cpp`, `vendor/cpp-httplib/httplib.cpp` (12/15 = 80%)

**漏引根因分析**:
- `common/common.cpp`: 答案正文**完全没有提及**
  → 原因推断: 同目录 `common/` 下有多个文件，LLM 只选了其一
  → 原因推断: `common.cpp` 是通用基础设施，LLM 可能认为它'太泛'而不具针对性

**错误引用的噪声文件** (1 个):
- `src/llama.cpp`
  → 上下文: ...

### posthoc_public_003 (调用方契约兼容性)
**问题**: AI 调整了模型加载默认参数初始化，我担心不同入口拿到的初值来源不一致。帮我看 common 加载、量化和底层适配路径是否仍从同一套默认参数出发？
**Gold 文件**: `common/common.cpp`, `src/llama-model.cpp`, `src/llama-quant.cpp`, `src/llama.cpp`
**检索到的文件**: `common/common.cpp`, `common/peg-parser.h`, `examples/convert-llama2c-to-ggml/convert-llama2c-to-ggml.cpp`, `examples/gguf/gguf.cpp`, `examples/retrieval/retrieval.cpp`, `src/llama-model.cpp`, `tests/test-backend-sampler.cpp`, `tests/test-quant-type-selection.cpp`, `tools/imatrix/imatrix.cpp`, `tools/server/server-context.cpp`, `tools/server/server-context.h`
**引用的文件**: ``
**漏引的文件**: `common/common.cpp`, `src/llama-model.cpp`
**噪声文件**: `common/peg-parser.h`, `examples/convert-llama2c-to-ggml/convert-llama2c-to-ggml.cpp`, `examples/gguf/gguf.cpp`, `examples/retrieval/retrieval.cpp`, `tests/test-backend-sampler.cpp`, `tests/test-quant-type-selection.cpp`, `tools/imatrix/imatrix.cpp`, `tools/server/server-context.cpp`, `tools/server/server-context.h` (9/11 = 82%)

**漏引根因分析**:
- `common/common.cpp`: 答案正文**提到了文件名**（无反引号），但未被纳入引用清单
  → 原因推断: `common.cpp` 是通用基础设施，LLM 可能认为它'太泛'而不具针对性
- `src/llama-model.cpp`: 答案正文**完全没有提及**
  → 原因推断: llama-model.cpp 是大型核心文件，LLM 可能回避深入分析

**答案结构**:
- ### 1. 模型加载的默认参数起点
- ### 2. 上下文/适配的默认参数起点
- ### 3. 业务代码的入口：以 retrieval 为例
- ### 4. 量化路径的一致性
- ### 结论
- ### 参考文件清单

### posthoc_public_004 (调用方契约兼容性)
**问题**: AI 抽了字符串首尾空白裁剪逻辑，我担心调用点对空字符串、全空白字符串和无副作用返回值的假设不一致。帮我看这些调用方是否仍能按相同契约使用裁剪结果？
**Gold 文件**: `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`
**检索到的文件**: `common/chat-auto-parser-helpers.cpp`, `common/chat-auto-parser-helpers.h`, `common/chat-auto-parser.h`, `common/chat-diff-analyzer.cpp`, `common/jinja/string.h`, `tests/test-chat-auto-parser.cpp`
**引用的文件**: `common/chat-diff-analyzer.cpp`
**漏引的文件**: `common/chat-auto-parser-helpers.cpp`
**噪声文件**: `common/chat-auto-parser-helpers.h`, `common/chat-auto-parser.h`, `common/jinja/string.h`, `tests/test-chat-auto-parser.cpp` (4/6 = 67%)

**漏引根因分析**:
- `common/chat-auto-parser-helpers.cpp`: 答案正文**提到了文件名**（无反引号），但未被纳入引用清单
  → 原因推断: 同目录 `common/` 下有多个文件，LLM 只选了其一
  → 原因推断: helper 实现文件，LLM 可能聚焦在调用方而非实现方

**答案结构**:
- ### 1. `compare_thinking_enabled`  
- ### 2. `analyze_content` 构造函数  
- ### 3. 其他潜在调用点  
- ### 结论

### posthoc_public_008 (调用方契约兼容性)
**问题**: AI 改了根据左右模板文本的公共前缀截取待分析片段的逻辑，我担心返回边界、参数约定和无副作用假设会影响调用方后续解析。帮我看这些调用方对返回值、输入参数和副作用的理解是否符合当前实现？
**Gold 文件**: `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`
**检索到的文件**: `common/chat-auto-parser-helpers.cpp`, `common/chat-auto-parser.h`, `common/chat-diff-analyzer.cpp`
**引用的文件**: `common/chat-auto-parser-helpers.cpp`
**漏引的文件**: `common/chat-diff-analyzer.cpp`
**噪声文件**: `common/chat-auto-parser.h` (1/3 = 33%)

**漏引根因分析**:
- `common/chat-diff-analyzer.cpp`: 答案正文**提到了文件名**（无反引号），但未被纳入引用清单
  → 原因推断: 同目录 `common/` 下有多个文件，LLM 只选了其一
  → 原因推断: 调用方文件，LLM 可能更关注实现文件

**答案结构**:
- ### 1. `compare_variants` 的契约
- ### 2. 调用方 `compare_reasoning_presence`
- ### 3. 调用方 `compare_thinking_enabled`
- ### 4. 调用方 `compare_reasoning_scope`
- ### 5. 调用方 `analyze_content`（间接调用 `compare_variants`）
- ### 总结

### posthoc_public_010 (调用方契约兼容性)
**问题**: AI 生成了一个后端注册入口，我担心静态初始化、互斥保护和重复调用时的注册状态不一致。帮我看这个注册流程是否符合现有后端获取路径的使用假设？
**Gold 文件**: `ggml/src/ggml-backend-reg.cpp`, `ggml/src/ggml-cann/ggml-cann.cpp`
**检索到的文件**: `ggml/include/ggml-cann.h`, `ggml/src/ggml-backend-reg.cpp`, `ggml/src/ggml-cann/ggml-cann.cpp`, `ggml/src/ggml-hexagon/ggml-hexagon.cpp`, `ggml/src/ggml-openvino/ggml-openvino.cpp`, `ggml/src/ggml-rpc/ggml-rpc.cpp`, `ggml/src/ggml-virtgpu/backend/backend-dispatched.cpp`, `ggml/src/ggml-virtgpu/ggml-backend-reg.cpp`, `ggml/src/ggml-webgpu/ggml-webgpu.cpp`, `ggml/src/ggml-zdnn/ggml-zdnn.cpp`, `src/llama-quant.cpp`, `tests/test-quantize-stats.cpp`
**引用的文件**: `ggml/src/ggml-cann/ggml-cann.cpp`
**漏引的文件**: `ggml/src/ggml-backend-reg.cpp`
**噪声文件**: `ggml/include/ggml-cann.h`, `ggml/src/ggml-hexagon/ggml-hexagon.cpp`, `ggml/src/ggml-openvino/ggml-openvino.cpp`, `ggml/src/ggml-rpc/ggml-rpc.cpp`, `ggml/src/ggml-virtgpu/backend/backend-dispatched.cpp`, `ggml/src/ggml-virtgpu/ggml-backend-reg.cpp`, `ggml/src/ggml-webgpu/ggml-webgpu.cpp`, `ggml/src/ggml-zdnn/ggml-zdnn.cpp`, `src/llama-quant.cpp`, `tests/test-quantize-stats.cpp` (10/12 = 83%)

**漏引根因分析**:
- `ggml/src/ggml-backend-reg.cpp`: 答案正文**提到了文件名**（无反引号），但未被纳入引用清单
  → 原因推断: ggml 目录文件在答案中被忽略

**答案结构**:
- ### 1. 正确模式：以 `ggml_backend_cann_reg` 为例
- ### 2. 潜在风险模式：`ggml_backend_zdnn_reg`
- ### 3. 其他特殊处理需要关注的细节
- #### a. 显式泄漏上下文（WebGPU）
- #### b. 初始化失败时返回 NULL（Hexagon）
- #### c. `reg` 指针的稳定性
- ... (2 more)

### posthoc_public_011 (配置/参数传播一致性)
**问题**: AI 改了 sampler 初始化时的配置搬运逻辑，我担心采样参数、性能开关和 grammar 相关字段在进入内部参数前被漏传或改义。帮我顺一下这些值来源和最终使用是否一致？
**Gold 文件**: `common/common.cpp`, `common/sampling.cpp`, `common/speculative.cpp`
**检索到的文件**: `common/common.cpp`, `common/llguidance.cpp`, `common/log.cpp`, `common/sampling.cpp`, `common/sampling.h`, `examples/speculative/speculative.cpp`, `src/llama-sampler.cpp`, `tests/test-backend-sampler.cpp`, `tests/test-grammar-llguidance.cpp`, `tests/test-sampling.cpp`, `tools/completion/completion.cpp`
**引用的文件**: ``
**漏引的文件**: `common/common.cpp`, `common/sampling.cpp`
**噪声文件**: `common/llguidance.cpp`, `common/log.cpp`, `common/sampling.h`, `examples/speculative/speculative.cpp`, `src/llama-sampler.cpp`, `tests/test-backend-sampler.cpp`, `tests/test-grammar-llguidance.cpp`, `tests/test-sampling.cpp`, `tools/completion/completion.cpp` (9/11 = 82%)

**漏引根因分析**:
- `common/common.cpp`: 答案正文**完全没有提及**
  → 原因推断: `common.cpp` 是通用基础设施，LLM 可能认为它'太泛'而不具针对性
- `common/sampling.cpp`: 答案正文**提到了文件名**（无反引号），但未被纳入引用清单
  → 原因推断: sampling.cpp 被忽略，LLM 可能选择了其他更'显眼'的文件

### posthoc_public_015 (配置/参数传播一致性)
**问题**: AI 改了模型下载 endpoint 的默认值和环境变量处理，我担心不同下载入口拿到的 endpoint 不一致。帮我看默认来源、环境变量覆盖和尾部斜杠规范化是否被各调用路径一致使用？
**Gold 文件**: `common/arg.cpp`, `common/common.cpp`, `common/hf-cache.cpp`
**检索到的文件**: `common/arg.cpp`, `common/common.cpp`, `common/common.h`, `common/download.cpp`, `common/download.h`, `common/hf-cache.cpp`, `common/hf-cache.h`, `common/peg-parser.h`, `tests/gguf-model-data.cpp`, `tools/llama-bench/llama-bench.cpp`, `tools/rpc/rpc-server.cpp`, `tools/server/server-common.cpp`, `tools/server/server-models.cpp`, `vendor/miniaudio/miniaudio.h`
**引用的文件**: `common/common.cpp`
**漏引的文件**: `common/arg.cpp`, `common/hf-cache.cpp`
**噪声文件**: `common/common.h`, `common/download.cpp`, `common/download.h`, `common/hf-cache.h`, `common/peg-parser.h`, `tests/gguf-model-data.cpp`, `tools/llama-bench/llama-bench.cpp`, `tools/rpc/rpc-server.cpp`, `tools/server/server-common.cpp`, `tools/server/server-models.cpp`, `vendor/miniaudio/miniaudio.h` (11/14 = 79%)

**漏引根因分析**:
- `common/arg.cpp`: 答案正文**提到了文件名**（无反引号），但未被纳入引用清单
  → 原因推断: 同目录 `common/` 下有多个文件，LLM 只选了其一
  → 原因推断: `arg.cpp` 是参数解析入口，问题若未直接问'参数解析'，LLM 可能觉得不需要引用
- `common/hf-cache.cpp`: 答案正文**提到了文件名**（无反引号），但未被纳入引用清单
  → 原因推断: 同目录 `common/` 下有多个文件，LLM 只选了其一
  → 原因推断: hf-cache.cpp 是辅助文件，问题未聚焦缓存逻辑

**答案结构**:
- ### 1. `common_get_model_endpoint()` 的规范逻辑
- ### 2. 已知直接调用 `common_get_model_endpoint()` 的路径（行为一致）
- #### 2.1 远程预设下载（preset.ini）
- #### 2.2 获取仓库最新 commit
- ### 3. 潜在不一致的调用路径（需进一步确认）
- #### 3.1 Hugging Face 模型下载任务生成（`get_hf_plan`）
- ... (3 more)

### posthoc_public_016 (配置/参数传播一致性)
**问题**: AI 改了缓存模型列表读取逻辑，我担心参数解析和 preset 展示路径对返回列表的结构或排序假设不一致。帮我看这些入口是否按同一套缓存模型信息使用结果？
**Gold 文件**: `common/arg.cpp`, `common/download.cpp`, `common/preset.cpp`
**检索到的文件**: `common/arg.cpp`, `common/download.cpp`, `common/download.h`, `common/preset.cpp`, `common/preset.h`, `src/llama-kv-cache.cpp`, `src/llama-model.cpp`, `tests/gguf-model-data.cpp`, `tests/test-llama-archs.cpp`, `tools/mtmd/clip.cpp`, `tools/server/server-models.cpp`, `vendor/cpp-httplib/httplib.cpp`, `vendor/miniaudio/miniaudio.h`, `vendor/stb/stb_image.h`
**引用的文件**: `common/download.cpp`, `common/preset.cpp`
**漏引的文件**: `common/arg.cpp`
**噪声文件**: `common/download.h`, `common/preset.h`, `src/llama-kv-cache.cpp`, `src/llama-model.cpp`, `tests/gguf-model-data.cpp`, `tests/test-llama-archs.cpp`, `tools/mtmd/clip.cpp`, `tools/server/server-models.cpp`, `vendor/cpp-httplib/httplib.cpp`, `vendor/miniaudio/miniaudio.h`, `vendor/stb/stb_image.h` (11/14 = 79%)

**漏引根因分析**:
- `common/arg.cpp`: 答案正文**完全没有提及**
  → 原因推断: 同目录 `common/` 下有多个文件，LLM 只选了其一
  → 原因推断: `arg.cpp` 是参数解析入口，问题若未直接问'参数解析'，LLM 可能觉得不需要引用

**答案结构**:
- ### 1. 缓存模型列表的唯一产生点
- ### 2. 直接消费该函数的路径（`load_from_cache`）
- ### 3. 间接消费路径（server 模型加载）
- ### 4. 关于“结构假设”的一致性
- ### 结论
- ### 参考文件清单

### posthoc_public_017 (配置/参数传播一致性)
**问题**: AI 改了命令行参数映射逻辑，我担心解析失败、别名处理和输出 map 的填充方式会影响 preset 调用方。帮我看返回值和异常处理是否符合调用方契约？
**Gold 文件**: `common/arg.cpp`, `common/preset.cpp`
**检索到的文件**: `common/arg.cpp`, `common/arg.h`, `common/chat-auto-parser.h`, `common/chat-diff-analyzer.cpp`, `common/preset.cpp`, `tests/test-arg-parser.cpp`, `tests/test-backend-sampler.cpp`, `tools/llama-bench/llama-bench.cpp`, `tools/parser/debug-template-parser.cpp`, `tools/server/server-tools.cpp`, `vendor/cpp-httplib/httplib.cpp`, `vendor/miniaudio/miniaudio.h`
**引用的文件**: `common/arg.cpp`
**漏引的文件**: `common/preset.cpp`
**噪声文件**: `common/arg.h`, `common/chat-auto-parser.h`, `common/chat-diff-analyzer.cpp`, `tests/test-arg-parser.cpp`, `tests/test-backend-sampler.cpp`, `tools/llama-bench/llama-bench.cpp`, `tools/parser/debug-template-parser.cpp`, `tools/server/server-tools.cpp`, `vendor/cpp-httplib/httplib.cpp`, `vendor/miniaudio/miniaudio.h` (10/12 = 83%)

**漏引根因分析**:
- `common/preset.cpp`: 答案正文**完全没有提及**
  → 原因推断: 同目录 `common/` 下有多个文件，LLM 只选了其一
  → 原因推断: `preset.cpp` 被忽略——LLM 可能认为 download.cpp 已足够覆盖配置逻辑

**答案结构**:
- ## 1. 返回值契约：`common_params_parse_ex` 符合调用方预期  
- ## 2. 异常处理：清晰且不会影响 preset 流程  
- ## 3. 别名处理：映射表构建方式保证了别名统一解析  
- ## 4. 输出 map 填充方式对 preset 调用方的影响  
- ### 参考文件清单

### posthoc_public_018 (配置/参数传播一致性)
**问题**: AI 改了模型下载 endpoint 的规范化逻辑，我担心环境变量、默认 endpoint 和尾部斜杠处理在不同下载路径里不一致。帮我顺一下这些入口是否仍共享同一结果？
**Gold 文件**: `common/arg.cpp`, `common/common.cpp`, `common/hf-cache.cpp`
**检索到的文件**: `common/arg.cpp`, `common/common.cpp`, `common/common.h`, `common/download.cpp`, `common/download.h`, `common/hf-cache.cpp`, `common/peg-parser.h`, `tools/llama-bench/llama-bench.cpp`, `tools/server/server.cpp`
**引用的文件**: `common/common.cpp`, `common/hf-cache.cpp`
**漏引的文件**: `common/arg.cpp`
**噪声文件**: `common/common.h`, `common/download.cpp`, `common/download.h`, `common/peg-parser.h`, `tools/llama-bench/llama-bench.cpp`, `tools/server/server.cpp` (6/9 = 67%)

**漏引根因分析**:
- `common/arg.cpp`: 答案正文**提到了文件名**（无反引号），但未被纳入引用清单
  → 原因推断: 同目录 `common/` 下有多个文件，LLM 只选了其一
  → 原因推断: `arg.cpp` 是参数解析入口，问题若未直接问'参数解析'，LLM 可能觉得不需要引用

### posthoc_public_019 (配置/参数传播一致性)
**问题**: AI 改了模型加载默认参数初始化，我担心 common 加载、量化和底层适配入口拿到的初值来源分叉。帮我确认这些路径是否仍从同一套默认参数开始？
**Gold 文件**: `common/common.cpp`, `src/llama-model.cpp`, `src/llama-quant.cpp`, `src/llama.cpp`
**检索到的文件**: `common/common.cpp`, `common/debug.cpp`, `common/download.h`, `common/ngram-cache.cpp`, `src/llama-model.cpp`, `src/llama-quant.cpp`, `tests/export-graph-ops.cpp`, `tests/test-opt.cpp`, `tests/test-quant-type-selection.cpp`, `tools/mtmd/clip.cpp`, `tools/server/server-context.cpp`, `tools/server/server-context.h`
**引用的文件**: `common/common.cpp`
**漏引的文件**: `src/llama-model.cpp`, `src/llama-quant.cpp`
**噪声文件**: `common/debug.cpp`, `common/download.h`, `common/ngram-cache.cpp`, `tests/export-graph-ops.cpp`, `tests/test-opt.cpp`, `tests/test-quant-type-selection.cpp`, `tools/mtmd/clip.cpp`, `tools/server/server-context.cpp`, `tools/server/server-context.h` (9/12 = 75%)

**漏引根因分析**:
- `src/llama-model.cpp`: 答案正文**完全没有提及**
  → 原因推断: llama-model.cpp 是大型核心文件，LLM 可能回避深入分析
- `src/llama-quant.cpp`: 答案正文**完全没有提及**
  → 原因推断: quant.cpp 未在答案中直接分析

**答案结构**:
- ### 1. common 加载入口（`common_init_from_params` 和 `common_init_result` 构造函数）
- ### 2. 底层适配入口（`llama_model_load_from_file` / `llama_init_from_model`）
- ### 3. 量化工具路径（信息有限，但推断一致）
- ### 总结
- ### 参考文件清单

### posthoc_public_020 (配置/参数传播一致性)
**问题**: AI 改了 sampler 名称解析和顺序处理，我担心命令行参数和 metadata fallback 会解析出不同的 sampler 列表或顺序。帮我看两个入口是否仍走同一套别名和顺序规则？
**Gold 文件**: `common/arg.cpp`, `common/common.cpp`, `common/sampling.cpp`
**检索到的文件**: `common/arg.h`, `common/common.cpp`, `common/common.h`, `common/llguidance.cpp`, `common/log.cpp`, `common/sampling.cpp`, `examples/simple/simple.cpp`, `include/llama.h`, `src/llama-context.cpp`, `src/llama-context.h`, `src/llama-grammar.h`, `src/llama-graph.h`, `src/llama-sampler.cpp`, `tests/test-backend-sampler.cpp`, `tests/test-grammar-llguidance.cpp`, `tests/test-sampling.cpp`, `tests/test-thread-safety.cpp`, `tools/mtmd/clip.cpp`, `tools/server/server-task.h`, `vendor/miniaudio/miniaudio.h`
**引用的文件**: `common/arg.cpp`, `common/llama-sampling.cpp`, `common/sampling.cpp`, `include/llama.h`, `src/llama-sampling.cpp`, `tools/mtmd/clip.cpp`, `vendor/miniaudio/miniaudio.h`
**漏引的文件**: `common/common.cpp`
**噪声文件**: `common/arg.h`, `common/common.h`, `common/llguidance.cpp`, `common/log.cpp`, `examples/simple/simple.cpp`, `include/llama.h`, `src/llama-context.cpp`, `src/llama-context.h`, `src/llama-grammar.h`, `src/llama-graph.h`, `src/llama-sampler.cpp`, `tests/test-backend-sampler.cpp`, `tests/test-grammar-llguidance.cpp`, `tests/test-sampling.cpp`, `tests/test-thread-safety.cpp`, `tools/mtmd/clip.cpp`, `tools/server/server-task.h`, `vendor/miniaudio/miniaudio.h` (18/20 = 90%)

**漏引根因分析**:
- `common/common.cpp`: 答案正文**完全没有提及**
  → 原因推断: 同目录 `common/` 下有多个文件，LLM 只选了其一
  → 原因推断: `common.cpp` 是通用基础设施，LLM 可能认为它'太泛'而不具针对性

**错误引用的噪声文件** (5 个):
- `common/llama-sampling.cpp`
  → 上下文:    如果最近的修改没有单独为 metadata fallback 写一套新的解析逻辑，那么两者依然会共享同一套别名表（如 `sampler_name_to_enum()` 的映射）和顺序处理逻辑（如 `sampler_chain_from...
  → 原因推断: common/ 下其他文件，LLM 可能混淆了同目录文件
- `include/llama.h`
  → 上下文: ...
- `src/llama-sampling.cpp`
  → 上下文:    如果最近的修改没有单独为 metadata fallback 写一套新的解析逻辑，那么两者依然会共享同一套别名表（如 `sampler_name_to_enum()` 的映射）和顺序处理逻辑（如 `sampler_chain_from...
- `tools/mtmd/clip.cpp`
  → 上下文: **结论**：当前提供的代码片段不足以直接证实两个入口是否使用同一套规则，但根据项目惯例，它们大概率仍遵循同一套别名和顺序处理路径，除非最近的改动引入了新的代码分支。...
  → 原因推断: tools/ 文件被 LLM 当作'实际应用场景'引用
- `vendor/miniaudio/miniaudio.h`
  → 上下文: **参考文件清单**（本次直接引用的提供片段）：...
  → 原因推断: vendor/ 第三方库文件被幻觉引用

### posthoc_public_021 (共享 helper 复用影响)
**问题**: AI 抽了一个通用空白裁剪 helper，我担心多个调用点对返回新字符串、空输入和无副作用的假设不一致。帮我看这些复用点是否仍能按同一契约使用它？
**Gold 文件**: `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`
**检索到的文件**: `common/chat-diff-analyzer.cpp`, `common/chat-peg-parser.h`, `examples/retrieval/retrieval.cpp`, `ggml/src/ggml-webgpu/pre_wgsl.hpp`, `src/llama-chat.cpp`, `src/llama-graph.cpp`, `src/llama-kv-cache.cpp`, `src/llama-model.cpp`, `src/llama-vocab.cpp`, `tests/test-chat-auto-parser.cpp`, `vendor/cpp-httplib/httplib.cpp`, `vendor/miniaudio/miniaudio.h`, `vendor/nlohmann/json.hpp`
**引用的文件**: `vendor/cpp-httplib/httplib.cpp`
**漏引的文件**: `common/chat-diff-analyzer.cpp`
**噪声文件**: `common/chat-peg-parser.h`, `examples/retrieval/retrieval.cpp`, `ggml/src/ggml-webgpu/pre_wgsl.hpp`, `src/llama-chat.cpp`, `src/llama-graph.cpp`, `src/llama-kv-cache.cpp`, `src/llama-model.cpp`, `src/llama-vocab.cpp`, `tests/test-chat-auto-parser.cpp`, `vendor/cpp-httplib/httplib.cpp`, `vendor/miniaudio/miniaudio.h`, `vendor/nlohmann/json.hpp` (12/13 = 92%)

**漏引根因分析**:
- `common/chat-diff-analyzer.cpp`: 答案正文**完全没有提及**
  → 原因推断: 调用方文件，LLM 可能更关注实现文件

**错误引用的噪声文件** (1 个):
- `vendor/cpp-httplib/httplib.cpp`
  → 上下文: ...
  → 原因推断: vendor/ 第三方库文件被幻觉引用

**答案结构**:
- ## 1. 现存的内联空白裁剪代码及其契约
- ## 2. 潜在复用点的一致性分析
- ## 3. 结论
- ## 参考文件清单

### posthoc_public_022 (共享 helper 复用影响)
**问题**: AI 抽了只处理前导空白的字符串 helper，我担心调用点误以为它会做完整 trim。帮我看当前使用点是否都只依赖“去掉开头空白、返回新字符串”的契约？
**Gold 文件**: `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`
**检索到的文件**: `common/chat-auto-parser-helpers.cpp`, `common/chat-auto-parser-helpers.h`, `common/chat-peg-parser.cpp`, `src/llama-model.cpp`, `tools/perplexity/perplexity.cpp`, `vendor/cpp-httplib/httplib.cpp`
**引用的文件**: `vendor/cpp-httplib/httplib.cpp`
**漏引的文件**: `common/chat-auto-parser-helpers.cpp`
**噪声文件**: `common/chat-auto-parser-helpers.h`, `common/chat-peg-parser.cpp`, `src/llama-model.cpp`, `tools/perplexity/perplexity.cpp`, `vendor/cpp-httplib/httplib.cpp` (5/6 = 83%)

**漏引根因分析**:
- `common/chat-auto-parser-helpers.cpp`: 答案正文**完全没有提及**
  → 原因推断: helper 实现文件，LLM 可能聚焦在调用方而非实现方

**错误引用的噪声文件** (1 个):
- `vendor/cpp-httplib/httplib.cpp`
  → 上下文: 根据提供的代码片段（`vendor/cpp-httplib/httplib.cpp:4057-16238`），**未找到独立的、专门用于去除前导空白的字符串 helper 函数**。由于截断提示“还有 15 个函数未展示”，目标 helpe...
  → 原因推断: vendor/ 第三方库文件被幻觉引用

### posthoc_public_023 (共享 helper 复用影响)
**问题**: AI 抽了只处理尾部空白的字符串 helper，我担心空字符串和边界字符处理会影响多个复用点。帮我看这些调用点是否都按同一尾部裁剪契约使用返回值？
**Gold 文件**: `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`
**检索到的文件**: `common/chat-auto-parser-helpers.cpp`, `common/chat-auto-parser-helpers.h`, `common/chat-peg-parser.cpp`, `src/llama-model.cpp`, `tests/test-chat-auto-parser.cpp`, `tools/perplexity/perplexity.cpp`, `vendor/cpp-httplib/httplib.cpp`
**引用的文件**: `tools/perplexity/perplexity.cpp`, `vendor/cpp-httplib/httplib.cpp`
**漏引的文件**: `common/chat-auto-parser-helpers.cpp`
**噪声文件**: `common/chat-auto-parser-helpers.h`, `common/chat-peg-parser.cpp`, `src/llama-model.cpp`, `tests/test-chat-auto-parser.cpp`, `tools/perplexity/perplexity.cpp`, `vendor/cpp-httplib/httplib.cpp` (6/7 = 86%)

**漏引根因分析**:
- `common/chat-auto-parser-helpers.cpp`: 答案正文**完全没有提及**
  → 原因推断: helper 实现文件，LLM 可能聚焦在调用方而非实现方

**错误引用的噪声文件** (2 个):
- `tools/perplexity/perplexity.cpp`
  → 上下文: 根据您提供的检索信息，我没有找到您所说的“只处理尾部空白的字符串 helper”的定义或任何调用点。...
  → 原因推断: tools/ 文件被 LLM 当作'实际应用场景'引用
- `vendor/cpp-httplib/httplib.cpp`
  → 上下文: - `hellaswag_score` (`tools/perplexity/perplexity.cpp:743-1014`)...
  → 原因推断: vendor/ 第三方库文件被幻觉引用

**答案结构**:
- ### 参考文件清单

### posthoc_public_024 (共享 helper 复用影响)
**问题**: AI 抽了公共前缀截取 helper，我担心不同调用点对返回片段边界的理解不一样。帮我看它们是否都按相同的公共前缀语义使用结果？
**Gold 文件**: `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`
**检索到的文件**: `common/chat-auto-parser-helpers.cpp`, `common/chat-auto-parser.h`, `common/chat-peg-parser.h`, `common/log.cpp`, `common/peg-parser.cpp`, `examples/simple-chat/simple-chat.cpp`, `ggml/src/ggml-metal/ggml-metal-common.cpp`, `ggml/src/ggml-metal/ggml-metal-ops.cpp`, `ggml/src/gguf.cpp`, `src/llama-kv-cache.cpp`, `src/llama-model.cpp`, `src/llama-vocab.cpp`, `src/models/lfm2.cpp`, `tests/test-chat-auto-parser.cpp`, `tools/mtmd/debug/mtmd-debug.cpp`, `tools/mtmd/models/mobilenetv5.cpp`, `tools/server/server-common.cpp`
**引用的文件**: `examples/simple-chat/simple-chat.cpp`
**漏引的文件**: `common/chat-auto-parser-helpers.cpp`
**噪声文件**: `common/chat-auto-parser.h`, `common/chat-peg-parser.h`, `common/log.cpp`, `common/peg-parser.cpp`, `examples/simple-chat/simple-chat.cpp`, `ggml/src/ggml-metal/ggml-metal-common.cpp`, `ggml/src/ggml-metal/ggml-metal-ops.cpp`, `ggml/src/gguf.cpp`, `src/llama-kv-cache.cpp`, `src/llama-model.cpp`, `src/llama-vocab.cpp`, `src/models/lfm2.cpp`, `tests/test-chat-auto-parser.cpp`, `tools/mtmd/debug/mtmd-debug.cpp`, `tools/mtmd/models/mobilenetv5.cpp`, `tools/server/server-common.cpp` (16/17 = 94%)

**漏引根因分析**:
- `common/chat-auto-parser-helpers.cpp`: 答案正文**完全没有提及**
  → 原因推断: helper 实现文件，LLM 可能聚焦在调用方而非实现方

**错误引用的噪声文件** (1 个):
- `examples/simple-chat/simple-chat.cpp`
  → 上下文: 在 `examples/simple-chat/simple-chat.cpp` 的对话循环中，两次调用 `llama_chat_apply_template` 的返回值确实被用于实现“公共前缀截取”，且语义协调：...
  → 原因推断: examples/ 文件被 LLM 当作'调用方证据'引用

**答案结构**:
- ### 参考文件清单

### posthoc_public_025 (共享 helper 复用影响)
**问题**: AI 抽了 marker 文本分段 helper，我担心多个调用点对 marker 开闭、普通文本片段和返回列表结构的理解不一致。帮我看复用点是否仍按同一片段契约处理？
**Gold 文件**: `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`
**检索到的文件**: `common/chat-auto-parser-helpers.cpp`, `common/chat-auto-parser-helpers.h`, `common/chat-auto-parser.h`, `common/chat-diff-analyzer.cpp`, `common/peg-parser.cpp`, `tests/test-chat-auto-parser.cpp`, `tools/cli/cli.cpp`, `tools/mtmd/mtmd-cli.cpp`, `tools/mtmd/mtmd.cpp`, `tools/server/server-context.cpp`, `vendor/cpp-httplib/httplib.cpp`, `vendor/nlohmann/json.hpp`
**引用的文件**: `mtmd-cli.cpp`, `tools/mtmd/mtmd-cli.cpp`
**漏引的文件**: `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`
**噪声文件**: `common/chat-auto-parser-helpers.h`, `common/chat-auto-parser.h`, `common/peg-parser.cpp`, `tests/test-chat-auto-parser.cpp`, `tools/cli/cli.cpp`, `tools/mtmd/mtmd-cli.cpp`, `tools/mtmd/mtmd.cpp`, `tools/server/server-context.cpp`, `vendor/cpp-httplib/httplib.cpp`, `vendor/nlohmann/json.hpp` (10/12 = 83%)

**漏引根因分析**:
- `common/chat-auto-parser-helpers.cpp`: 答案正文**完全没有提及**
  → 原因推断: helper 实现文件，LLM 可能聚焦在调用方而非实现方
- `common/chat-diff-analyzer.cpp`: 答案正文**完全没有提及**
  → 原因推断: 调用方文件，LLM 可能更关注实现文件

**错误引用的噪声文件** (2 个):
- `mtmd-cli.cpp`
  → 上下文: ...
- `tools/mtmd/mtmd-cli.cpp`
  → 上下文: ...
  → 原因推断: tools/ 文件被 LLM 当作'实际应用场景'引用

**答案结构**:
- ### 1. `mtmd-cli.cpp` 中的调用点（`tools/mtmd/mtmd-cli.cpp:277-441`）
- ### 2. `server-context.cpp` 的 `load_model` 仅设置 marker 参数（`tools/server/server-context.cpp:743-968`）
- ### 3. 测试文件暗示存在分段 helper（`tests/test-chat-auto-parser.cpp:84-108`）
- ### 结论与风险点
- ### 参考文件清单

### posthoc_public_026 (共享 helper 复用影响)
**问题**: AI 抽了过滤空白片段的 helper，我担心调用点对“哪些片段会被保留”的假设不一致。帮我看它是否只按裁剪后的非空内容筛选，且不会改写原片段语义？
**Gold 文件**: `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`
**检索到的文件**: `common/chat-auto-parser-helpers.cpp`, `common/chat-auto-parser.h`, `common/chat-diff-analyzer.cpp`, `src/llama-model.cpp`, `src/models/lfm2.cpp`, `vendor/cpp-httplib/httplib.cpp`, `vendor/nlohmann/json.hpp`
**引用的文件**: `vendor/cpp-httplib/httplib.cpp`
**漏引的文件**: `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`
**噪声文件**: `common/chat-auto-parser.h`, `src/llama-model.cpp`, `src/models/lfm2.cpp`, `vendor/cpp-httplib/httplib.cpp`, `vendor/nlohmann/json.hpp` (5/7 = 71%)

**漏引根因分析**:
- `common/chat-auto-parser-helpers.cpp`: 答案正文**完全没有提及**
  → 原因推断: helper 实现文件，LLM 可能聚焦在调用方而非实现方
- `common/chat-diff-analyzer.cpp`: 答案正文**完全没有提及**
  → 原因推断: 调用方文件，LLM 可能更关注实现文件

**错误引用的噪声文件** (1 个):
- `vendor/cpp-httplib/httplib.cpp`
  → 上下文: ...
  → 原因推断: vendor/ 第三方库文件被幻觉引用

**答案结构**:
- ### 分析 `match_hostname` 对分割结果的假设
- ### 关于其他潜在调用点
- ### 建议

### posthoc_public_027 (共享 helper 复用影响)
**问题**: AI 抽了构建解析器的 helper，我担心 builder 生命周期、root 设置和返回 arena 的所有权会被调用方误解。帮我看复用点是否按同一初始化和返回契约使用它？
**Gold 文件**: `common/chat-auto-parser-generator.cpp`, `common/chat.cpp`
**检索到的文件**: `common/chat-auto-parser-generator.cpp`, `common/chat-auto-parser.h`, `common/chat-peg-parser.h`, `common/chat.cpp`, `common/chat.h`, `common/peg-parser.cpp`, `common/peg-parser.h`, `common/preset.cpp`, `tests/peg-parser/test-json-serialization.cpp`, `tests/test-chat.cpp`, `vendor/cpp-httplib/httplib.cpp`, `vendor/nlohmann/json.hpp`
**引用的文件**: `common/peg-parser.cpp`
**漏引的文件**: `common/chat-auto-parser-generator.cpp`, `common/chat.cpp`
**噪声文件**: `common/chat-auto-parser.h`, `common/chat-peg-parser.h`, `common/chat.h`, `common/peg-parser.cpp`, `common/peg-parser.h`, `common/preset.cpp`, `tests/peg-parser/test-json-serialization.cpp`, `tests/test-chat.cpp`, `vendor/cpp-httplib/httplib.cpp`, `vendor/nlohmann/json.hpp` (10/12 = 83%)

**漏引根因分析**:
- `common/chat-auto-parser-generator.cpp`: 答案正文**完全没有提及**
  → 原因推断: 同目录 `common/` 下有多个文件，LLM 只选了其一
- `common/chat.cpp`: 答案正文**提到了文件名**（无反引号），但未被纳入引用清单
  → 原因推断: 同目录 `common/` 下有多个文件，LLM 只选了其一
  → 原因推断: chat.cpp 是通用调用方，LLM 可能认为不够具体

**错误引用的噪声文件** (1 个):
- `common/peg-parser.cpp`
  → 上下文: ...
  → 原因推断: common/ 下其他文件，LLM 可能混淆了同目录文件

**答案结构**:
- ### 1. 工厂函数的实现（契约一致）
- ### 2. 调用点分析
- #### 2.1 `force_pure_content` 分支 – `common/chat.cpp:2214-2218`
- #### 2.2 `autoparser` 路径 – `common/chat.cpp:2233-2237`
- ### 3. 结论
- ### 参考文件清单

### posthoc_public_028 (共享 helper 复用影响)
**问题**: AI 抽了模板差异拆分 helper，我担心左右输入和公共片段的返回结构在多个复用点里被不同方式解释。帮我看这些调用点是否仍按同一 diff 结构使用？
**Gold 文件**: `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`, `common/chat.cpp`
**检索到的文件**: `common/chat-auto-parser-helpers.cpp`, `common/chat-auto-parser-helpers.h`, `common/chat-auto-parser.h`, `common/chat-diff-analyzer.cpp`, `ggml/src/ggml-metal/ggml-metal-ops.cpp`, `ggml/src/ggml-quants.c`, `src/llama-model.cpp`, `tests/test-chat-auto-parser.cpp`, `tools/parser/template-analysis.cpp`, `vendor/cpp-httplib/httplib.cpp`, `vendor/nlohmann/json.hpp`
**引用的文件**: `vendor/cpp-httplib/httplib.cpp`
**漏引的文件**: `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`
**噪声文件**: `common/chat-auto-parser-helpers.h`, `common/chat-auto-parser.h`, `ggml/src/ggml-metal/ggml-metal-ops.cpp`, `ggml/src/ggml-quants.c`, `src/llama-model.cpp`, `tests/test-chat-auto-parser.cpp`, `tools/parser/template-analysis.cpp`, `vendor/cpp-httplib/httplib.cpp`, `vendor/nlohmann/json.hpp` (9/11 = 82%)

**漏引根因分析**:
- `common/chat-auto-parser-helpers.cpp`: 答案正文**完全没有提及**
  → 原因推断: helper 实现文件，LLM 可能聚焦在调用方而非实现方
- `common/chat-diff-analyzer.cpp`: 答案正文**完全没有提及**
  → 原因推断: 调用方文件，LLM 可能更关注实现文件

**错误引用的噪声文件** (1 个):
- `vendor/cpp-httplib/httplib.cpp`
  → 上下文: 基于你所提供的全部检索信息，我未能定位到任何名为“模板差异拆分 helper”、或具有“左右输入 / 公共片段返回结构”这类特征的函数。目前展示的代码片段（`vendor/cpp-httplib/httplib.cpp` 中的 `subst...
  → 原因推断: vendor/ 第三方库文件被幻觉引用

### posthoc_public_029 (调用方契约兼容性)
**问题**: AI 改了把命令行参数转成配置 map 的实现，我担心解析错误和异常路径没有按调用方预期返回。帮我看参数解析实现和 preset 调用点对失败处理的理解是否一致？
**Gold 文件**: `common/arg.cpp`, `common/preset.cpp`
**检索到的文件**: `common/arg.cpp`, `common/preset.cpp`, `tools/llama-bench/llama-bench.cpp`, `tools/server/server-models.cpp`, `tools/server/server-models.h`, `vendor/cpp-httplib/httplib.cpp`, `vendor/miniaudio/miniaudio.h`
**引用的文件**: `vendor/cpp-httplib/httplib.cpp`
**漏引的文件**: `common/arg.cpp`, `common/preset.cpp`
**噪声文件**: `tools/llama-bench/llama-bench.cpp`, `tools/server/server-models.cpp`, `tools/server/server-models.h`, `vendor/cpp-httplib/httplib.cpp`, `vendor/miniaudio/miniaudio.h` (5/7 = 71%)

**漏引根因分析**:
- `common/arg.cpp`: 答案正文**完全没有提及**
  → 原因推断: `arg.cpp` 是参数解析入口，问题若未直接问'参数解析'，LLM 可能觉得不需要引用
- `common/preset.cpp`: 答案正文**完全没有提及**
  → 原因推断: `preset.cpp` 被忽略——LLM 可能认为 download.cpp 已足够覆盖配置逻辑

**错误引用的噪声文件** (1 个):
- `vendor/cpp-httplib/httplib.cpp`
  → 上下文: 根据您提供的检索信息，我无法分析您所关心的“命令行参数转配置 map 的实现”以及“preset 调用点对失败处理的理解是否一致”的问题。...
  → 原因推断: vendor/ 第三方库文件被幻觉引用

### posthoc_public_032 (初始化/外部 API 契约)
**问题**: AI 改了一个后端注册入口，我担心静态对象、注册上下文和进程退出时的生命周期处理不一致。帮我看注册和使用路径是否符合当前调用顺序假设？
**Gold 文件**: `ggml/src/ggml-webgpu/ggml-webgpu.cpp`
**检索到的文件**: `ggml/include/ggml-backend.h`, `ggml/include/ggml-openvino.h`, `ggml/include/ggml-webgpu.h`, `ggml/src/ggml-backend-reg.cpp`, `ggml/src/ggml-cann/ggml-cann.cpp`, `ggml/src/ggml-hexagon/ggml-hexagon.cpp`, `ggml/src/ggml-openvino/ggml-openvino.cpp`, `ggml/src/ggml-webgpu/ggml-webgpu.cpp`, `tests/test-chat-auto-parser.cpp`
**引用的文件**: `ggml/src/ggml-backend-reg.cpp`
**漏引的文件**: `ggml/src/ggml-webgpu/ggml-webgpu.cpp`
**噪声文件**: `ggml/include/ggml-backend.h`, `ggml/include/ggml-openvino.h`, `ggml/include/ggml-webgpu.h`, `ggml/src/ggml-backend-reg.cpp`, `ggml/src/ggml-cann/ggml-cann.cpp`, `ggml/src/ggml-hexagon/ggml-hexagon.cpp`, `ggml/src/ggml-openvino/ggml-openvino.cpp`, `tests/test-chat-auto-parser.cpp` (8/9 = 89%)

**漏引根因分析**:
- `ggml/src/ggml-webgpu/ggml-webgpu.cpp`: 答案正文**提到了文件名**（无反引号），但未被纳入引用清单
  → 原因推断: ggml 目录文件在答案中被忽略

**错误引用的噪声文件** (1 个):
- `ggml/src/ggml-backend-reg.cpp`
  → 上下文: ### 1. 注册表的创建与销毁路径...

**答案结构**:
- ### 1. 注册表的创建与销毁路径
- ### 2. 后端注册入口的静态对象与上下文管理
- ### 3. 静态析构顺序分析
- ### 4. 回答具体问题
- ### 参考文件清单

### posthoc_public_033 (失败返回语义)
**问题**: AI 生成了 sampler 后端能力检查逻辑，我担心 CPU fast path、临时计算上下文、后端 apply 回调和 unsupported 返回 false 这些路径没有统一表达失败语义。帮我看这些分支是否闭合？
**Gold 文件**: `src/llama-sampler.cpp`
**检索到的文件**: `common/arg.cpp`, `common/chat-auto-parser.h`, `common/chat-diff-analyzer.cpp`, `common/llguidance.cpp`, `common/sampling.cpp`, `ggml/include/ggml.h`, `src/llama-context.cpp`, `src/llama-graph.cpp`, `src/llama-sampler.cpp`, `tests/test-backend-sampler.cpp`, `tests/test-chat-auto-parser.cpp`, `tests/test-grammar-llguidance.cpp`, `tests/test-thread-safety.cpp`, `tools/completion/completion.cpp`, `tools/server/server-task.h`, `vendor/miniaudio/miniaudio.h`
**引用的文件**: `vendor/miniaudio/miniaudio.h`
**漏引的文件**: `src/llama-sampler.cpp`
**噪声文件**: `common/arg.cpp`, `common/chat-auto-parser.h`, `common/chat-diff-analyzer.cpp`, `common/llguidance.cpp`, `common/sampling.cpp`, `ggml/include/ggml.h`, `src/llama-context.cpp`, `src/llama-graph.cpp`, `tests/test-backend-sampler.cpp`, `tests/test-chat-auto-parser.cpp`, `tests/test-grammar-llguidance.cpp`, `tests/test-thread-safety.cpp`, `tools/completion/completion.cpp`, `tools/server/server-task.h`, `vendor/miniaudio/miniaudio.h` (15/16 = 94%)

**漏引根因分析**:
- `src/llama-sampler.cpp`: 答案正文**完全没有提及**
  → 原因推断: llama-sampler.cpp 被 vendor/ 噪声（如 miniaudio.h）替代

**错误引用的噪声文件** (1 个):
- `vendor/miniaudio/miniaudio.h`
  → 上下文: 根据您提供的检索信息，所有代码片段均来自Miniaudio音频库的实现文件 `vendor/miniaudio/miniaudio.h`。这些片段主要涉及节点图处理、数据源节点、分割器、滤波器、引擎节点等音频处理功能，**没有任何与“sam...
  → 原因推断: vendor/ 第三方库文件被幻觉引用

**答案结构**:
- ### 参考文件清单

### posthoc_public_035 (资源生命周期/清理边界)
**问题**: AI 改了文本分段逻辑后，我担心这里被当成需要手动清理的资源路径。帮我看这段实现实际是否涉及手动资源申请、失败返回和清理闭环？
**Gold 文件**: `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`
**检索到的文件**: `common/chat-auto-parser-helpers.cpp`, `common/chat-auto-parser.h`, `common/chat-diff-analyzer.cpp`, `common/download.cpp`, `common/reasoning-budget.cpp`, `tests/test-chat-auto-parser.cpp`, `tests/test-chat-template.cpp`, `tools/cli/cli.cpp`
**引用的文件**: `common/chat-auto-parser-helpers.cpp`, `common/download.cpp`
**漏引的文件**: `common/chat-diff-analyzer.cpp`
**噪声文件**: `common/chat-auto-parser.h`, `common/download.cpp`, `common/reasoning-budget.cpp`, `tests/test-chat-auto-parser.cpp`, `tests/test-chat-template.cpp`, `tools/cli/cli.cpp` (6/8 = 75%)

**漏引根因分析**:
- `common/chat-diff-analyzer.cpp`: 答案正文**完全没有提及**
  → 原因推断: 同目录 `common/` 下有多个文件，LLM 只选了其一
  → 原因推断: 调用方文件，LLM 可能更关注实现文件

**错误引用的噪声文件** (1 个):
- `common/download.cpp`
  → 上下文: ...
  → 原因推断: common/ 下其他文件，LLM 可能混淆了同目录文件

### posthoc_public_036 (状态一致性)
**问题**: AI 改了 ngram cache 的窗口推进逻辑，我担心旧窗口数据没有随着 begin 位置更新而清掉。帮我看 key 映射、keys 和 values 的过期条目是否保持一致？
**Gold 文件**: `common/ngram-map.cpp`
**检索到的文件**: `common/arg.cpp`, `common/ngram-cache.cpp`, `common/ngram-cache.h`, `common/ngram-map.cpp`, `common/ngram-map.h`, `common/ngram-mod.cpp`, `ggml/src/ggml-vulkan/vulkan-shaders/vulkan-shaders-gen.cpp`, `ggml/src/ggml.c`, `src/llama-grammar.h`, `src/llama-memory-recurrent.cpp`, `tools/server/server-task.cpp`, `vendor/nlohmann/json.hpp`
**引用的文件**: `common/lookup.cpp`, `common/ngram-cache.cpp`, `common/speculative.cpp`
**漏引的文件**: `common/ngram-map.cpp`
**噪声文件**: `common/arg.cpp`, `common/ngram-cache.cpp`, `common/ngram-cache.h`, `common/ngram-map.h`, `common/ngram-mod.cpp`, `ggml/src/ggml-vulkan/vulkan-shaders/vulkan-shaders-gen.cpp`, `ggml/src/ggml.c`, `src/llama-grammar.h`, `src/llama-memory-recurrent.cpp`, `tools/server/server-task.cpp`, `vendor/nlohmann/json.hpp` (11/12 = 92%)

**漏引根因分析**:
- `common/ngram-map.cpp`: 答案正文**完全没有提及**
  → 原因推断: 同目录 `common/` 下有多个文件，LLM 只选了其一
  → 原因推断: ngram-map.cpp 被 ngram-cache.cpp / speculative.cpp 等相似文件替代

**错误引用的噪声文件** (3 个):
- `common/lookup.cpp`
  → 上下文: - 关键同步点：当 `begin` 递增时，应遍历即将丢弃的窗口范围，对每个节点执行映射删除；若使用循环数组或双端队列，需保证 `pop_front` 操作同时清理映射。...
  → 原因推断: common/ 下其他文件，LLM 可能混淆了同目录文件
- `common/ngram-cache.cpp`
  → 上下文: - 窗口推进通常由 `ngram_cache::erase` 或类似函数完成，需确保在移动 `begin` 位置时，同时从映射表（如 `unordered_map`）中移除对应旧位置的 key，并从 `keys`/`values` 数组中移...
  → 原因推断: common/ 下其他文件，LLM 可能混淆了同目录文件
- `common/speculative.cpp`
  → 上下文: - 关键同步点：当 `begin` 递增时，应遍历即将丢弃的窗口范围，对每个节点执行映射删除；若使用循环数组或双端队列，需保证 `pop_front` 操作同时清理映射。...
  → 原因推断: common/ 下其他文件，LLM 可能混淆了同目录文件

### posthoc_public_037 (失败返回语义)
**问题**: AI 改了聊天模板校验逻辑，我担心 Jinja 分支和普通分支对失败的表达方式不一致。帮我看两条路径是否都只用 boolean 返回校验结果，并且不会改外部状态？
**Gold 文件**: `common/arg.cpp`, `common/chat.cpp`
**检索到的文件**: `common/arg.cpp`, `common/chat-diff-analyzer.cpp`, `common/chat.cpp`, `common/chat.h`, `common/jinja/parser.cpp`, `common/jinja/value.cpp`, `common/log.cpp`, `common/preset.cpp`, `src/llama.cpp`, `tests/test-chat-auto-parser.cpp`, `tests/test-chat-template.cpp`, `tools/cli/cli.cpp`, `tools/server/server-common.cpp`
**引用的文件**: `common/chat.cpp`
**漏引的文件**: `common/arg.cpp`
**噪声文件**: `common/chat-diff-analyzer.cpp`, `common/chat.h`, `common/jinja/parser.cpp`, `common/jinja/value.cpp`, `common/log.cpp`, `common/preset.cpp`, `src/llama.cpp`, `tests/test-chat-auto-parser.cpp`, `tests/test-chat-template.cpp`, `tools/cli/cli.cpp`, `tools/server/server-common.cpp` (11/13 = 85%)

**漏引根因分析**:
- `common/arg.cpp`: 答案正文**完全没有提及**
  → 原因推断: 同目录 `common/` 下有多个文件，LLM 只选了其一
  → 原因推断: `arg.cpp` 是参数解析入口，问题若未直接问'参数解析'，LLM 可能觉得不需要引用

### posthoc_public_039 (状态一致性)
**问题**: AI 改了模型服务 endpoint 的状态处理，我担心环境变量读取、默认值和规范化结果在调用点里被当成不同状态。帮我看 reset/cache/context 相关假设是否会被破坏？
**Gold 文件**: `common/arg.cpp`, `common/common.cpp`, `common/hf-cache.cpp`
**检索到的文件**: `common/arg.cpp`, `common/common.cpp`, `examples/llama.android/lib/src/main/cpp/ai_chat.cpp`, `ggml/src/ggml-openvino/utils.cpp`, `ggml/src/ggml-rpc/ggml-rpc.cpp`, `src/llama-batch.cpp`, `src/llama-context.cpp`, `src/llama-memory-recurrent.cpp`, `tools/llama-bench/llama-bench.cpp`, `tools/server/server-context.cpp`, `tools/server/server-http.cpp`, `tools/server/server-models.cpp`, `tools/server/server.cpp`, `vendor/miniaudio/miniaudio.h`
**引用的文件**: `common/arg.cpp`
**漏引的文件**: `common/common.cpp`
**噪声文件**: `examples/llama.android/lib/src/main/cpp/ai_chat.cpp`, `ggml/src/ggml-openvino/utils.cpp`, `ggml/src/ggml-rpc/ggml-rpc.cpp`, `src/llama-batch.cpp`, `src/llama-context.cpp`, `src/llama-memory-recurrent.cpp`, `tools/llama-bench/llama-bench.cpp`, `tools/server/server-context.cpp`, `tools/server/server-http.cpp`, `tools/server/server-models.cpp`, `tools/server/server.cpp`, `vendor/miniaudio/miniaudio.h` (12/14 = 86%)

**漏引根因分析**:
- `common/common.cpp`: 答案正文**完全没有提及**
  → 原因推断: 同目录 `common/` 下有多个文件，LLM 只选了其一
  → 原因推断: `common.cpp` 是通用基础设施，LLM 可能认为它'太泛'而不具针对性

**答案结构**:
- ### 1. 环境变量读取与状态一致性
- ### 2. 默认值与规范化
- ### 3. reset / cache 假设
- ### 4. context 假设
- ### 潜在风险点（基于现有信息无法完全排除）
- ### 参考文件清单

### posthoc_public_043 (并发/异步安全)
**问题**: AI 生成了并行下载逻辑，我担心异步任务提前返回、future 收集和调用方同步读取结果之间生命周期不一致。帮我看下载结果是否会按调用方预期完成并返回？
**Gold 文件**: `common/arg.cpp`, `common/download.cpp`
**检索到的文件**: `common/arg.cpp`, `common/download.cpp`, `src/llama-adapter.cpp`, `src/llama-context.cpp`, `src/llama-kv-cache.cpp`, `src/llama-model-loader.cpp`, `tools/cli/cli.cpp`, `tools/llama-bench/llama-bench.cpp`, `tools/server/server-models.cpp`, `tools/server/server-queue.cpp`, `tools/server/server.cpp`, `vendor/cpp-httplib/httplib.cpp`
**引用的文件**: `common/download.cpp`
**漏引的文件**: `common/arg.cpp`
**噪声文件**: `src/llama-adapter.cpp`, `src/llama-context.cpp`, `src/llama-kv-cache.cpp`, `src/llama-model-loader.cpp`, `tools/cli/cli.cpp`, `tools/llama-bench/llama-bench.cpp`, `tools/server/server-models.cpp`, `tools/server/server-queue.cpp`, `tools/server/server.cpp`, `vendor/cpp-httplib/httplib.cpp` (10/12 = 83%)

**漏引根因分析**:
- `common/arg.cpp`: 答案正文**完全没有提及**
  → 原因推断: 同目录 `common/` 下有多个文件，LLM 只选了其一
  → 原因推断: `arg.cpp` 是参数解析入口，问题若未直接问'参数解析'，LLM 可能觉得不需要引用

**答案结构**:
- ## 问题代码分析
- ### 1. 悬垂引用风险
- ### 2. Future 收集与同步等待
- ### 3. 其他相关函数的状态
- ## 结论
- ### 参考文件清单

### posthoc_public_046 (并发/异步安全)
**问题**: AI 生成了扫描物理 CPU 核心数的逻辑，我担心它在读取系统拓扑文件时引入共享状态或线程安全问题。帮我看这条路径是否只是局部只读扫描？
**Gold 文件**: `common/common.cpp`
**检索到的文件**: `common/chat-auto-parser.h`, `common/common.cpp`, `examples/gguf-hash/deps/xxhash/xxhash.h`, `ggml/src/ggml-backend-reg.cpp`, `ggml/src/ggml-backend.cpp`, `ggml/src/ggml-cpu/arch/x86/cpu-feats.cpp`, `ggml/src/ggml-vulkan/ggml-vulkan.cpp`, `pocs/vdot/q8dot.cpp`, `pocs/vdot/vdot.cpp`, `src/llama-context.cpp`, `src/llama-graph.cpp`, `src/llama-memory-hybrid-iswa.cpp`, `src/llama-memory-hybrid.cpp`, `src/llama-mmap.cpp`, `src/llama-model.cpp`
**引用的文件**: `pocs/vdot/q8dot.cpp`, `pocs/vdot/vdot.cpp`, `src/llama-model.cpp`
**漏引的文件**: `common/common.cpp`
**噪声文件**: `common/chat-auto-parser.h`, `examples/gguf-hash/deps/xxhash/xxhash.h`, `ggml/src/ggml-backend-reg.cpp`, `ggml/src/ggml-backend.cpp`, `ggml/src/ggml-cpu/arch/x86/cpu-feats.cpp`, `ggml/src/ggml-vulkan/ggml-vulkan.cpp`, `pocs/vdot/q8dot.cpp`, `pocs/vdot/vdot.cpp`, `src/llama-context.cpp`, `src/llama-graph.cpp`, `src/llama-memory-hybrid-iswa.cpp`, `src/llama-memory-hybrid.cpp`, `src/llama-mmap.cpp`, `src/llama-model.cpp` (14/15 = 93%)

**漏引根因分析**:
- `common/common.cpp`: 答案正文**完全没有提及**
  → 原因推断: `common.cpp` 是通用基础设施，LLM 可能认为它'太泛'而不具针对性

**错误引用的噪声文件** (3 个):
- `pocs/vdot/q8dot.cpp`
  → 上下文: 根据你提供的检索信息，**我没有找到直接匹配“扫描物理 CPU 核心数并读取系统拓扑文件”的函数**。检索到的内容主要是量化点积性能测试（`pocs/vdot/q8dot.cpp`、`pocs/vdot/vdot.cpp`）和模型加载相关代...
  → 原因推断: pocs/ 实验代码被误引用
- `pocs/vdot/vdot.cpp`
  → 上下文: 根据你提供的检索信息，**我没有找到直接匹配“扫描物理 CPU 核心数并读取系统拓扑文件”的函数**。检索到的内容主要是量化点积性能测试（`pocs/vdot/q8dot.cpp`、`pocs/vdot/vdot.cpp`）和模型加载相关代...
  → 原因推断: pocs/ 实验代码被误引用
- `src/llama-model.cpp`
  → 上下文: 根据你提供的检索信息，**我没有找到直接匹配“扫描物理 CPU 核心数并读取系统拓扑文件”的函数**。检索到的内容主要是量化点积性能测试（`pocs/vdot/q8dot.cpp`、`pocs/vdot/vdot.cpp`）和模型加载相关代...

**答案结构**:
- ### 这类操作通常的特征
- ### 建议你自行核查的点
- ### 参考文件清单

### posthoc_public_047 (配置/参数传播一致性)
**问题**: AI 改了 CPU 参数后处理逻辑，我担心线程数缺省补全、模型角色继承和 CPU mask 校验之间默认值假设不一致。帮我看参数解析后的补全路径是否稳定？
**Gold 文件**: `common/arg.cpp`, `common/common.cpp`
**检索到的文件**: `common/arg.cpp`, `common/common.cpp`, `common/common.h`, `common/log.cpp`, `examples/training/finetune.cpp`, `ggml/include/ggml.h`, `ggml/src/ggml-backend.cpp`, `ggml/src/ggml-openvino/ggml-decoder.cpp`, `src/llama.cpp`, `tests/test-backend-sampler.cpp`, `tests/test-llama-archs.cpp`, `tools/fit-params/fit-params.cpp`, `tools/llama-bench/llama-bench.cpp`
**引用的文件**: `common/common.cpp`
**漏引的文件**: `common/arg.cpp`
**噪声文件**: `common/common.h`, `common/log.cpp`, `examples/training/finetune.cpp`, `ggml/include/ggml.h`, `ggml/src/ggml-backend.cpp`, `ggml/src/ggml-openvino/ggml-decoder.cpp`, `src/llama.cpp`, `tests/test-backend-sampler.cpp`, `tests/test-llama-archs.cpp`, `tools/fit-params/fit-params.cpp`, `tools/llama-bench/llama-bench.cpp` (11/13 = 85%)

**漏引根因分析**:
- `common/arg.cpp`: 答案正文**提到了文件名**（无反引号），但未被纳入引用清单
  → 原因推断: 同目录 `common/` 下有多个文件，LLM 只选了其一
  → 原因推断: `arg.cpp` 是参数解析入口，问题若未直接问'参数解析'，LLM 可能觉得不需要引用

**答案结构**:
- ### 1. `postprocess_cpu_params` 的继承与补全逻辑（`common/common.cpp:265-287`）
- ### 2. 参数解析对 `n_threads` 和 `mask` 的影响（`common/arg.cpp:427-644` 调用链）
- ### 3. 稳定性风险点
- ### 结论
- ### 参考文件清单

### posthoc_public_048 (调用方契约兼容性)
**问题**: AI 改了模型 endpoint 获取实现，我担心返回值规范化、环境变量覆盖和调用方使用方式对不上。帮我看现有调用方是否只依赖统一的 endpoint 字符串结果？
**Gold 文件**: `common/arg.cpp`, `common/common.cpp`, `common/hf-cache.cpp`
**检索到的文件**: `common/arg.cpp`, `common/common.cpp`, `common/common.h`, `common/hf-cache.cpp`, `common/hf-cache.h`, `common/peg-parser.h`, `ggml/src/ggml-rpc/ggml-rpc.cpp`, `tests/test-chat.cpp`, `tools/llama-bench/llama-bench.cpp`, `tools/server/server-common.cpp`, `tools/server/server-common.h`, `tools/server/server.cpp`, `vendor/cpp-httplib/httplib.cpp`, `vendor/miniaudio/miniaudio.h`
**引用的文件**: `common/common.cpp`
**漏引的文件**: `common/arg.cpp`, `common/hf-cache.cpp`
**噪声文件**: `common/common.h`, `common/hf-cache.h`, `common/peg-parser.h`, `ggml/src/ggml-rpc/ggml-rpc.cpp`, `tests/test-chat.cpp`, `tools/llama-bench/llama-bench.cpp`, `tools/server/server-common.cpp`, `tools/server/server-common.h`, `tools/server/server.cpp`, `vendor/cpp-httplib/httplib.cpp`, `vendor/miniaudio/miniaudio.h` (11/14 = 79%)

**漏引根因分析**:
- `common/arg.cpp`: 答案正文**提到了文件名**（无反引号），但未被纳入引用清单
  → 原因推断: 同目录 `common/` 下有多个文件，LLM 只选了其一
  → 原因推断: `arg.cpp` 是参数解析入口，问题若未直接问'参数解析'，LLM 可能觉得不需要引用
- `common/hf-cache.cpp`: 答案正文**提到了文件名**（无反引号），但未被纳入引用清单
  → 原因推断: 同目录 `common/` 下有多个文件，LLM 只选了其一
  → 原因推断: hf-cache.cpp 是辅助文件，问题未聚焦缓存逻辑

**答案结构**:
- ### 1. `common_get_model_endpoint()` 的实现（已规范化）
- ### 2. 调用方如何使用该返回值
- #### （1）`common_params_handle_remote_preset`
- #### （2）`get_repo_commit`
- #### （3）其他可能的调用
- ### 3. 结论

### posthoc_public_049 (配置/参数传播一致性)
**问题**: AI 改了命令行参数帮助文本的格式化逻辑，我担心别名、value hint 和 help 文本展示会依赖外部状态。帮我看它是否只根据当前参数对象已有字段生成输出？
**Gold 文件**: `common/arg.cpp`
**检索到的文件**: `common/arg.cpp`, `common/arg.h`, `examples/diffusion/diffusion-cli.cpp`, `examples/gen-docs/gen-docs.cpp`, `examples/gguf-hash/deps/xxhash/xxhash.h`, `src/llama-model.cpp`, `tests/test-chat-auto-parser.cpp`, `tools/llama-bench/llama-bench.cpp`, `tools/parser/debug-template-parser.cpp`, `tools/quantize/quantize.cpp`, `vendor/cpp-httplib/httplib.cpp`
**引用的文件**: `examples/gen-docs/gen-docs.cpp`
**漏引的文件**: `common/arg.cpp`
**噪声文件**: `common/arg.h`, `examples/diffusion/diffusion-cli.cpp`, `examples/gen-docs/gen-docs.cpp`, `examples/gguf-hash/deps/xxhash/xxhash.h`, `src/llama-model.cpp`, `tests/test-chat-auto-parser.cpp`, `tools/llama-bench/llama-bench.cpp`, `tools/parser/debug-template-parser.cpp`, `tools/quantize/quantize.cpp`, `vendor/cpp-httplib/httplib.cpp` (10/11 = 91%)

**漏引根因分析**:
- `common/arg.cpp`: 答案正文**提到了文件名**（无反引号），但未被纳入引用清单
  → 原因推断: `arg.cpp` 是参数解析入口，问题若未直接问'参数解析'，LLM 可能觉得不需要引用

**错误引用的噪声文件** (1 个):
- `examples/gen-docs/gen-docs.cpp`
  → 上下文: ...
  → 原因推断: examples/ 文件被 LLM 当作'调用方证据'引用

**答案结构**:
- ### 1. 核心函数 `write_table_entry` 的行为
- ### 2. 成员字段的来源
- ### 3. 关于“别名、value hint 和 help 文本展示依赖外部状态”的担忧
- ### 结论
- ### 参考文件清单

## 漏引根因分类汇总

### 按漏引原因分类

| 原因 | 次数 |
|------|------|
| arg.cpp 未被问题直接提及 | 9 |
| helper 实现文件被忽略 | 7 |
| common.cpp 被当作'太泛'而忽略 | 6 |
| 调用方文件被忽略 | 6 |
| llama-model.cpp 被忽略 | 2 |
| ggml 目录文件被忽略 | 2 |
| hf-cache.cpp 被忽略 | 2 |
| preset.cpp 被忽略 | 2 |
| sampling.cpp 被忽略 | 1 |
| llama-quant.cpp 被忽略 | 1 |
| 其他文件被忽略 | 1 |
| chat.cpp 被忽略 | 1 |
| llama-sampler.cpp 被忽略 | 1 |
| ngram-map.cpp 被忽略 | 1 |

### 按被漏引文件分类

| 文件 | 漏引次数 |
|------|----------|
| `common/arg.cpp` | 9 |
| `common/chat-auto-parser-helpers.cpp` | 7 |
| `common/common.cpp` | 6 |
| `common/chat-diff-analyzer.cpp` | 6 |
| `src/llama-model.cpp` | 2 |
| `common/hf-cache.cpp` | 2 |
| `common/preset.cpp` | 2 |
| `ggml/src/ggml-backend-reg.cpp` | 1 |
| `common/sampling.cpp` | 1 |
| `src/llama-quant.cpp` | 1 |
| `common/chat-auto-parser-generator.cpp` | 1 |
| `common/chat.cpp` | 1 |
| `ggml/src/ggml-webgpu/ggml-webgpu.cpp` | 1 |
| `src/llama-sampler.cpp` | 1 |
| `common/ngram-map.cpp` | 1 |

### 错误引用噪声的根因

| 根因 | 次数 |
|------|------|
| vendor/ 第三方库被幻觉引用 | 8 |
| 其他噪声被引用 | 6 |
| common/ 下其他文件被混淆 | 6 |
| tools/ 文件被当作应用场景 | 3 |
| examples/ 文件被当作调用方证据 | 2 |
| pocs/ 实验代码被误引用 | 2 |

## 核心结论

1. **搜到未引是 Hard benchmark 的主要问题（29/50 题）**，远超 easy benchmark 的 5/50。
2. **漏引集中在三类文件**:
   - `common/common.cpp` — 被当作'太泛'的基础设施而忽略（4 次）
   - `common/arg.cpp` — 参数解析文件，问题未直接提及则不被引用（5 次）
   - `common/chat-auto-parser-helpers.cpp` / `chat-diff-analyzer.cpp` — helper 文件配对遗漏（8 次）
3. **LLM 主动选择噪声作为论据**:
   - examples/ 文件被当作'调用方证据'引用
   - tools/ 文件被当作'实际应用场景'引用
   - vendor/ 第三方库被幻觉引用（httplib.cpp、miniaudio.h）
4. **根因在答案生成 prompt**: LLM 没有被强制要求'分析并引用所有检索到的证据文件'，导致它 cherry-pick 自己认为重要的文件，而忽略 gold。
5. **Hard benchmark 放大了 easy benchmark 的问题**: easy 中 5 题搜到未引，hard 中 29 题——问题越模糊，LLM 越倾向于凭经验而非证据推理。