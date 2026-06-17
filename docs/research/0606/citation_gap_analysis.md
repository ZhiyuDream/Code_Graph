# 答案引用完整性分析 — 检索到了但没引用

> 分析时间：2026-06-06  
> 基于 benchmark: `results/benchmark_symbol_fastpath_20260606_211124.json`

---

## 一、概述

在修复类型2（检索层遗漏）后，新系统 Coverage 从 82.0% 提升到 86.7%，但仍存在 **15 处** "文件在检索列表中，但答案没引用" 的遗漏。这些遗漏**不是检索问题**，而是**答案生成层的引用完整性问题**。

---

## 二、分类统计

| 根因模式 | 数量 | 占比 | 典型题目 |
|---|---|---|---|
| 答案中完全没有任何 `file.cpp:line` 引用 | 4 | 27% | Q00, Q05, Q18, Q45 |
| 答案提到了函数名，但没写文件路径 | 9 | 60% | Q01, Q04, Q10, Q21, Q34, Q36 等 |
| 文件被测试函数淹没，排太后未展开 | 1 | 7% | Q02 |
| 其他 | 1 | 7% | Q27 |

---

## 三、逐类根因分析

### 模式 A：答案中完全没有任何 `file.cpp:line` 引用（4 处）

**现象**：LLM 在答案中详细分析了函数的返回值、参数和副作用，提到了函数名（如 `ggml_sycl_cpy`、`trim_trailing_whitespace`），但**整篇答案中没有任何 `file.cpp:line` 格式的代码来源标注**。

**典型案例**：

- **Q00 `ggml_sycl_set_device`**：答案中分析了 `ggml_sycl_set_device` 的返回值和调用方，提到了 `ggml_sycl_cpy` 和 `release_extra_gpu`，但完全没有写 `ggml/src/ggml-sycl/cpy.cpp:xxx` 或 `ggml/src/ggml-sycl/common.cpp:xxx`。
- **Q05 `trim_trailing_whitespace`**：答案完整分析了参数、返回值、副作用，提到 "`compare_reasoning_presence` 中的 `end = trim_trailing_whitespace(...)`"，但没有标注这些调用在哪个文件。

**根因**：LLM **没有养成标注代码来源的习惯**。它在答案中讨论函数时只用函数名，不写 `file.cpp:line`。这不是"漏看文件"，而是**输出格式问题**。

---

### 模式 B：答案提到了函数名，但没写文件路径（9 处）

**现象**：LLM 分析了某个函数或调用了某个函数，但只在答案中写了函数名，没有标注该函数所在的文件路径。或者 LLM 认为"已经引用了足够多的文件"，选择性省略了某些文件的引用。

**典型案例**：

- **Q01 `llama_model_chat_template`**：`src/llama-model.cpp` 的 `llama_model_chat_template` 排在检索列表第0位，答案中也讨论了这个函数（"签名：`const char * llama_model_chat_template(...)`"），但答案只标注了 `common/chat.cpp:xxx`，**没有标注 `src/llama-model.cpp`**。
- **Q18 `llama_model_default_params`**：`src/llama-model.cpp` 的 `llama_model_default_params` 排在检索列表第0位，答案完整分析了函数，但**0 个 file:line 引用**。
- **Q10 `common_sampler_init`**：`common/speculative.cpp` 的 `common_speculative_state_draft` 排在检索列表第1位，答案引用了 3 个文件，唯独没引用 `common/speculative.cpp`。

**根因**：LLM **选择性标注**。它分析时看了这些文件，但认为：
1. "已经引用了足够多的文件"
2. "这个文件的内容已经被其他引用覆盖"
3. 或者单纯是**注意力分散**，忘了标注

---

### 模式 C：文件被测试函数淹没，排太后未展开（1 处）

**典型案例**：

- **Q02 `llama_model_default_params`**：`common/common.cpp` 的 `common_model_params_to_llama` 排在检索列表第17位。前16位全是 `tests/` 和 `examples/` 的 `main` 函数。LLM 的注意力被分散到大量不相关的测试函数上，没看到排第17位的重要文件。

---

## 四、核心结论

当前瓶颈不是"LLM 没看到文件"，而是 **"LLM 看到了但没在答案中标注出来"**。

具体表现为：
1. **输出格式问题**：LLM 经常在答案中分析函数但不标注 `file.cpp:line` 格式
2. **选择性标注**：LLM 认为"够了"，故意不标某些文件
3. **注意力分散**：上下文函数太多，LLM 只引用了一部分

这本质上是 **answer_generation prompt 的指令不够强制**——prompt 可能说"请引用相关代码"，但没有明确说"你必须为每一个你分析到的函数标注 `file.cpp:line` 格式，且列出所有参考过的文件"。

---

## 五、修复方向

### 方向 1：强化 answer_generation prompt（高优先级）

在 answer_generation prompt 中增加以下要求：

1. **强制标注格式**："每当你分析一个函数时，必须用 `file.cpp:start_line-end_line` 的格式标注其代码来源"
2. **完整性检查**："在答案末尾，列出所有你参考过的文件清单"
3. **惩罚不引用**："如果你分析了一个函数但没有标注其来源，你的回答会被认为是不完整的"

### 方向 2：后处理补全（中优先级）

在答案生成后，扫描答案中提到的函数名：
- 如果某个检索到的文件中的函数被提到但未标注路径，自动补全引用
- 例如：答案中提到 `ggml_sycl_cpy`，但检索列表中有 `ggml/src/ggml-sycl/cpy.cpp:xxx`，则自动追加引用

### 方向 3：减少前排噪声（低优先级）

- 过滤 `tests/` 和 `examples/` 的 `main` 函数，避免淹没重要文件
- 或者降低测试文件的排序权重
