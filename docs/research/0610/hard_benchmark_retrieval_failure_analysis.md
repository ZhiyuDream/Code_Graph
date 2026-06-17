# Hard Benchmark 检索失败根因分析

**结果文件**: `benchmark_hard_20260607_200601.json`
**分析口径**: 排除 `.h` / `.hpp` 头文件

## 总览

- 检索失败的题目: **21 / 50**
- 其中覆盖率 0%: 5 题
- 其中部分覆盖 (1%-99%): 16 题

### 按维度分布

| 维度 | 检索失败题数 | 0%覆盖 | 部分覆盖 |
|------|------------|--------|----------|
| 配置/参数传播一致性 | 5 | 0 | 5 |
| 共享 helper 复用影响 | 5 | 0 | 5 |
| 调用方契约兼容性 | 4 | 1 | 3 |
| 资源生命周期/清理边界 | 2 | 2 | 0 |
| 失败返回语义 | 2 | 1 | 1 |
| 初始化/外部 API 契约 | 1 | 1 | 0 |
| 状态一致性 | 1 | 0 | 1 |
| 并发/异步安全 | 1 | 0 | 1 |

## 逐题根因分析

### posthoc_public_001 (调用方契约兼容性)
**问题**: AI 生成了设备切换相关实现，我担心几个调用路径对返回值和副作用的理解不一样。帮我顺一下现有调用方主要依赖什么行为，失败或重复切换时会不会破坏调用方假设？
**Gold 文件 (3)**: `ggml/src/ggml-sycl/common.cpp`, `ggml/src/ggml-sycl/cpy.cpp`, `ggml/src/ggml-sycl/element_wise.cpp`
**检索到的文件 (10)**: `common/chat-auto-parser.h`, `common/chat-diff-analyzer.cpp`, `ggml/src/ggml-cann/common.h`, `ggml/src/ggml-cann/ggml-cann.cpp`, `ggml/src/ggml-metal/ggml-metal-device.cpp`, `ggml/src/ggml-sycl/dpct/helper.hpp`, `tests/test-chat-auto-parser.cpp`, `tests/test-opt.cpp`, `tools/rpc/rpc-server.cpp`, `tools/server/server-task.h`
**缺失的文件**: `ggml/src/ggml-sycl/common.cpp`, `ggml/src/ggml-sycl/cpy.cpp`, `ggml/src/ggml-sycl/element_wise.cpp`
**检索覆盖率**: 0%

**初始检索 query**: AI 生成了设备切换相关实现，我担心几个调用路径对返回值和副作用的理解不一样。帮我顺一下现有调用方主要依赖什么行为，失败或重复切换时会不会破坏调用方假设？
**ReAct actions**: call_llm, grep_search, semantic_search, retrieve
**推断根因**:
- 问题中**没有反引号函数名**，LLM 需要从自然语言推断目标符号
- 检索到了同模块文件，但没命中具体 gold 文件

### posthoc_public_003 (调用方契约兼容性)
**问题**: AI 调整了模型加载默认参数初始化，我担心不同入口拿到的初值来源不一致。帮我看 common 加载、量化和底层适配路径是否仍从同一套默认参数出发？
**Gold 文件 (4)**: `common/common.cpp`, `src/llama-model.cpp`, `src/llama-quant.cpp`, `src/llama.cpp`
**检索到的文件 (11)**: `common/common.cpp`, `common/peg-parser.h`, `examples/convert-llama2c-to-ggml/convert-llama2c-to-ggml.cpp`, `examples/gguf/gguf.cpp`, `examples/retrieval/retrieval.cpp`, `src/llama-model.cpp`, `tests/test-backend-sampler.cpp`, `tests/test-quant-type-selection.cpp`, `tools/imatrix/imatrix.cpp`, `tools/server/server-context.cpp`, `tools/server/server-context.h`
**缺失的文件**: `src/llama-quant.cpp`, `src/llama.cpp`
**检索覆盖率**: 50%

**初始检索 query**: AI 调整了模型加载默认参数初始化，我担心不同入口拿到的初值来源不一致。帮我看 common 加载、量化和底层适配路径是否仍从同一套默认参数出发？
**ReAct actions**: call_llm, grep_search, retrieve
**推断根因**:
- 问题中**没有反引号函数名**，LLM 需要从自然语言推断目标符号
- **同目录遗漏**: 同一目录下有多个相关文件，只命中了部分

### posthoc_public_005 (调用方契约兼容性)
**问题**: AI 改了只裁剪字符串开头空白的逻辑，我担心调用方把它和完整裁剪混用。帮我确认现有使用点是否只依赖前缀空白被移除，而不是期待尾部也被处理？
**Gold 文件 (2)**: `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`
**检索到的文件 (6)**: `common/chat-auto-parser-helpers.cpp`, `common/chat-auto-parser-helpers.h`, `common/chat-auto-parser.h`, `common/chat-peg-parser.cpp`, `common/jinja/string.h`, `src/llama-chat.cpp`
**缺失的文件**: `common/chat-diff-analyzer.cpp`
**检索覆盖率**: 50%

**初始检索 query**: AI 改了只裁剪字符串开头空白的逻辑，我担心调用方把它和完整裁剪混用。帮我确认现有使用点是否只依赖前缀空白被移除，而不是期待尾部也被处理？
**ReAct actions**: call_llm, expand_callees, expand_callers, retrieve
**推断根因**:
- 问题中**没有反引号函数名**，LLM 需要从自然语言推断目标符号
- **同目录遗漏**: 同一目录下有多个相关文件，只命中了部分

### posthoc_public_007 (调用方契约兼容性)
**问题**: AI 生成了两段模板文本的差异拆分逻辑，我担心分段、公共部分和左右差异的返回结构会被调用方误解，也担心调用方传入的文本参数和无副作用假设没对齐。帮我看现有调用方对返回结构、参数和副作用的使用是否符合实现语义？
**Gold 文件 (3)**: `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`, `common/chat.cpp`
**检索到的文件 (6)**: `common/chat-auto-parser-helpers.cpp`, `common/chat-auto-parser-helpers.h`, `common/chat-auto-parser.h`, `common/chat-diff-analyzer.cpp`, `tests/test-chat-auto-parser.cpp`, `tools/parser/template-analysis.cpp`
**缺失的文件**: `common/chat.cpp`
**检索覆盖率**: 67%

**初始检索 query**: AI 生成了两段模板文本的差异拆分逻辑，我担心分段、公共部分和左右差异的返回结构会被调用方误解，也担心调用方传入的文本参数和无副作用假设没对齐。帮我看现有调用方对返回结构、参数和副作用的使用是否符合实现语义？
**ReAct actions**: read_class, call_llm, expand_callers, retrieve
**推断根因**:
- 问题中**没有反引号函数名**，LLM 需要从自然语言推断目标符号
- **同目录遗漏**: 同一目录下有多个相关文件，只命中了部分

### posthoc_public_011 (配置/参数传播一致性)
**问题**: AI 改了 sampler 初始化时的配置搬运逻辑，我担心采样参数、性能开关和 grammar 相关字段在进入内部参数前被漏传或改义。帮我顺一下这些值来源和最终使用是否一致？
**Gold 文件 (3)**: `common/common.cpp`, `common/sampling.cpp`, `common/speculative.cpp`
**检索到的文件 (11)**: `common/common.cpp`, `common/llguidance.cpp`, `common/log.cpp`, `common/sampling.cpp`, `common/sampling.h`, `examples/speculative/speculative.cpp`, `src/llama-sampler.cpp`, `tests/test-backend-sampler.cpp`, `tests/test-grammar-llguidance.cpp`, `tests/test-sampling.cpp`, `tools/completion/completion.cpp`
**缺失的文件**: `common/speculative.cpp`
**检索覆盖率**: 67%

**初始检索 query**: AI 改了 sampler 初始化时的配置搬运逻辑，我担心采样参数、性能开关和 grammar 相关字段在进入内部参数前被漏传或改义。帮我顺一下这些值来源和最终使用是否一致？
**ReAct actions**: read_class, grep_search, retrieve, expand_callees, expand_callers, call_llm
**推断根因**:
- 问题中**没有反引号函数名**，LLM 需要从自然语言推断目标符号
- **同目录遗漏**: 同一目录下有多个相关文件，只命中了部分

### posthoc_public_012 (配置/参数传播一致性)
**问题**: AI 改了聊天模板配置读取逻辑，我担心默认模板名、缺失模板和调用方 fallback 的语义不一致。帮我看不同聊天入口是否仍按同一套模板选择规则工作？
**Gold 文件 (3)**: `common/chat.cpp`, `common/common.cpp`, `src/llama-model.cpp`
**检索到的文件 (10)**: `common/chat.cpp`, `common/chat.h`, `src/llama-chat.cpp`, `src/llama-mmap.cpp`, `src/llama-model.cpp`, `tests/test-chat-template.cpp`, `tests/test-chat.cpp`, `tools/cli/cli.cpp`, `tools/rpc/rpc-server.cpp`, `vendor/cpp-httplib/httplib.cpp`
**缺失的文件**: `common/common.cpp`
**检索覆盖率**: 67%

**初始检索 query**: AI 改了聊天模板配置读取逻辑，我担心默认模板名、缺失模板和调用方 fallback 的语义不一致。帮我看不同聊天入口是否仍按同一套模板选择规则工作？
**ReAct actions**: grep_search, retrieve, expand_callers, call_llm, semantic_search
**推断根因**:
- 问题中**没有反引号函数名**，LLM 需要从自然语言推断目标符号
- **同目录遗漏**: 同一目录下有多个相关文件，只命中了部分

### posthoc_public_013 (资源生命周期/清理边界)
**问题**: AI 改了缓存扩容和重新分配逻辑，我担心旧 buffer 释放后重新申请失败，会让容量记录和真实指针状态对不上。帮我查一下成功扩容和分配失败路径是否都处理清楚？
**Gold 文件 (1)**: `ggml/src/ggml-cann/aclnn_ops.cpp`
**检索到的文件 (13)**: `ggml/src/ggml-backend.cpp`, `ggml/src/ggml-cann/ggml-cann.cpp`, `ggml/src/ggml-hexagon/htp/main.c`, `ggml/src/ggml-sycl/dpct/helper.hpp`, `include/llama.h`, `src/llama-context.cpp`, `src/llama-context.h`, `src/llama-kv-cache.cpp`, `src/llama-mmap.cpp`, `tests/test-alloc.cpp`, `tools/cli/cli.cpp`, `vendor/miniaudio/miniaudio.h`, `vendor/stb/stb_image.h`
**缺失的文件**: `ggml/src/ggml-cann/aclnn_ops.cpp`
**检索覆盖率**: 0%

**初始检索 query**: AI 改了缓存扩容和重新分配逻辑，我担心旧 buffer 释放后重新申请失败，会让容量记录和真实指针状态对不上。帮我查一下成功扩容和分配失败路径是否都处理清楚？
**ReAct actions**: call_llm, grep_search, semantic_search, retrieve
**推断根因**:
- 问题中**没有反引号函数名**，LLM 需要从自然语言推断目标符号
- 检索到了同模块文件，但没命中具体 gold 文件

### posthoc_public_014 (配置/参数传播一致性)
**问题**: AI 改了聊天模板查找相关配置处理，我担心不同入口对默认模板、指定模板和缺失模板的返回含义理解不一致。帮我看现有路径是否还能按同一规则判断模板是否可用？
**Gold 文件 (3)**: `common/chat.cpp`, `common/common.cpp`, `src/llama-model.cpp`
**检索到的文件 (9)**: `common/arg.cpp`, `common/chat-auto-parser-helpers.cpp`, `common/chat-auto-parser-helpers.h`, `common/chat.cpp`, `src/llama-chat.cpp`, `src/llama-grammar.h`, `src/llama.cpp`, `tests/test-chat.cpp`, `tools/parser/template-analysis.cpp`
**缺失的文件**: `common/common.cpp`, `src/llama-model.cpp`
**检索覆盖率**: 33%

**初始检索 query**: AI 改了聊天模板查找相关配置处理，我担心不同入口对默认模板、指定模板和缺失模板的返回含义理解不一致。帮我看现有路径是否还能按同一规则判断模板是否可用？
**ReAct actions**: call_llm, expand_callees, expand_callers, retrieve
**推断根因**:
- 问题中**没有反引号函数名**，LLM 需要从自然语言推断目标符号
- **跨目录遗漏**: 命中了 `common` 下的文件，但遗漏了 `src/common` 下的文件

### posthoc_public_019 (配置/参数传播一致性)
**问题**: AI 改了模型加载默认参数初始化，我担心 common 加载、量化和底层适配入口拿到的初值来源分叉。帮我确认这些路径是否仍从同一套默认参数开始？
**Gold 文件 (4)**: `common/common.cpp`, `src/llama-model.cpp`, `src/llama-quant.cpp`, `src/llama.cpp`
**检索到的文件 (12)**: `common/common.cpp`, `common/debug.cpp`, `common/download.h`, `common/ngram-cache.cpp`, `src/llama-model.cpp`, `src/llama-quant.cpp`, `tests/export-graph-ops.cpp`, `tests/test-opt.cpp`, `tests/test-quant-type-selection.cpp`, `tools/mtmd/clip.cpp`, `tools/server/server-context.cpp`, `tools/server/server-context.h`
**缺失的文件**: `src/llama.cpp`
**检索覆盖率**: 75%

**初始检索 query**: AI 改了模型加载默认参数初始化，我担心 common 加载、量化和底层适配入口拿到的初值来源分叉。帮我确认这些路径是否仍从同一套默认参数开始？
**ReAct actions**: call_llm, grep_search, expand_callers, retrieve
**推断根因**:
- 问题中**没有反引号函数名**，LLM 需要从自然语言推断目标符号
- **同目录遗漏**: 同一目录下有多个相关文件，只命中了部分

### posthoc_public_020 (配置/参数传播一致性)
**问题**: AI 改了 sampler 名称解析和顺序处理，我担心命令行参数和 metadata fallback 会解析出不同的 sampler 列表或顺序。帮我看两个入口是否仍走同一套别名和顺序规则？
**Gold 文件 (3)**: `common/arg.cpp`, `common/common.cpp`, `common/sampling.cpp`
**检索到的文件 (20)**: `common/arg.h`, `common/common.cpp`, `common/common.h`, `common/llguidance.cpp`, `common/log.cpp`, `common/sampling.cpp`, `examples/simple/simple.cpp`, `include/llama.h`, `src/llama-context.cpp`, `src/llama-context.h`, `src/llama-grammar.h`, `src/llama-graph.h`, `src/llama-sampler.cpp`, `tests/test-backend-sampler.cpp`, `tests/test-grammar-llguidance.cpp`, `tests/test-sampling.cpp`, `tests/test-thread-safety.cpp`, `tools/mtmd/clip.cpp`, `tools/server/server-task.h`, `vendor/miniaudio/miniaudio.h`
**缺失的文件**: `common/arg.cpp`
**检索覆盖率**: 67%

**初始检索 query**: AI 改了 sampler 名称解析和顺序处理，我担心命令行参数和 metadata fallback 会解析出不同的 sampler 列表或顺序。帮我看两个入口是否仍走同一套别名和顺序规则？
**ReAct actions**: grep_search, retrieve, expand_callees, expand_callers, call_llm, semantic_search
**推断根因**:
- 问题中**没有反引号函数名**，LLM 需要从自然语言推断目标符号
- **同目录遗漏**: 同一目录下有多个相关文件，只命中了部分
- Gold 包含 `common/arg.cpp`（参数解析），但检索未命中——可能因为问题未直接提及'参数解析'或'arg'

### posthoc_public_021 (共享 helper 复用影响)
**问题**: AI 抽了一个通用空白裁剪 helper，我担心多个调用点对返回新字符串、空输入和无副作用的假设不一致。帮我看这些复用点是否仍能按同一契约使用它？
**Gold 文件 (2)**: `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`
**检索到的文件 (13)**: `common/chat-diff-analyzer.cpp`, `common/chat-peg-parser.h`, `examples/retrieval/retrieval.cpp`, `ggml/src/ggml-webgpu/pre_wgsl.hpp`, `src/llama-chat.cpp`, `src/llama-graph.cpp`, `src/llama-kv-cache.cpp`, `src/llama-model.cpp`, `src/llama-vocab.cpp`, `tests/test-chat-auto-parser.cpp`, `vendor/cpp-httplib/httplib.cpp`, `vendor/miniaudio/miniaudio.h`, `vendor/nlohmann/json.hpp`
**缺失的文件**: `common/chat-auto-parser-helpers.cpp`
**检索覆盖率**: 50%

**初始检索 query**: AI 抽了一个通用空白裁剪 helper，我担心多个调用点对返回新字符串、空输入和无副作用的假设不一致。帮我看这些复用点是否仍能按同一契约使用它？
**ReAct actions**: grep_search, retrieve, expand_callees, expand_callers, call_llm
**推断根因**:
- 问题中**没有反引号函数名**，LLM 需要从自然语言推断目标符号
- **同目录遗漏**: 同一目录下有多个相关文件，只命中了部分
- 问题涉及字符串 helper（trim/strip/whitespace），但 natural language 难以定位具体 helper 函数名

### posthoc_public_022 (共享 helper 复用影响)
**问题**: AI 抽了只处理前导空白的字符串 helper，我担心调用点误以为它会做完整 trim。帮我看当前使用点是否都只依赖“去掉开头空白、返回新字符串”的契约？
**Gold 文件 (2)**: `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`
**检索到的文件 (6)**: `common/chat-auto-parser-helpers.cpp`, `common/chat-auto-parser-helpers.h`, `common/chat-peg-parser.cpp`, `src/llama-model.cpp`, `tools/perplexity/perplexity.cpp`, `vendor/cpp-httplib/httplib.cpp`
**缺失的文件**: `common/chat-diff-analyzer.cpp`
**检索覆盖率**: 50%

**初始检索 query**: AI 抽了只处理前导空白的字符串 helper，我担心调用点误以为它会做完整 trim。帮我看当前使用点是否都只依赖“去掉开头空白、返回新字符串”的契约？
**ReAct actions**: call_llm, retrieve
**推断根因**:
- 问题中**没有反引号函数名**，LLM 需要从自然语言推断目标符号
- **同目录遗漏**: 同一目录下有多个相关文件，只命中了部分
- 问题涉及字符串 helper（trim/strip/whitespace），但 natural language 难以定位具体 helper 函数名

### posthoc_public_023 (共享 helper 复用影响)
**问题**: AI 抽了只处理尾部空白的字符串 helper，我担心空字符串和边界字符处理会影响多个复用点。帮我看这些调用点是否都按同一尾部裁剪契约使用返回值？
**Gold 文件 (2)**: `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`
**检索到的文件 (7)**: `common/chat-auto-parser-helpers.cpp`, `common/chat-auto-parser-helpers.h`, `common/chat-peg-parser.cpp`, `src/llama-model.cpp`, `tests/test-chat-auto-parser.cpp`, `tools/perplexity/perplexity.cpp`, `vendor/cpp-httplib/httplib.cpp`
**缺失的文件**: `common/chat-diff-analyzer.cpp`
**检索覆盖率**: 50%

**初始检索 query**: AI 抽了只处理尾部空白的字符串 helper，我担心空字符串和边界字符处理会影响多个复用点。帮我看这些调用点是否都按同一尾部裁剪契约使用返回值？
**ReAct actions**: call_llm, expand_callers, retrieve
**推断根因**:
- 问题中**没有反引号函数名**，LLM 需要从自然语言推断目标符号
- **同目录遗漏**: 同一目录下有多个相关文件，只命中了部分
- 问题涉及字符串 helper（trim/strip/whitespace），但 natural language 难以定位具体 helper 函数名

### posthoc_public_024 (共享 helper 复用影响)
**问题**: AI 抽了公共前缀截取 helper，我担心不同调用点对返回片段边界的理解不一样。帮我看它们是否都按相同的公共前缀语义使用结果？
**Gold 文件 (2)**: `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`
**检索到的文件 (17)**: `common/chat-auto-parser-helpers.cpp`, `common/chat-auto-parser.h`, `common/chat-peg-parser.h`, `common/log.cpp`, `common/peg-parser.cpp`, `examples/simple-chat/simple-chat.cpp`, `ggml/src/ggml-metal/ggml-metal-common.cpp`, `ggml/src/ggml-metal/ggml-metal-ops.cpp`, `ggml/src/gguf.cpp`, `src/llama-kv-cache.cpp`, `src/llama-model.cpp`, `src/llama-vocab.cpp`, `src/models/lfm2.cpp`, `tests/test-chat-auto-parser.cpp`, `tools/mtmd/debug/mtmd-debug.cpp`, `tools/mtmd/models/mobilenetv5.cpp`, `tools/server/server-common.cpp`
**缺失的文件**: `common/chat-diff-analyzer.cpp`
**检索覆盖率**: 50%

**初始检索 query**: AI 抽了公共前缀截取 helper，我担心不同调用点对返回片段边界的理解不一样。帮我看它们是否都按相同的公共前缀语义使用结果？
**ReAct actions**: call_llm, grep_search, semantic_search, retrieve
**推断根因**:
- 问题中**没有反引号函数名**，LLM 需要从自然语言推断目标符号
- **同目录遗漏**: 同一目录下有多个相关文件，只命中了部分
- 问题涉及字符串 helper（trim/strip/whitespace），但 natural language 难以定位具体 helper 函数名

### posthoc_public_028 (共享 helper 复用影响)
**问题**: AI 抽了模板差异拆分 helper，我担心左右输入和公共片段的返回结构在多个复用点里被不同方式解释。帮我看这些调用点是否仍按同一 diff 结构使用？
**Gold 文件 (3)**: `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`, `common/chat.cpp`
**检索到的文件 (11)**: `common/chat-auto-parser-helpers.cpp`, `common/chat-auto-parser-helpers.h`, `common/chat-auto-parser.h`, `common/chat-diff-analyzer.cpp`, `ggml/src/ggml-metal/ggml-metal-ops.cpp`, `ggml/src/ggml-quants.c`, `src/llama-model.cpp`, `tests/test-chat-auto-parser.cpp`, `tools/parser/template-analysis.cpp`, `vendor/cpp-httplib/httplib.cpp`, `vendor/nlohmann/json.hpp`
**缺失的文件**: `common/chat.cpp`
**检索覆盖率**: 67%

**初始检索 query**: AI 抽了模板差异拆分 helper，我担心左右输入和公共片段的返回结构在多个复用点里被不同方式解释。帮我看这些调用点是否仍按同一 diff 结构使用？
**ReAct actions**: read_class, grep_search, retrieve, expand_callers, call_llm
**推断根因**:
- 问题中**没有反引号函数名**，LLM 需要从自然语言推断目标符号
- **同目录遗漏**: 同一目录下有多个相关文件，只命中了部分
- 问题涉及字符串 helper（trim/strip/whitespace），但 natural language 难以定位具体 helper 函数名

### posthoc_public_031 (初始化/外部 API 契约)
**问题**: AI 生成了一个硬件后端初始化入口，我担心外部初始化调用、设备编号检查、context/backend 创建和失败返回之间没有形成一致契约。帮我看失败路径是否会按预期返回或清理？
**Gold 文件 (1)**: `ggml/src/ggml-cann/ggml-cann.cpp`
**检索到的文件 (15)**: `common/console.h`, `ggml/src/ggml-virtgpu/backend/backend-dispatched.cpp`, `ggml/src/ggml-virtgpu/backend/backend.cpp`, `ggml/src/ggml-virtgpu/backend/shared/api_remoting.h`, `ggml/src/ggml-virtgpu/backend/shared/apir_backend.h`, `ggml/src/ggml-virtgpu/ggml-backend-reg.cpp`, `ggml/src/ggml-zdnn/ggml-zdnn.cpp`, `tests/test-alloc.cpp`, `tests/test-backend-ops.cpp`, `tests/test-backend-sampler.cpp`, `tests/test-chat-template.cpp`, `tests/test-llama-archs.cpp`, `tests/test-thread-safety.cpp`, `tools/cli/cli.cpp`, `tools/server/server-context.cpp`
**缺失的文件**: `ggml/src/ggml-cann/ggml-cann.cpp`
**检索覆盖率**: 0%

**初始检索 query**: AI 生成了一个硬件后端初始化入口，我担心外部初始化调用、设备编号检查、context/backend 创建和失败返回之间没有形成一致契约。帮我看失败路径是否会按预期返回或清理？
**ReAct actions**: grep_search, retrieve, expand_callees, expand_callers, call_llm, semantic_search
**推断根因**:
- 问题中**没有反引号函数名**，LLM 需要从自然语言推断目标符号
- 检索到了同模块文件，但没命中具体 gold 文件
- Gold 在 `ggml-cann` 模块，但检索结果完全未涉及 CANN

### posthoc_public_034 (资源生命周期/清理边界)
**问题**: AI 改了后端释放入口，我担心空指针保护、具体后端释放分发和多个调用点的释放契约不一致。帮我看现有释放路径是否会出现漏释放或重复释放风险？
**Gold 文件 (4)**: `ggml/src/ggml-backend-meta.cpp`, `ggml/src/ggml-backend.cpp`, `ggml/src/ggml-rpc/ggml-rpc.cpp`, `src/llama-model-loader.cpp`
**检索到的文件 (9)**: `common/speculative.cpp`, `common/speculative.h`, `ggml/src/ggml-hexagon/ggml-hexagon.cpp`, `ggml/src/ggml-hexagon/htp/main.c`, `ggml/src/ggml-opencl/ggml-opencl.cpp`, `ggml/src/ggml-sycl/common.cpp`, `ggml/src/ggml-vulkan/vulkan-shaders/vulkan-shaders-gen.cpp`, `tests/test-alloc.cpp`, `tools/server/server-context.cpp`
**缺失的文件**: `ggml/src/ggml-backend-meta.cpp`, `ggml/src/ggml-backend.cpp`, `ggml/src/ggml-rpc/ggml-rpc.cpp`, `src/llama-model-loader.cpp`
**检索覆盖率**: 0%

**初始检索 query**: AI 改了后端释放入口，我担心空指针保护、具体后端释放分发和多个调用点的释放契约不一致。帮我看现有释放路径是否会出现漏释放或重复释放风险？
**ReAct actions**: call_llm, semantic_search, expand_callers, retrieve
**推断根因**:
- 问题中**没有反引号函数名**，LLM 需要从自然语言推断目标符号
- 检索到了同模块文件，但没命中具体 gold 文件

### posthoc_public_039 (状态一致性)
**问题**: AI 改了模型服务 endpoint 的状态处理，我担心环境变量读取、默认值和规范化结果在调用点里被当成不同状态。帮我看 reset/cache/context 相关假设是否会被破坏？
**Gold 文件 (3)**: `common/arg.cpp`, `common/common.cpp`, `common/hf-cache.cpp`
**检索到的文件 (14)**: `common/arg.cpp`, `common/common.cpp`, `examples/llama.android/lib/src/main/cpp/ai_chat.cpp`, `ggml/src/ggml-openvino/utils.cpp`, `ggml/src/ggml-rpc/ggml-rpc.cpp`, `src/llama-batch.cpp`, `src/llama-context.cpp`, `src/llama-memory-recurrent.cpp`, `tools/llama-bench/llama-bench.cpp`, `tools/server/server-context.cpp`, `tools/server/server-http.cpp`, `tools/server/server-models.cpp`, `tools/server/server.cpp`, `vendor/miniaudio/miniaudio.h`
**缺失的文件**: `common/hf-cache.cpp`
**检索覆盖率**: 67%

**初始检索 query**: AI 改了模型服务 endpoint 的状态处理，我担心环境变量读取、默认值和规范化结果在调用点里被当成不同状态。帮我看 reset/cache/context 相关假设是否会被破坏？
**ReAct actions**: read_class, grep_search, call_llm, retrieve
**推断根因**:
- 问题中**没有反引号函数名**，LLM 需要从自然语言推断目标符号
- **同目录遗漏**: 同一目录下有多个相关文件，只命中了部分

### posthoc_public_041 (并发/异步安全)
**问题**: AI 生成了一个 SYCL kernel 提交路径，我担心队列提交、目标 tensor 生命周期和上层 compute 调用顺序不一致。帮我看这条异步计算路径是否符合现有调度假设？
**Gold 文件 (2)**: `ggml/src/ggml-sycl/count-equal.cpp`, `ggml/src/ggml-sycl/ggml-sycl.cpp`
**检索到的文件 (13)**: `ggml/src/ggml-backend.cpp`, `ggml/src/ggml-sycl/add-id.cpp`, `ggml/src/ggml-sycl/element_wise.cpp`, `ggml/src/ggml-sycl/fattn-tile.hpp`, `ggml/src/ggml-sycl/fattn.cpp`, `ggml/src/ggml-sycl/ggml-sycl.cpp`, `ggml/src/ggml-sycl/im2col.cpp`, `ggml/src/ggml-sycl/mmq.cpp`, `ggml/src/ggml-sycl/pad_reflect_1d.cpp`, `ggml/src/ggml-sycl/roll.cpp`, `ggml/src/ggml-vulkan/ggml-vulkan.cpp`, `src/llama-context.cpp`, `src/llama-model.cpp`
**缺失的文件**: `ggml/src/ggml-sycl/count-equal.cpp`
**检索覆盖率**: 50%

**初始检索 query**: AI 生成了一个 SYCL kernel 提交路径，我担心队列提交、目标 tensor 生命周期和上层 compute 调用顺序不一致。帮我看这条异步计算路径是否符合现有调度假设？
**ReAct actions**: call_llm, grep_search, expand_callers, retrieve
**推断根因**:
- 问题中**没有反引号函数名**，LLM 需要从自然语言推断目标符号
- **同目录遗漏**: 同一目录下有多个相关文件，只命中了部分

### posthoc_public_044 (失败返回语义)
**问题**: AI 生成了上下文初始化入口，我担心多个 nullptr 早退分支混在一起，调用方难以区分是哪类前置条件失败。帮我看这些早退路径是否分别对应清楚的校验条件？
**Gold 文件 (1)**: `src/llama-context.cpp`
**检索到的文件 (12)**: `common/chat-auto-parser.h`, `common/common.cpp`, `ggml/src/ggml-virtgpu/backend/shared/apir_backend.h`, `src/llama-grammar.cpp`, `src/llama-impl.h`, `src/llama-memory-hybrid-iswa.cpp`, `tests/test-chat-auto-parser.cpp`, `tests/test-grammar-integration.cpp`, `tests/test-grammar-parser.cpp`, `tests/test-opt.cpp`, `tests/test-reasoning-budget.cpp`, `tools/server/server-context.cpp`
**缺失的文件**: `src/llama-context.cpp`
**检索覆盖率**: 0%

**初始检索 query**: AI 生成了上下文初始化入口，我担心多个 nullptr 早退分支混在一起，调用方难以区分是哪类前置条件失败。帮我看这些早退路径是否分别对应清楚的校验条件？
**ReAct actions**: read_class, grep_search, retrieve, expand_callers, call_llm, semantic_search
**推断根因**:
- 问题中**没有反引号函数名**，LLM 需要从自然语言推断目标符号
- 检索到了同模块文件，但没命中具体 gold 文件

### posthoc_public_045 (失败返回语义)
**问题**: AI 改了 CPU 范围字符串解析逻辑，我担心非法格式、起止下标越界和解析异常没有被调用方按错误处理契约接住。帮我看这些失败返回是否会被参数解析路径正确处理？
**Gold 文件 (2)**: `common/arg.cpp`, `common/common.cpp`
**检索到的文件 (7)**: `common/chat-auto-parser.h`, `common/common.cpp`, `tests/test-backend-sampler.cpp`, `tests/test-thread-safety.cpp`, `tools/llama-bench/llama-bench.cpp`, `tools/parser/debug-template-parser.cpp`, `tools/parser/template-analysis.cpp`
**缺失的文件**: `common/arg.cpp`
**检索覆盖率**: 50%

**初始检索 query**: AI 改了 CPU 范围字符串解析逻辑，我担心非法格式、起止下标越界和解析异常没有被调用方按错误处理契约接住。帮我看这些失败返回是否会被参数解析路径正确处理？
**ReAct actions**: grep_search, retrieve, expand_callees, expand_callers, call_llm
**推断根因**:
- 问题中**没有反引号函数名**，LLM 需要从自然语言推断目标符号
- **同目录遗漏**: 同一目录下有多个相关文件，只命中了部分
- Gold 包含 `common/arg.cpp`（参数解析），但检索未命中——可能因为问题未直接提及'参数解析'或'arg'

## 根因分类汇总

| 根因 | 题数 |
|------|------|
| 问题无显式符号名 | 21 |
| 部分覆盖（同模块遗漏） | 16 |
| 模块完全跑偏（0%覆盖） | 5 |
| 字符串 helper 定位困难 | 5 |
| CANN 模块映射失败 | 2 |
| SYCL 模块映射失败 | 1 |

## 核心结论

1. **问题无显式符号名是最大障碍**: Hard benchmark 的问题用自然语言描述场景，没有反引号函数名，Symbol Fast Path 无法直接 grep 定位。
2. **模块跑偏是第二杀手**: 5 题覆盖率 0%，检索系统被关键词误导到完全不同的模块（如 SYCL→CANN、backend→hexagon/virtgpu）。
3. **字符串 helper 最难定位**: 涉及 trim/strip/whitespace 的问题，gold 在 chat-auto-parser-helpers.cpp，但自然语言难以推断这个具体文件名。
4. **common/arg.cpp 系统性遗漏**: 多题 gold 包含 arg.cpp（参数解析），但问题描述通常不提'参数解析'，导致检索失败。
5. **跨目录配对遗漏**: 同一功能分散在多个目录（如 common/ + src/ + ggml/），检索只命中部分目录。