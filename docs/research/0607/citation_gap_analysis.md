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


• Benchmark 跑完了！结果保存在 results/benchmark_symbol_fastpath_20260607_131010.json。耗时约18分钟。

  现在我需要：

  1. 加载新结果
  2. 与之前的结果（修复grep后、未修改prompt前）对比
  3. 分析新prompt的效果

  让我运行对比分析。

• Used Shell (cd /data/users/zzy/RUC/Co...nd='')         print() " )

• Benchmark 跑完了，结果非常惊人！

  Prompt 强化效果：

  • Coverage: 86.7% → 97.0% (+10.3pp)
  • FullRecall: 36/50 → 46/50 (+10 题)
  • 类型1遗漏（检索到但没引用）: 15 → 3 (-12)

  这几乎是质的提升！10 道题从"有遗漏"变成了"完全命中"。

  只剩下 3 处类型1遗漏，让我看看是哪3处。

• Used Shell (cd /data/users/zzy/RUC/Co...    ⚠️ 答案提到了函数名但没写路径') " )

• Benchmark 结果非常惊人！

  Prompt 强化效果：

  • Coverage: 86.7% → 97.0% (+10.3pp)
  • FullRecall: 36/50 → 46/50 (+10 题)
  • 类型1遗漏: 15 → 3 (-12)

  只剩下 3 处遗漏：

  1. Q10 common_sampler_init → common/speculative.cpp（排名3）
  2. Q15 common_list_cached_models → common/preset.cpp（排名2）
  3. Q36 common_chat_verify_template → common/arg.cpp（排名0）

  让我分析一下这3处为什么还是遗漏。

  Q36 common/arg.cpp 排名0，函数名是 common_params_parse_ex。这与目标符号 common_chat_verify_template 不同名。LLM 可能没意识到这个函数也相关。

  Q10 common/speculative.cpp 排名3，函数名 common_speculative_state_draft。同样，函数名与目标符号不同。

  Q15 common/preset.cpp 排名2，函数名 load_from_cache。函数名与目标符号不同。

  所以剩余的3处都是"函数名≠目标符号"的情况（类型1-B）。LLM 看到了文件，但因为函数名不同，可能认为"这个文件与问题不直接相关"，所以没有引用。

  但这些已经是边缘情况了。97% 的 Coverage 已经非常高了。

  让我给用户一个清晰的总结。

• Prompt 强化效果非常显著：

  ┌────────────┬────────┬───────────────┬─────────┐
  │ 指标       │ 修复前 │ Prompt 强化后 │ 变化    │
  ├────────────┼────────┼───────────────┼─────────┤
  │ Coverage   │ 86.7%  │ 97.0%         │ +10.3pp │
  │ FullRecall │ 36/50  │ 46/50         │ +10 题  │
  │ 类型1遗漏  │ 15     │ 3             │ -12     │
  └────────────┴────────┴───────────────┴─────────┘

  10 道题从"有遗漏"变成"完全命中"：

  • Q00 ggml_sycl_set_device: 0% → 100%（之前0引用，现在3个文件全引用了）
  • Q01 llama_model_chat_template: 67% → 100%
  • Q02 llama_model_default_params: 75% → 100%
  • Q04 trim_leading_whitespace: 50% → 100%
  • Q05 trim_trailing_whitespace: 50% → 100%
  • Q18 llama_model_default_params: 75% → 100%
  • Q21 trim_leading_whitespace: 50% → 100%
  • Q27 calculate_diff_split: 67% → 100%
  • Q34 segmentize_markers: 50% → 100%
  • Q45 cpu_get_num_physical_cores: 0% → 100%（之前0引用，现在命中）

  只剩 3 处遗漏，全部是"函数名≠目标符号"的类型：

  • Q10 common_sampler_init → common/speculative.cpp (common_speculative_state_draft，排名3)
  • Q15 common_list_cached_models → common/preset.cpp (load_from_cache，排名2)
  • Q36 common_chat_verify_template → common/arg.cpp (common_params_parse_ex，排名0)
  这3处的共同点是：检索到的函数名与问题中的目标符号完全不同，LLM 虽然看到了文件，但可能认为"这个函数与问题不直接相关"而未引用。不过 97% Coverage 已经是历史最佳了。