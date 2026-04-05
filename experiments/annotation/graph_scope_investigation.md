# 建图范围调查

## 1. Evidence 中的路径（全题去重）
- 去重后路径数：352
- 按后缀：
  - `.cpp`: 236
  - `.h`: 100
  - `.hpp`: 11
  - `.c`: 5
- 按顶层目录：
  - `src/`: 161
  - `ggml/`: 131
  - `common/`: 59
  - `include/`: 1

## 2. compile_commands.json 中的路径
- 去重后路径数：316
- 按后缀：
  - `.cpp`: 305
  - `.c`: 11
- 按顶层目录：
  - `src/`: 139
  - `tools/`: 46
  - `tests/`: 45
  - `common/`: 29
  - `examples/`: 27
  - `ggml/`: 25
  - `build/`: 2
  - `pocs/`: 2
  - `vendor/`: 1

## 3. 图中 Function 的 file_path（Neo4j）
- 去重后路径数：311
- 按后缀：
  - `.cpp`: 301
  - `.c`: 10
- 按顶层目录：
  - `src/`: 138
  - `tools/`: 46
  - `tests/`: 45
  - `common/`: 29
  - `examples/`: 26
  - `ggml/`: 24
  - `pocs/`: 2
  - `vendor/`: 1

### 3.5 Evidence 中未出现在图中的路径（抽样）

- `.h` 共 100 条，抽样：
  - `src/llama-chat.h`
  - `ggml/include/ggml-cpp.h`
  - `src/llama-graph.h`
  - `ggml/src/ggml-metal/ggml-metal-common.h`
  - `common/download.h`
  - `common/chat-auto-parser.h`
  - `common/jinja/string.h`
  - `src/llama-adapter.h`
  - `ggml/src/ggml-cpu/quants.h`
  - `ggml/src/ggml-cann/aclnn_ops.h`
- `.cpp` 共 62 条，抽样：
  - `ggml/src/ggml-metal/ggml-metal-device.cpp`
  - `ggml/src/ggml-metal/ggml-metal.cpp`
  - `ggml/src/ggml-sycl/set_rows.cpp`
  - `ggml/src/ggml-sycl/getrows.cpp`
  - `ggml/src/ggml-sycl/roll.cpp`
  - `ggml/src/ggml-virtgpu/virtgpu-forward-buffer.cpp`
  - `ggml/src/ggml-virtgpu/ggml-backend-reg.cpp`
  - `ggml/src/ggml-virtgpu/virtgpu.cpp`
  - `ggml/src/ggml-sycl/dmmv.cpp`
  - `ggml/src/ggml-sycl/norm.cpp`
- 其他后缀共 11 条，抽样：
  - `ggml/src/ggml-webgpu/ggml-webgpu-shader-lib.hpp`
  - `ggml/src/ggml-sycl/cpy.hpp`
  - `ggml/src/ggml-sycl/convert.hpp`
  - `common/base64.hpp`
  - `ggml/src/ggml-zdnn/utils.hpp`

图中路径抽样（前 20）：
- `common/arg.cpp`
- `common/chat-auto-parser-generator.cpp`
- `common/chat-auto-parser-helpers.cpp`
- `common/chat-diff-analyzer.cpp`
- `common/chat-peg-parser.cpp`
- `common/chat.cpp`
- `common/common.cpp`
- `common/console.cpp`
- `common/debug.cpp`
- `common/download.cpp`
- `common/jinja/caps.cpp`
- `common/jinja/lexer.cpp`
- `common/jinja/parser.cpp`
- `common/jinja/runtime.cpp`
- `common/jinja/string.cpp`
- `common/jinja/value.cpp`
- `common/json-partial.cpp`
- `common/json-schema-to-grammar.cpp`
- `common/llguidance.cpp`
- `common/log.cpp`

## 4. 对比与可能原因

### 4.1 Evidence 中大量 .h 与建图范围不一致

- 建图时**只解析 compile_commands.json 里列出的文件**；而 compile_commands 通常只包含**参与编译的编译单元**（.c / .cpp / .cc / .cxx），**不包含仅被 #include 的 .h 头文件**。
- 解析脚本（ast_parser / clangd_parser）也只处理 SOURCE_EXTENSIONS = {".c", ".cpp", ".cc", ".cxx"}，**不会把 .h 当作独立文件去解析**。
- 若 Evidence 里列了大量 `.h`（本次 Evidence 去重后 `.h` 数量：100，编译单元约 241），则这些 `.h` 路径**不可能**出现在图的 file_path 中（图中仅有编译单元路径或 clang 报告的定义所在文件）。因此 **Evidence 中 .h 占比越高，图对 Evidence 的覆盖率上限就越低**。

### 4.2 图中为何没有 .h 作为 file_path

- **ast_parser**：遍历时每个函数的 `file_path` 被设为**当前解析的编译单元路径**（即 .cpp/.c），即使用户代码定义在 .h 里，ast_parser 当前实现也写的是 TU 路径。
- **clangd_parser**：同样只对 compile_commands 中的**源文件**逐文件请求 documentSymbol，得到的 symbol 的 file_path 传的是**当前打开的文件路径**（即 .cpp），不是声明所在头文件。
- 因此图中 Function 的 file_path **只有 .cpp/.c 等编译单元**，没有单独的 .h。若 Evidence 按「源码文件」列了 .cpp 和 .h，则 .h 在图中必然匹配不到。

### 4.3 compile_commands 范围与解析失败

- compile_commands 中去重后文件数：316；图中 file_path 数：311。
- **图中路径数少于 compile_commands**，说明有 **5** 个文件在解析或导入时未产生节点（解析失败、无符号、或写入 Neo4j 时被过滤）。可逐文件检查解析日志或失败率。

### 4.4 建议

1. **Evidence 与图的定义对齐**：若 Evidence 列出「相关文件」含 .h，可约定覆盖率只按 **.cpp/.c** 计算，或建图时增加「从已解析 TU 中提取声明所在文件」写入节点/边属性，使 .h 也能被统计。
2. **扩大建图范围**：确认 CMake 配置是否包含全仓库（如 ggml、examples、tools 等）；若只 build 了部分目标，compile_commands 会少很多文件，需 Full 或 All 构建后再生成 compile_commands。
3. **解析失败**：若 compile_commands 中文件数远大于图中路径数，需排查解析失败原因（依赖缺失、宏、clang 版本等），或对失败文件做白名单/重试。
