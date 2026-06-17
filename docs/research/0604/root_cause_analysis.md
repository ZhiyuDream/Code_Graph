# 调用方遗漏与配对遗漏的根因分析

**日期**: 2026-06-04

---

## 问题现象

1. **配对遗漏**: `chat-auto-parser-helpers.cpp` ↔ `chat-diff-analyzer.cpp` 经常只命中其一
2. **调用方遗漏**: `common/common.cpp` 被遗漏 5 次

---

## 根因一：C++ 类成员函数的调用关系解析缺陷（主要）

### 现象

`trim_whitespace` 在 `chat-diff-analyzer.cpp` 中有 **9 处调用**：

```
common/chat-diff-analyzer.cpp:217   — analyze_reasoning::compare_thinking_enabled
common/chat-diff-analyzer.cpp:339   — analyze_content::analyze_content
common/chat-diff-analyzer.cpp:340   — analyze_content::analyze_content
common/chat-diff-analyzer.cpp:379   — analyze_content::analyze_content
common/chat-diff-analyzer.cpp:510   — analyze_content::is_always_wrapped
common/chat-diff-analyzer.cpp:671   — analyze_tools::check_per_call_markers
common/chat-diff-analyzer.cpp:672   — analyze_tools::check_per_call_markers
common/chat-diff-analyzer.cpp:782   — analyze_tools::extract_function_markers
common/chat-diff-analyzer.cpp:1057  — analyze_tools::analyze_arguments
```

但 Neo4j 中 `trim_whitespace` 的调用者只有 **3 个**：
- `prune_whitespace_segments @ common/chat-auto-parser-helpers.h`
- `analyze_content @ common/chat-auto-parser.h`
- `prune_whitespace_segments @ common/chat-auto-parser-helpers.cpp`

**缺失的 6 个调用者全部在 `chat-diff-analyzer.cpp` 中！**

### 验证

查询 `chat-diff-analyzer.cpp` 中类成员函数的出边（调用了谁）：

```
analyze_content::analyze_content       -> ['analyze_base', 'common_log_get_verbosity_thold']
analyze_reasoning::compare_thinking_enabled -> ['common_log_get_verbosity_thold']
analyze_tools::check_per_call_markers  -> ['common_log_get_verbosity_thold']
analyze_tools::extract_function_markers -> ['common_log_get_verbosity_thold']
analyze_tools::analyze_arguments       -> (无)
...
```

**完全没有 `trim_whitespace`！**

但实际代码中这些函数都大量调用了 `trim_whitespace`。

### 结论

AST 解析器（clang-based）**没有正确解析 C++ 类成员函数对普通函数的调用关系**。

具体表现：
- `chat-diff-analyzer.cpp` 中的函数都是类成员函数（`ClassName::methodName` 格式）
- AST 解析器记录了这些函数的定义（在 Neo4j 中函数名为 `analyze_content::analyze_content`）
- 但**没有记录**这些类成员函数对 `trim_whitespace` / `segmentize_markers` / `calculate_diff_split` 的调用边
- 相反，调用关系被错误地关联到了头文件（`.h`）中的声明节点

### 影响

- `expand_callers(trim_whitespace)` 不会返回 `chat-diff-analyzer.cpp` 中的调用者
- 答案只能命中 `chat-auto-parser-helpers.cpp`（实现文件），无法命中 `chat-diff-analyzer.cpp`（调用文件）
- 导致"配对遗漏"——只命中了实现端，没命中调用端

---

## 根因二：调用者过多导致 Expansion 选择器被淹没（次要）

### 现象

`llama_model_chat_template` 在 Neo4j 中有 **73 个调用者**：

```
common_init_from_params @ common/common.cpp:1268
common_init_from_params @ common/common.h:840
common_chat_templates_init @ common/chat.cpp:591
format_prompt_rerank @ tools/server/server-common.cpp:2107
get_tts_version @ tools/tts/tts.cpp:478
main @ examples/embedding/embedding.cpp:97
main @ examples/simple-chat/simple-chat.cpp:15
main @ tools/cli/cli.cpp:346
main @ tests/test-chat-template.cpp:112
... (还有 60+ 个 main 函数)
```

### 问题

Expansion 选择器最多选 **10 个**相关函数。当 `expand_callers` 返回 73 个调用者时：

1. 大量 `main` 函数和 `tools/` / `examples/` / `tests/` 目录的调用占据了选择名额
2. `common_init_from_params`（在 `common/common.cpp` 中）被淹没在噪声中
3. 答案引用了 `common/chat.cpp` 和 `src/llama-model.cpp`，但遗漏了 `common/common.cpp`

### 验证

`common/common.cpp:1321` 确实有 `llama_model_chat_template` 的调用：
```cpp
bool has_rerank_prompt = llama_model_chat_template(model, "rerank") != NULL;
```

Neo4j 也记录了 `common_init_from_params @ common/common.cpp:1268` 这个调用者。

但答案没引用到，说明是 **Expansion 选择器的选择策略问题**，不是调用图缺失。

---

## 根因对比

| 问题 | 根因 | 说明 |
|---|---|---|
| `chat-diff-analyzer.cpp` 被遗漏 | **AST 解析缺陷** | 类成员函数的调用关系根本没进 Neo4j |
| `common/common.cpp` 被遗漏 | **选择器被淹没** | 调用者在 Neo4j 中存在，但 73 个调用者中选不到它 |

---

## 量化分布

22 处真实缺失证据的根因分类：

| 根因 | 数量 | 占比 | 典型目标函数 |
|---|---|---|---|
| **AST 解析缺陷** | 12 | **54.5%** | `segmentize_markers`(4), `calculate_diff_split`(3), `trim_whitespace`(2), `prune_whitespace_segments`(1), `common_get_model_endpoint`(1), `common_sampler_init`(1) |
| **选择器被淹没** | 10 | **45.5%** | `llama_model_chat_template`(5), `common_get_model_endpoint`(2), `ggml_sycl_set_device`(2), `common_sampler_init`(1) |

### 按目标函数的纯根因分布

| 目标函数 | 根因 | 说明 |
|---|---|---|
| `llama_model_chat_template` | **纯选择器问题** | 73 个调用者，Expansion 选不到 `common/common.cpp` 和 `common/chat.cpp` |
| `segmentize_markers` | **纯 AST 缺陷** | `chat-diff-analyzer.cpp` 中类成员函数的调用边全部丢失 |
| `calculate_diff_split` | **纯 AST 缺陷** | 同上 |
| `trim_whitespace` | **纯 AST 缺陷** | 同上 |
| `prune_whitespace_segments` | **纯 AST 缺陷** | 同上 |
| `ggml_sycl_set_device` | **纯选择器问题** | `ggml-sycl` 目录下调用者众多，选不到 `common.cpp` 和 `cpy.cpp` |
| `common_get_model_endpoint` | **混合** | AST 1 + 选择器 2 |
| `common_sampler_init` | **混合** | AST 1 + 选择器 1 |

---

## 修复方向

### 高优先级：修复 AST 解析器对类成员函数调用的解析

**方案 A**: 检查 clang AST 解析代码，确认是否遗漏了 `CXXMemberCallExpr` 或 `CallExpr` 中对自由函数的调用。

**方案 B**: 用 grep 作为 fallback，对 AST 解析器遗漏的调用关系进行补充。

### 中优先级：优化 Expansion 选择器

**方案**: 对调用者进行去噪和排序：
1. 过滤掉 `tests/` 目录的 `main` 函数（测试代码通常不是业务调用方）
2. 优先选择 `common/` / `src/` 目录的调用者
3. 对同名调用者去重（`main` 函数有几十个，但都是不同文件中的入口函数）
