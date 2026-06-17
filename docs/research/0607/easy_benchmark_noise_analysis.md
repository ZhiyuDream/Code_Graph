# Easy Benchmark 噪声分析

**结果文件**: `benchmark_symbol_fastpath_20260607_131010.json`

**统计口径**: 排除 `.h` / `.hpp` 头文件，只统计 `.cpp` / `.c` 文件

## 总览

- 总题数: 50 题
- 总检索文件数: 297
- 总噪声文件数: 189
- **整体噪声占比: 63.6%**
- 平均每题检索: 5.9 文件
- 平均每题噪声: 3.8 文件

### 噪声占比分布

| 噪声占比区间 | 题目数 |
|-------------|-------|
| 0% (无噪声) | 8 |
| 1%-20% | 0 |
| 21%-40% | 10 |
| 41%-60% | 14 |
| 61%-80% | 13 |
| 81%-100% | 5 |

## 逐题噪声分析

| QID | Gold | 检索 | 相关 | 噪声 | 噪声% | 缺失Gold | 搜到未引 |
|-----|------|------|------|------|-------|----------|----------|
| posthoc_audit_001 | 3 | 8 | 3 | 5 | 62% | - | ggml/src/ggml-sycl/common.c... |
| posthoc_audit_002 | 3 | 11 | 3 | 8 | 73% | - | common/chat.cpp, common/com... |
| posthoc_audit_003 | 4 | 18 | 4 | 14 | 78% | - | common/common.cpp, src/llam... |
| posthoc_audit_004 | 2 | 4 | 2 | 2 | 50% | - | common/chat-auto-parser-hel... |
| posthoc_audit_005 | 2 | 4 | 2 | 2 | 50% | - | - |
| posthoc_audit_006 | 2 | 2 | 2 | 0 | 0% | - | common/chat-auto-parser-hel... |
| posthoc_audit_007 | 3 | 5 | 3 | 2 | 40% | - | common/chat-auto-parser-hel... |
| posthoc_audit_008 | 2 | 5 | 2 | 3 | 60% | - | common/chat-auto-parser-hel... |
| posthoc_audit_009 | 2 | 6 | 2 | 4 | 67% | - | common/chat-auto-parser-hel... |
| posthoc_audit_010 | 2 | 3 | 2 | 1 | 33% | - | ggml/src/ggml-backend-reg.c... |
| posthoc_audit_011 | 3 | 17 | 3 | 14 | 82% | - | common/sampling.cpp, common... |
| posthoc_audit_012 | 3 | 15 | 3 | 12 | 80% | - | common/common.cpp |
| posthoc_audit_013 | 1 | 4 | 1 | 3 | 75% | - | ggml/src/ggml-cann/aclnn_op... |
| posthoc_audit_014 | 3 | 7 | 3 | 4 | 57% | - | common/chat.cpp, common/com... |
| posthoc_audit_015 | 3 | 4 | 3 | 1 | 25% | - | common/arg.cpp, common/comm... |
| posthoc_audit_016 | 3 | 3 | 3 | 0 | 0% | - | common/arg.cpp, common/down... |
| posthoc_audit_017 | 2 | 5 | 2 | 3 | 60% | - | - |
| posthoc_audit_018 | 3 | 4 | 3 | 1 | 25% | - | common/arg.cpp, common/comm... |
| posthoc_audit_019 | 4 | 22 | 4 | 18 | 82% | - | - |
| posthoc_audit_020 | 3 | 6 | 2 | 4 | 67% | common/arg.cpp | common/common.cpp, common/s... |
| posthoc_audit_021 | 2 | 3 | 2 | 1 | 33% | - | common/chat-auto-parser-hel... |
| posthoc_audit_022 | 2 | 2 | 2 | 0 | 0% | - | common/chat-auto-parser-hel... |
| posthoc_audit_023 | 2 | 2 | 2 | 0 | 0% | - | common/chat-auto-parser-hel... |
| posthoc_audit_024 | 2 | 3 | 2 | 1 | 33% | - | common/chat-auto-parser-hel... |
| posthoc_audit_025 | 2 | 3 | 2 | 1 | 33% | - | common/chat-auto-parser-hel... |
| posthoc_audit_026 | 2 | 2 | 2 | 0 | 0% | - | common/chat-auto-parser-hel... |
| posthoc_audit_027 | 2 | 6 | 2 | 4 | 67% | - | common/chat-auto-parser-gen... |
| posthoc_audit_028 | 3 | 5 | 3 | 2 | 40% | - | common/chat-auto-parser-hel... |
| posthoc_audit_029 | 2 | 2 | 2 | 0 | 0% | - | common/arg.cpp, common/pres... |
| posthoc_audit_030 | 2 | 3 | 2 | 1 | 33% | - | - |
| posthoc_audit_031 | 1 | 2 | 1 | 1 | 50% | - | ggml/src/ggml-cann/ggml-can... |
| posthoc_audit_032 | 1 | 2 | 1 | 1 | 50% | - | ggml/src/ggml-webgpu/ggml-w... |
| posthoc_audit_033 | 1 | 5 | 1 | 4 | 80% | - | src/llama-sampler.cpp |
| posthoc_audit_034 | 4 | 10 | 4 | 6 | 60% | - | ggml/src/ggml-backend-meta.... |
| posthoc_audit_035 | 2 | 5 | 2 | 3 | 60% | - | common/chat-auto-parser-hel... |
| posthoc_audit_036 | 1 | 4 | 1 | 3 | 75% | - | common/ngram-map.cpp |
| posthoc_audit_037 | 2 | 4 | 2 | 2 | 50% | - | common/arg.cpp |
| posthoc_audit_038 | 1 | 6 | 1 | 5 | 83% | - | common/arg.cpp |
| posthoc_audit_039 | 3 | 6 | 3 | 3 | 50% | - | - |
| posthoc_audit_040 | 3 | 10 | 3 | 7 | 70% | - | common/chat.cpp, common/com... |
| posthoc_audit_041 | 2 | 6 | 2 | 4 | 67% | - | - |
| posthoc_audit_042 | 2 | 5 | 2 | 3 | 60% | - | ggml/src/ggml-sycl/ggml-syc... |
| posthoc_audit_043 | 2 | 4 | 2 | 2 | 50% | - | common/arg.cpp, common/down... |
| posthoc_audit_044 | 1 | 25 | 1 | 24 | 96% | - | src/llama-context.cpp |
| posthoc_audit_045 | 2 | 2 | 1 | 1 | 50% | common/arg.cpp | common/common.cpp |
| posthoc_audit_046 | 1 | 3 | 1 | 2 | 67% | - | common/common.cpp |
| posthoc_audit_047 | 2 | 2 | 2 | 0 | 0% | - | common/arg.cpp, common/comm... |
| posthoc_audit_048 | 3 | 4 | 3 | 1 | 25% | - | common/arg.cpp, common/comm... |
| posthoc_audit_049 | 1 | 1 | 1 | 0 | 0% | - | - |
| posthoc_audit_050 | 1 | 7 | 1 | 6 | 86% | - | src/llama-sampler.cpp |

## 引用缺失题目详细分析（含噪声上下文）

### posthoc_audit_001
- Gold files (3): `ggml/src/ggml-sycl/common.cpp`, `ggml/src/ggml-sycl/cpy.cpp`, `ggml/src/ggml-sycl/element_wise.cpp`
- Retrieved files (8): `examples/sycl/ls-sycl-device.cpp`, `ggml/src/ggml-sycl/common.cpp`, `ggml/src/ggml-sycl/common.hpp`, `ggml/src/ggml-sycl/cpy.cpp`, `ggml/src/ggml-sycl/element_wise.cpp`, `ggml/src/ggml-sycl/fattn.cpp`, `ggml/src/ggml-sycl/ggml-sycl.cpp`, `ggml/src/ggml-sycl/norm.cpp`
- Noise files (5): `examples/sycl/ls-sycl-device.cpp`, `ggml/src/ggml-sycl/common.hpp`, `ggml/src/ggml-sycl/fattn.cpp`, `ggml/src/ggml-sycl/ggml-sycl.cpp`, `ggml/src/ggml-sycl/norm.cpp`
- **噪声占比: 62%**
- ⚠️ **检索到但未引用**: `ggml/src/ggml-sycl/common.cpp`, `ggml/src/ggml-sycl/cpy.cpp`, `ggml/src/ggml-sycl/element_wise.cpp`

### posthoc_audit_002
- Gold files (3): `common/chat.cpp`, `common/common.cpp`, `src/llama-model.cpp`
- Retrieved files (11): `common/chat.cpp`, `common/common.cpp`, `common/peg-parser.cpp`, `examples/embedding/embedding.cpp`, `examples/simple-chat/simple-chat.cpp`, `src/llama-arch.h`, `src/llama-grammar.h`, `src/llama-model.cpp`, `src/llama-vocab.h`, `tools/mtmd/mtmd-cli.cpp`, `tools/server/server-common.cpp`
- Noise files (8): `common/peg-parser.cpp`, `examples/embedding/embedding.cpp`, `examples/simple-chat/simple-chat.cpp`, `src/llama-arch.h`, `src/llama-grammar.h`, `src/llama-vocab.h`, `tools/mtmd/mtmd-cli.cpp`, `tools/server/server-common.cpp`
- **噪声占比: 73%**
- ⚠️ **检索到但未引用**: `common/chat.cpp`, `common/common.cpp`, `src/llama-model.cpp`

### posthoc_audit_003
- Gold files (4): `common/common.cpp`, `src/llama-model.cpp`, `src/llama-quant.cpp`, `src/llama.cpp`
- Retrieved files (18): `common/common.cpp`, `examples/diffusion/diffusion-cli.cpp`, `examples/llama.android/lib/src/main/cpp/ai_chat.cpp`, `examples/simple-chat/simple-chat.cpp`, `examples/simple/simple.cpp`, `src/llama-model.cpp`, `src/llama-quant.cpp`, `src/llama.cpp`, `tests/export-graph-ops.cpp`, `tests/test-autorelease.cpp`, `tests/test-backend-sampler.cpp`, `tests/test-grammar-llguidance.cpp`, `tests/test-llama-archs.cpp`, `tests/test-quantize-stats.cpp`, `tests/test-tokenizer-0.cpp`, `tests/test-tokenizer-1-bpe.cpp`, `tests/test-tokenizer-1-spm.cpp`, `tools/llama-bench/llama-bench.cpp`
- Noise files (14): `examples/diffusion/diffusion-cli.cpp`, `examples/llama.android/lib/src/main/cpp/ai_chat.cpp`, `examples/simple-chat/simple-chat.cpp`, `examples/simple/simple.cpp`, `tests/export-graph-ops.cpp`, `tests/test-autorelease.cpp`, `tests/test-backend-sampler.cpp`, `tests/test-grammar-llguidance.cpp`, `tests/test-llama-archs.cpp`, `tests/test-quantize-stats.cpp`, `tests/test-tokenizer-0.cpp`, `tests/test-tokenizer-1-bpe.cpp`, `tests/test-tokenizer-1-spm.cpp`, `tools/llama-bench/llama-bench.cpp`
- **噪声占比: 78%**
- ⚠️ **检索到但未引用**: `common/common.cpp`, `src/llama-model.cpp`, `src/llama-quant.cpp`, `src/llama.cpp`

### posthoc_audit_004
- Gold files (2): `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`
- Retrieved files (4): `common/chat-auto-parser-helpers.cpp`, `common/chat-auto-parser.h`, `common/chat-diff-analyzer.cpp`, `common/jinja/string.h`
- Noise files (2): `common/chat-auto-parser.h`, `common/jinja/string.h`
- **噪声占比: 50%**
- ⚠️ **检索到但未引用**: `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`

### posthoc_audit_006
- Gold files (2): `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`
- Retrieved files (2): `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`
- Noise files (0): ``
- **噪声占比: 0%**
- ⚠️ **检索到但未引用**: `common/chat-auto-parser-helpers.cpp`

### posthoc_audit_007
- Gold files (3): `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`, `common/chat.cpp`
- Retrieved files (5): `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`, `common/chat.cpp`, `tests/test-chat-auto-parser.cpp`, `tools/parser/template-analysis.cpp`
- Noise files (2): `tests/test-chat-auto-parser.cpp`, `tools/parser/template-analysis.cpp`
- **噪声占比: 40%**
- ⚠️ **检索到但未引用**: `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`, `common/chat.cpp`

### posthoc_audit_008
- Gold files (2): `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`
- Retrieved files (5): `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`, `common/jinja/string.h`, `src/llama-grammar.h`, `tests/test-chat-auto-parser.cpp`
- Noise files (3): `common/jinja/string.h`, `src/llama-grammar.h`, `tests/test-chat-auto-parser.cpp`
- **噪声占比: 60%**
- ⚠️ **检索到但未引用**: `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`

### posthoc_audit_009
- Gold files (2): `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`
- Retrieved files (6): `common/chat-auto-parser-helpers.cpp`, `common/chat-auto-parser.h`, `common/chat-diff-analyzer.cpp`, `common/jinja/string.h`, `tests/test-chat-auto-parser.cpp`, `tests/testing.h`
- Noise files (4): `common/chat-auto-parser.h`, `common/jinja/string.h`, `tests/test-chat-auto-parser.cpp`, `tests/testing.h`
- **噪声占比: 67%**
- ⚠️ **检索到但未引用**: `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`

### posthoc_audit_010
- Gold files (2): `ggml/src/ggml-backend-reg.cpp`, `ggml/src/ggml-cann/ggml-cann.cpp`
- Retrieved files (3): `ggml/include/ggml-cann.h`, `ggml/src/ggml-backend-reg.cpp`, `ggml/src/ggml-cann/ggml-cann.cpp`
- Noise files (1): `ggml/include/ggml-cann.h`
- **噪声占比: 33%**
- ⚠️ **检索到但未引用**: `ggml/src/ggml-backend-reg.cpp`, `ggml/src/ggml-cann/ggml-cann.cpp`

### posthoc_audit_011
- Gold files (3): `common/common.cpp`, `common/sampling.cpp`, `common/speculative.cpp`
- Retrieved files (17): `common/common.cpp`, `common/common.h`, `common/log.cpp`, `common/sampling.cpp`, `common/speculative.cpp`, `examples/llama.android/lib/src/main/cpp/ai_chat.cpp`, `examples/lookahead/lookahead.cpp`, `examples/lookup/lookup.cpp`, `examples/parallel/parallel.cpp`, `examples/speculative-simple/speculative-simple.cpp`, `examples/speculative/speculative.cpp`, `ggml/src/ggml.c`, `src/llama-sampler.cpp`, `tests/test-thread-safety.cpp`, `tools/mtmd/mtmd-cli.cpp`, `tools/server/server-context.cpp`, `tools/tts/tts.cpp`
- Noise files (14): `common/common.h`, `common/log.cpp`, `examples/llama.android/lib/src/main/cpp/ai_chat.cpp`, `examples/lookahead/lookahead.cpp`, `examples/lookup/lookup.cpp`, `examples/parallel/parallel.cpp`, `examples/speculative-simple/speculative-simple.cpp`, `examples/speculative/speculative.cpp`, `ggml/src/ggml.c`, `src/llama-sampler.cpp`, `tests/test-thread-safety.cpp`, `tools/mtmd/mtmd-cli.cpp`, `tools/server/server-context.cpp`, `tools/tts/tts.cpp`
- **噪声占比: 82%**
- ⚠️ **检索到但未引用**: `common/sampling.cpp`, `common/speculative.cpp`

### posthoc_audit_012
- Gold files (3): `common/chat.cpp`, `common/common.cpp`, `src/llama-model.cpp`
- Retrieved files (15): `common/chat.cpp`, `common/common.cpp`, `common/log.cpp`, `common/peg-parser.cpp`, `examples/embedding/embedding.cpp`, `examples/simple-chat/simple-chat.cpp`, `ggml/src/ggml.c`, `src/llama-arch.h`, `src/llama-grammar.h`, `src/llama-model.cpp`, `src/llama-vocab.cpp`, `src/llama-vocab.h`, `tests/test-chat.cpp`, `tools/mtmd/mtmd-cli.cpp`, `tools/server/server-common.cpp`
- Noise files (12): `common/log.cpp`, `common/peg-parser.cpp`, `examples/embedding/embedding.cpp`, `examples/simple-chat/simple-chat.cpp`, `ggml/src/ggml.c`, `src/llama-arch.h`, `src/llama-grammar.h`, `src/llama-vocab.cpp`, `src/llama-vocab.h`, `tests/test-chat.cpp`, `tools/mtmd/mtmd-cli.cpp`, `tools/server/server-common.cpp`
- **噪声占比: 80%**
- ⚠️ **检索到但未引用**: `common/common.cpp`

### posthoc_audit_013
- Gold files (1): `ggml/src/ggml-cann/aclnn_ops.cpp`
- Retrieved files (4): `ggml/include/ggml.h`, `ggml/src/ggml-cann/aclnn_ops.cpp`, `ggml/src/ggml-cann/common.h`, `ggml/src/ggml-cann/ggml-cann.cpp`
- Noise files (3): `ggml/include/ggml.h`, `ggml/src/ggml-cann/common.h`, `ggml/src/ggml-cann/ggml-cann.cpp`
- **噪声占比: 75%**
- ⚠️ **检索到但未引用**: `ggml/src/ggml-cann/aclnn_ops.cpp`

### posthoc_audit_014
- Gold files (3): `common/chat.cpp`, `common/common.cpp`, `src/llama-model.cpp`
- Retrieved files (7): `common/chat.cpp`, `common/common.cpp`, `examples/embedding/embedding.cpp`, `examples/simple-chat/simple-chat.cpp`, `src/llama-model.cpp`, `tools/mtmd/mtmd-cli.cpp`, `tools/server/server-common.cpp`
- Noise files (4): `examples/embedding/embedding.cpp`, `examples/simple-chat/simple-chat.cpp`, `tools/mtmd/mtmd-cli.cpp`, `tools/server/server-common.cpp`
- **噪声占比: 57%**
- ⚠️ **检索到但未引用**: `common/chat.cpp`, `common/common.cpp`, `src/llama-model.cpp`

### posthoc_audit_015
- Gold files (3): `common/arg.cpp`, `common/common.cpp`, `common/hf-cache.cpp`
- Retrieved files (4): `common/arg.cpp`, `common/common.cpp`, `common/hf-cache.cpp`, `common/peg-parser.h`
- Noise files (1): `common/peg-parser.h`
- **噪声占比: 25%**
- ⚠️ **检索到但未引用**: `common/arg.cpp`, `common/common.cpp`, `common/hf-cache.cpp`

### posthoc_audit_016
- Gold files (3): `common/arg.cpp`, `common/download.cpp`, `common/preset.cpp`
- Retrieved files (3): `common/arg.cpp`, `common/download.cpp`, `common/preset.cpp`
- Noise files (0): ``
- **噪声占比: 0%**
- ⚠️ **检索到但未引用**: `common/arg.cpp`, `common/download.cpp`, `common/preset.cpp`

### posthoc_audit_018
- Gold files (3): `common/arg.cpp`, `common/common.cpp`, `common/hf-cache.cpp`
- Retrieved files (4): `common/arg.cpp`, `common/common.cpp`, `common/hf-cache.cpp`, `common/peg-parser.h`
- Noise files (1): `common/peg-parser.h`
- **噪声占比: 25%**
- ⚠️ **检索到但未引用**: `common/arg.cpp`, `common/common.cpp`, `common/hf-cache.cpp`

### posthoc_audit_020
- Gold files (3): `common/arg.cpp`, `common/common.cpp`, `common/sampling.cpp`
- Retrieved files (6): `common/common.cpp`, `common/log.cpp`, `common/sampling.cpp`, `include/llama.h`, `src/llama-grammar.h`, `tools/server/server-task.cpp`
- Noise files (4): `common/log.cpp`, `include/llama.h`, `src/llama-grammar.h`, `tools/server/server-task.cpp`
- **噪声占比: 67%**
- ❌ **未检索到**: `common/arg.cpp`
- ⚠️ **检索到但未引用**: `common/common.cpp`, `common/sampling.cpp`

### posthoc_audit_021
- Gold files (2): `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`
- Retrieved files (3): `common/chat-auto-parser-helpers.cpp`, `common/chat-auto-parser.h`, `common/chat-diff-analyzer.cpp`
- Noise files (1): `common/chat-auto-parser.h`
- **噪声占比: 33%**
- ⚠️ **检索到但未引用**: `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`

### posthoc_audit_022
- Gold files (2): `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`
- Retrieved files (2): `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`
- Noise files (0): ``
- **噪声占比: 0%**
- ⚠️ **检索到但未引用**: `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`

### posthoc_audit_023
- Gold files (2): `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`
- Retrieved files (2): `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`
- Noise files (0): ``
- **噪声占比: 0%**
- ⚠️ **检索到但未引用**: `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`

### posthoc_audit_024
- Gold files (2): `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`
- Retrieved files (3): `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`, `tests/test-chat-auto-parser.cpp`
- Noise files (1): `tests/test-chat-auto-parser.cpp`
- **噪声占比: 33%**
- ⚠️ **检索到但未引用**: `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`

### posthoc_audit_025
- Gold files (2): `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`
- Retrieved files (3): `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`, `tests/test-chat-auto-parser.cpp`
- Noise files (1): `tests/test-chat-auto-parser.cpp`
- **噪声占比: 33%**
- ⚠️ **检索到但未引用**: `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`

### posthoc_audit_026
- Gold files (2): `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`
- Retrieved files (2): `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`
- Noise files (0): ``
- **噪声占比: 0%**
- ⚠️ **检索到但未引用**: `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`

### posthoc_audit_027
- Gold files (2): `common/chat-auto-parser-generator.cpp`, `common/chat.cpp`
- Retrieved files (6): `common/chat-auto-parser-generator.cpp`, `common/chat-peg-parser.h`, `common/chat.cpp`, `common/chat.h`, `tests/test-chat-auto-parser.cpp`, `tests/test-chat-peg-parser.cpp`
- Noise files (4): `common/chat-peg-parser.h`, `common/chat.h`, `tests/test-chat-auto-parser.cpp`, `tests/test-chat-peg-parser.cpp`
- **噪声占比: 67%**
- ⚠️ **检索到但未引用**: `common/chat-auto-parser-generator.cpp`

### posthoc_audit_028
- Gold files (3): `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`, `common/chat.cpp`
- Retrieved files (5): `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`, `common/chat.cpp`, `tests/test-chat-auto-parser.cpp`, `tools/parser/template-analysis.cpp`
- Noise files (2): `tests/test-chat-auto-parser.cpp`, `tools/parser/template-analysis.cpp`
- **噪声占比: 40%**
- ⚠️ **检索到但未引用**: `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`, `common/chat.cpp`

### posthoc_audit_029
- Gold files (2): `common/arg.cpp`, `common/preset.cpp`
- Retrieved files (2): `common/arg.cpp`, `common/preset.cpp`
- Noise files (0): ``
- **噪声占比: 0%**
- ⚠️ **检索到但未引用**: `common/arg.cpp`, `common/preset.cpp`

### posthoc_audit_031
- Gold files (1): `ggml/src/ggml-cann/ggml-cann.cpp`
- Retrieved files (2): `ggml/src/ggml-cann/ggml-cann.cpp`, `ggml/src/ggml.c`
- Noise files (1): `ggml/src/ggml.c`
- **噪声占比: 50%**
- ⚠️ **检索到但未引用**: `ggml/src/ggml-cann/ggml-cann.cpp`

### posthoc_audit_032
- Gold files (1): `ggml/src/ggml-webgpu/ggml-webgpu.cpp`
- Retrieved files (2): `ggml/src/ggml-backend-reg.cpp`, `ggml/src/ggml-webgpu/ggml-webgpu.cpp`
- Noise files (1): `ggml/src/ggml-backend-reg.cpp`
- **噪声占比: 50%**
- ⚠️ **检索到但未引用**: `ggml/src/ggml-webgpu/ggml-webgpu.cpp`

### posthoc_audit_033
- Gold files (1): `src/llama-sampler.cpp`
- Retrieved files (5): `ggml/src/ggml-backend.cpp`, `ggml/src/ggml-sycl/ggml-sycl.cpp`, `ggml/src/ggml.c`, `src/llama-impl.cpp`, `src/llama-sampler.cpp`
- Noise files (4): `ggml/src/ggml-backend.cpp`, `ggml/src/ggml-sycl/ggml-sycl.cpp`, `ggml/src/ggml.c`, `src/llama-impl.cpp`
- **噪声占比: 80%**
- ⚠️ **检索到但未引用**: `src/llama-sampler.cpp`

### posthoc_audit_034
- Gold files (4): `ggml/src/ggml-backend-meta.cpp`, `ggml/src/ggml-backend.cpp`, `ggml/src/ggml-rpc/ggml-rpc.cpp`, `src/llama-model-loader.cpp`
- Retrieved files (10): `ggml/src/ggml-backend-meta.cpp`, `ggml/src/ggml-backend.cpp`, `ggml/src/ggml-rpc/ggml-rpc.cpp`, `src/llama-model-loader.cpp`, `tests/test-backend-ops.cpp`, `tests/test-gguf.cpp`, `tests/test-opt.cpp`, `tools/cvector-generator/pca.hpp`, `tools/export-lora/export-lora.cpp`, `tools/mtmd/clip.cpp`
- Noise files (6): `tests/test-backend-ops.cpp`, `tests/test-gguf.cpp`, `tests/test-opt.cpp`, `tools/cvector-generator/pca.hpp`, `tools/export-lora/export-lora.cpp`, `tools/mtmd/clip.cpp`
- **噪声占比: 60%**
- ⚠️ **检索到但未引用**: `ggml/src/ggml-backend-meta.cpp`, `ggml/src/ggml-backend.cpp`, `ggml/src/ggml-rpc/ggml-rpc.cpp`, `src/llama-model-loader.cpp`

### posthoc_audit_035
- Gold files (2): `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`
- Retrieved files (5): `common/chat-auto-parser-helpers.cpp`, `common/chat-auto-parser.h`, `common/chat-diff-analyzer.cpp`, `common/jinja/string.h`, `tests/test-chat-auto-parser.cpp`
- Noise files (3): `common/chat-auto-parser.h`, `common/jinja/string.h`, `tests/test-chat-auto-parser.cpp`
- **噪声占比: 60%**
- ⚠️ **检索到但未引用**: `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`

### posthoc_audit_036
- Gold files (1): `common/ngram-map.cpp`
- Retrieved files (4): `common/ngram-map.cpp`, `common/ngram-map.h`, `common/speculative.cpp`, `vendor/cpp-httplib/httplib.cpp`
- Noise files (3): `common/ngram-map.h`, `common/speculative.cpp`, `vendor/cpp-httplib/httplib.cpp`
- **噪声占比: 75%**
- ⚠️ **检索到但未引用**: `common/ngram-map.cpp`

### posthoc_audit_037
- Gold files (2): `common/arg.cpp`, `common/chat.cpp`
- Retrieved files (4): `common/arg.cpp`, `common/chat.cpp`, `common/log.cpp`, `src/llama.cpp`
- Noise files (2): `common/log.cpp`, `src/llama.cpp`
- **噪声占比: 50%**
- ⚠️ **检索到但未引用**: `common/arg.cpp`

### posthoc_audit_038
- Gold files (1): `common/arg.cpp`
- Retrieved files (6): `common/arg.cpp`, `common/download.cpp`, `tests/test-quant-type-selection.cpp`, `tests/test-thread-safety.cpp`, `tools/llama-bench/llama-bench.cpp`, `tools/server/server-context.cpp`
- Noise files (5): `common/download.cpp`, `tests/test-quant-type-selection.cpp`, `tests/test-thread-safety.cpp`, `tools/llama-bench/llama-bench.cpp`, `tools/server/server-context.cpp`
- **噪声占比: 83%**
- ⚠️ **检索到但未引用**: `common/arg.cpp`

### posthoc_audit_040
- Gold files (3): `common/chat.cpp`, `common/common.cpp`, `src/llama-model.cpp`
- Retrieved files (10): `common/arg.cpp`, `common/chat.cpp`, `common/common.cpp`, `examples/embedding/embedding.cpp`, `examples/llama.android/lib/src/main/cpp/ai_chat.cpp`, `examples/simple-chat/simple-chat.cpp`, `src/llama-model.cpp`, `tools/cli/cli.cpp`, `tools/mtmd/mtmd-cli.cpp`, `tools/server/server-common.cpp`
- Noise files (7): `common/arg.cpp`, `examples/embedding/embedding.cpp`, `examples/llama.android/lib/src/main/cpp/ai_chat.cpp`, `examples/simple-chat/simple-chat.cpp`, `tools/cli/cli.cpp`, `tools/mtmd/mtmd-cli.cpp`, `tools/server/server-common.cpp`
- **噪声占比: 70%**
- ⚠️ **检索到但未引用**: `common/chat.cpp`, `common/common.cpp`, `src/llama-model.cpp`

### posthoc_audit_042
- Gold files (2): `ggml/src/ggml-sycl/ggml-sycl.cpp`, `ggml/src/ggml-sycl/set.cpp`
- Retrieved files (5): `ggml/include/ggml.h`, `ggml/src/ggml-sycl/ggml-sycl.cpp`, `ggml/src/ggml-sycl/set.cpp`, `ggml/src/ggml-sycl/set_rows.cpp`, `ggml/src/ggml-vulkan/ggml-vulkan.cpp`
- Noise files (3): `ggml/include/ggml.h`, `ggml/src/ggml-sycl/set_rows.cpp`, `ggml/src/ggml-vulkan/ggml-vulkan.cpp`
- **噪声占比: 60%**
- ⚠️ **检索到但未引用**: `ggml/src/ggml-sycl/ggml-sycl.cpp`, `ggml/src/ggml-sycl/set.cpp`

### posthoc_audit_043
- Gold files (2): `common/arg.cpp`, `common/download.cpp`
- Retrieved files (4): `common/arg.cpp`, `common/download.cpp`, `common/download.h`, `tools/llama-bench/llama-bench.cpp`
- Noise files (2): `common/download.h`, `tools/llama-bench/llama-bench.cpp`
- **噪声占比: 50%**
- ⚠️ **检索到但未引用**: `common/arg.cpp`, `common/download.cpp`

### posthoc_audit_044
- Gold files (1): `src/llama-context.cpp`
- Retrieved files (25): `common/common.cpp`, `common/speculative.cpp`, `examples/batched/batched.cpp`, `examples/diffusion/diffusion-cli.cpp`, `examples/idle/idle.cpp`, `examples/llama.android/lib/src/main/cpp/ai_chat.cpp`, `examples/passkey/passkey.cpp`, `examples/save-load-state/save-load-state.cpp`, `examples/simple-chat/simple-chat.cpp`, `examples/simple/simple.cpp`, `include/llama.h`, `src/llama-context.cpp`, `src/llama.cpp`, `tests/export-graph-ops.cpp`, `tests/test-autorelease.cpp`, `tests/test-backend-sampler.cpp`, `tests/test-grammar-llguidance.cpp`, `tests/test-llama-archs.cpp`, `tests/test-quantize-stats.cpp`, `tests/test-thread-safety.cpp`, `tests/test-tokenizer-0.cpp`, `tests/test-tokenizer-1-bpe.cpp`, `tests/test-tokenizer-1-spm.cpp`, `tools/batched-bench/batched-bench.cpp`, `tools/llama-bench/llama-bench.cpp`
- Noise files (24): `common/common.cpp`, `common/speculative.cpp`, `examples/batched/batched.cpp`, `examples/diffusion/diffusion-cli.cpp`, `examples/idle/idle.cpp`, `examples/llama.android/lib/src/main/cpp/ai_chat.cpp`, `examples/passkey/passkey.cpp`, `examples/save-load-state/save-load-state.cpp`, `examples/simple-chat/simple-chat.cpp`, `examples/simple/simple.cpp`, `include/llama.h`, `src/llama.cpp`, `tests/export-graph-ops.cpp`, `tests/test-autorelease.cpp`, `tests/test-backend-sampler.cpp`, `tests/test-grammar-llguidance.cpp`, `tests/test-llama-archs.cpp`, `tests/test-quantize-stats.cpp`, `tests/test-thread-safety.cpp`, `tests/test-tokenizer-0.cpp`, `tests/test-tokenizer-1-bpe.cpp`, `tests/test-tokenizer-1-spm.cpp`, `tools/batched-bench/batched-bench.cpp`, `tools/llama-bench/llama-bench.cpp`
- **噪声占比: 96%**
- ⚠️ **检索到但未引用**: `src/llama-context.cpp`

### posthoc_audit_045
- Gold files (2): `common/arg.cpp`, `common/common.cpp`
- Retrieved files (2): `common/arg.h`, `common/common.cpp`
- Noise files (1): `common/arg.h`
- **噪声占比: 50%**
- ❌ **未检索到**: `common/arg.cpp`
- ⚠️ **检索到但未引用**: `common/common.cpp`

### posthoc_audit_046
- Gold files (1): `common/common.cpp`
- Retrieved files (3): `common/common.cpp`, `common/peg-parser.h`, `ggml/src/ggml-sycl/ggml-sycl.cpp`
- Noise files (2): `common/peg-parser.h`, `ggml/src/ggml-sycl/ggml-sycl.cpp`
- **噪声占比: 67%**
- ⚠️ **检索到但未引用**: `common/common.cpp`

### posthoc_audit_047
- Gold files (2): `common/arg.cpp`, `common/common.cpp`
- Retrieved files (2): `common/arg.cpp`, `common/common.cpp`
- Noise files (0): ``
- **噪声占比: 0%**
- ⚠️ **检索到但未引用**: `common/arg.cpp`, `common/common.cpp`

### posthoc_audit_048
- Gold files (3): `common/arg.cpp`, `common/common.cpp`, `common/hf-cache.cpp`
- Retrieved files (4): `common/arg.cpp`, `common/common.cpp`, `common/hf-cache.cpp`, `common/peg-parser.h`
- Noise files (1): `common/peg-parser.h`
- **噪声占比: 25%**
- ⚠️ **检索到但未引用**: `common/arg.cpp`, `common/common.cpp`, `common/hf-cache.cpp`

### posthoc_audit_050
- Gold files (1): `src/llama-sampler.cpp`
- Retrieved files (7): `common/peg-parser.h`, `common/reasoning-budget.cpp`, `common/sampling.h`, `ggml/src/ggml.c`, `src/llama-grammar.cpp`, `src/llama-sampler.cpp`, `tools/mtmd/mtmd.h`
- Noise files (6): `common/peg-parser.h`, `common/reasoning-budget.cpp`, `common/sampling.h`, `ggml/src/ggml.c`, `src/llama-grammar.cpp`, `tools/mtmd/mtmd.h`
- **噪声占比: 86%**
- ⚠️ **检索到但未引用**: `src/llama-sampler.cpp`

## 噪声与引用缺失的相关性分析

- 有'搜到未引'的题目: **43 题**，平均噪声占比: **48.7%**
- 无引用问题的题目: **7 题**，平均噪声占比: **48.8%**

> **关键发现**: 有引用缺失的题目与无引用问题的题目，平均噪声占比几乎相同（48.7% vs 48.8%）。
> 这说明 **'搜到未引'并非由噪声引起**，而是 LLM 在答案生成阶段主动选择不引用这些文件。
> 即使噪声为 0% 的题目（如 posthoc_audit_006），也存在'搜到未引'的情况。

## 高噪声题目 (≥80%)

共 **7 题**：

- **posthoc_audit_011**: 82% 噪声, 检索 17 个文件
  - 噪声文件: `common/common.h`, `common/log.cpp`, `examples/llama.android/lib/src/main/cpp/ai_chat.cpp`, `examples/lookahead/lookahead.cpp`, `examples/lookup/lookup.cpp`, `examples/parallel/parallel.cpp`, `examples/speculative-simple/speculative-simple.cpp`, `examples/speculative/speculative.cpp`, `ggml/src/ggml.c`, `src/llama-sampler.cpp`, `tests/test-thread-safety.cpp`, `tools/mtmd/mtmd-cli.cpp`, `tools/server/server-context.cpp`, `tools/tts/tts.cpp`
  - 搜到未引: `common/sampling.cpp`, `common/speculative.cpp`
- **posthoc_audit_012**: 80% 噪声, 检索 15 个文件
  - 噪声文件: `common/log.cpp`, `common/peg-parser.cpp`, `examples/embedding/embedding.cpp`, `examples/simple-chat/simple-chat.cpp`, `ggml/src/ggml.c`, `src/llama-arch.h`, `src/llama-grammar.h`, `src/llama-vocab.cpp`, `src/llama-vocab.h`, `tests/test-chat.cpp`, `tools/mtmd/mtmd-cli.cpp`, `tools/server/server-common.cpp`
  - 搜到未引: `common/common.cpp`
- **posthoc_audit_019**: 82% 噪声, 检索 22 个文件
  - 噪声文件: `examples/diffusion/diffusion-cli.cpp`, `examples/llama.android/lib/src/main/cpp/ai_chat.cpp`, `examples/simple-chat/simple-chat.cpp`, `examples/simple/simple.cpp`, `ggml/src/ggml-backend.cpp`, `ggml/src/ggml.c`, `src/llama-impl.cpp`, `tests/export-graph-ops.cpp`, `tests/test-autorelease.cpp`, `tests/test-backend-sampler.cpp`, `tests/test-grammar-llguidance.cpp`, `tests/test-llama-archs.cpp`, `tests/test-quantize-stats.cpp`, `tests/test-tokenizer-0.cpp`, `tests/test-tokenizer-1-bpe.cpp`, `tests/test-tokenizer-1-spm.cpp`, `tools/llama-bench/llama-bench.cpp`, `tools/mtmd/mtmd.h`
- **posthoc_audit_033**: 80% 噪声, 检索 5 个文件
  - 噪声文件: `ggml/src/ggml-backend.cpp`, `ggml/src/ggml-sycl/ggml-sycl.cpp`, `ggml/src/ggml.c`, `src/llama-impl.cpp`
  - 搜到未引: `src/llama-sampler.cpp`
- **posthoc_audit_038**: 83% 噪声, 检索 6 个文件
  - 噪声文件: `common/download.cpp`, `tests/test-quant-type-selection.cpp`, `tests/test-thread-safety.cpp`, `tools/llama-bench/llama-bench.cpp`, `tools/server/server-context.cpp`
  - 搜到未引: `common/arg.cpp`
- **posthoc_audit_044**: 96% 噪声, 检索 25 个文件
  - 噪声文件: `common/common.cpp`, `common/speculative.cpp`, `examples/batched/batched.cpp`, `examples/diffusion/diffusion-cli.cpp`, `examples/idle/idle.cpp`, `examples/llama.android/lib/src/main/cpp/ai_chat.cpp`, `examples/passkey/passkey.cpp`, `examples/save-load-state/save-load-state.cpp`, `examples/simple-chat/simple-chat.cpp`, `examples/simple/simple.cpp`, `include/llama.h`, `src/llama.cpp`, `tests/export-graph-ops.cpp`, `tests/test-autorelease.cpp`, `tests/test-backend-sampler.cpp`, `tests/test-grammar-llguidance.cpp`, `tests/test-llama-archs.cpp`, `tests/test-quantize-stats.cpp`, `tests/test-thread-safety.cpp`, `tests/test-tokenizer-0.cpp`, `tests/test-tokenizer-1-bpe.cpp`, `tests/test-tokenizer-1-spm.cpp`, `tools/batched-bench/batched-bench.cpp`, `tools/llama-bench/llama-bench.cpp`
  - 搜到未引: `src/llama-context.cpp`
- **posthoc_audit_050**: 86% 噪声, 检索 7 个文件
  - 噪声文件: `common/peg-parser.h`, `common/reasoning-budget.cpp`, `common/sampling.h`, `ggml/src/ggml.c`, `src/llama-grammar.cpp`, `tools/mtmd/mtmd.h`
  - 搜到未引: `src/llama-sampler.cpp`

## 零噪声但引用不全的题目

共 **7 题**（噪声为 0% 但仍未引用所有 gold 文件）：

- **posthoc_audit_006**: Gold=2, 检索=2
  - 搜到未引: `common/chat-auto-parser-helpers.cpp`
- **posthoc_audit_016**: Gold=3, 检索=3
  - 搜到未引: `common/arg.cpp`, `common/download.cpp`, `common/preset.cpp`
- **posthoc_audit_022**: Gold=2, 检索=2
  - 搜到未引: `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`
- **posthoc_audit_023**: Gold=2, 检索=2
  - 搜到未引: `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`
- **posthoc_audit_026**: Gold=2, 检索=2
  - 搜到未引: `common/chat-auto-parser-helpers.cpp`, `common/chat-diff-analyzer.cpp`
- **posthoc_audit_029**: Gold=2, 检索=2
  - 搜到未引: `common/arg.cpp`, `common/preset.cpp`
- **posthoc_audit_047**: Gold=2, 检索=2
  - 搜到未引: `common/arg.cpp`, `common/common.cpp`

## 结论

1. **整体噪声很高（63.6%）**：检索系统每找到 5.9 个文件，平均只有 2.1 个是 gold，3.8 个是噪声。
2. **噪声不是引用缺失的根因**：有/无引用缺失的题目平均噪声占比几乎相同（~49%），说明 LLM 不引用 gold 文件不是因为被噪声干扰。
3. **零噪声也引用不全**：8 题完全无噪声，其中 5 题仍存在'搜到未引'。
4. **高噪声题的噪声来源**：主要是 tests/、examples/、tools/ 目录下的文件被过度召回，以及不相关模块的 embedding 结果混入。
5. **优化方向**：应优先改善答案生成 prompt（强制引用所有检索到的证据），而非单纯降低检索噪声。