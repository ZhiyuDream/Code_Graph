# Bad Case Study：注解导致回答质量下降的案例分析

> 记录规范：每个 case 标注来源实验、运行时间和数据集版本，便于追踪改进效果。

---

## Case 1：注解导致 LLM 过早满足，放弃精确验证（2026-03-21）

**来源实验**：注解消融实验（`ablation_with_ann.json` vs `ablation_no_ann.json`），2026-03-21 运行，数据集 `llama_cpp_QA_v4.csv`

**类别**：工作流/影响分析（idx=77）

**问题**：我想修改函数 `llama_model_get_vocab`，这样做的风险有多大？会影响哪些上游调用者？

**Evidence**：`src/llama-model.cpp`

**参考答案**：`llama_model_get_vocab`（src/llama-model.cpp）被 **24 个**函数调用，修改风险较高。功能：Retrieve the vocabulary associated with a given llama model。建议修改前做充分的回归测试。

---

### 工具调用轨迹对比

| 步骤 | 有注解 | 无注解 |
|------|--------|--------|
| Step 1 | `get_function_detail("llama_model_get_vocab")` → 3行源码 + 注解 | `get_function_detail("llama_model_get_vocab")` → 3行源码（无注解） |
| Step 2 | `get_callers("llama_model_get_vocab", limit=10)` → **返回10个**调用者（受 limit 限制） | `get_callers("llama_model_get_vocab", limit=20)` → **返回20个**调用者 |

### 有注解回答（judge=0.7）

> 从调用情况看，共有 **10个上游调用者**，分别分布在如下文件中：
> - tools/perplexity/perplexity.cpp 中的多个函数（kl_divergence、multiple_choice_score、winogrande_score、hellaswag_score、perplexity、perplexity_v2）
> - tools/mtmd/mtmd.cpp 中的 mtmd_tokenizer
> - tools/mtmd/mtmd-cli.cpp 中的 mtmd_cli_context
> - tools/llama-bench/llama-bench.cpp 中的 test_gen 和 test_prompt
>
> 修改可能连带影响 **10个上游调用者**的逻辑。建议进行回归测试。

### 无注解回答（judge=1.0）

> 上游调用者非常多，共有 **24个调用点**，分布在多个模块：
> - perplexity工具相关的多个函数（kl_divergence等）
> - mtmd模块的tokenizer相关函数
> - llama-bench测试中的test_gen、test_prompt
> - imatrix计算模块
> - cvector-generator及测试模块
> - eval-callback、diffusion-cli、debug工具示例
> - common/speculative模块
>
> 结论：函数很核心且被广泛调用，属于底层接口。建议保持接口不变，只修改内部实现。

### 分析

**关键差异**：有注解版用 `limit=10` 只拿到了10个调用者，无注解版用 `limit=20` 拿到了20个（fan_in 实际是24）。

**根因**：有注解版的 annotation_json 中包含了函数功能摘要，LLM 读到注解后认为"信息已足够"，**用了默认 limit=10**，没有意识到 fan_in=24 意味着远超10个调用者；无注解版的 LLM 只有原始代码，看到 `fan_in=24` 这个数字后意识到调用者很多，**主动将 limit 调高到 20**，获取了更完整的调用者列表。

**模式**：注解提供的"功能摘要"让 LLM 对信息完整性产生了错误的满足感，**放弃了对结构化数字（fan_in）的深入追踪**。无注解版反而因为"没有摘要可以偷懒"，更仔细地利用了工具参数。

---

## Case 2：注解摘要遮蔽了代码中的具体约束条件（2026-03-21）

**来源实验**：注解消融实验（`ablation_with_ann.json` vs `ablation_no_ann.json`），2026-03-21 运行，数据集 `llama_cpp_QA_v4.csv`

**类别**：代码理解/Design Rationale（idx=45）

**问题**：函数 `ggml_acc_impl` 既被 2 个函数调用，又调用了 5 个函数。它在系统中处于什么层级？设计意图是什么？

**Evidence**：`ggml/src/ggml.c`

**参考答案**：`ggml_acc_impl`（ggml/src/ggml.c）是一个中间层函数，fan_in=2，fan_out=5，起到承上启下的作用。Create a new tensor representing the accumulation of two tensors with optional in-place modification and offset parameters.

---

### 工具调用轨迹（两个版本完全相同）

1. `get_callers("ggml_acc_impl", limit=5)` → 2个调用者：`ggml_acc_or_set`, `ggml_compute_backward`
2. `get_callees("ggml_acc_impl", limit=5)` → 5个被调用函数：`ggml_abort`, `ggml_dup_tensor`, `ggml_is_contiguous`, `ggml_nelements`, `ggml_view_tensor`
3. `get_function_detail("ggml_acc_impl")` → 25行源码（被截断）+ 有注解版额外包含注解摘要

### 有注解回答（judge=0.7）

> 设计意图上，ggml_acc_impl 的功能是创建一个新的张量，表示两个张量的累积（accumulation），支持是否就地修改（inplace）及偏移量参数。它的作用是为张量操作设置和初始化相关的参数与操作类型（GGML_OP_ACC），建立一个新的张量视图或复制。总结来说，ggml_acc_impl 是一个**低层的张量操作设置函数**，服务于更高层的累积计算流程。

### 无注解回答（judge=1.0）

> 功能是对两个张量 a 和 b 进行某种累加操作，参数中包含维度信息 nb1、nb2、nb3，偏移 offset，和是否就地修改 inplace。
>
> 内部先做了一些**断言，保证 b 的元素不超过 a，a 张量内存连续且类型为浮点数（F32）**，返回一个新的或就地的结果张量 result，设置操作类型标记（GGML_OP_ACC）。
>
> ggml_acc_impl 处于**中间层**：既服务于更上层的操作封装函数（ggml_acc_or_set 和反向传播 ggml_compute_backward），也封装了对更底层张量基础设施函数的调用。

### 分析

两个版本都读了同样的（截断）代码，但有注解版的 annotation_json 提供了高层摘要："Create a new tensor representing the accumulation"——LLM 读到摘要后**直接接受了这个概括**，没有深入分析源码中具体的约束条件（`assert b <= a`、`a 必须 F32 连续内存`）。

无注解版没有摘要可以依赖，**被迫从截断的源码中逐行推断**，反而提取出了更具体的约束信息（断言条件、数据类型要求），与参考答案对"设计意图"的期待更吻合。

**模式**：注解摘要是高层概括，**遮蔽了代码中的具体实现约束**。当问题考察的正是这些约束细节时，注解反而成了干扰。

---

## Case 3：注解让 LLM 满足于文字描述，不去确认实际调用关系（2026-03-21）

**来源实验**：注解消融实验（`ablation_with_ann.json` vs `ablation_no_ann.json`），2026-03-21 运行，数据集 `llama_cpp_QA_v4.csv`

**类别**：工作流/Code Review（idx=68）

**问题**：我在 review `common/arg.cpp` 的代码，`common_params_parse_ex` 和 `common_params_handle_remote_preset` 之间的调用关系合理吗？这个文件的内聚性如何？

**Evidence**：`common/arg.cpp`

**参考答案**：在 `common/arg.cpp` 中，`common_params_parse_ex`（fan_out=8）调用了 `common_params_handle_remote_preset`（fan_in=1）。功能描述：parse command line arguments / fetches and applies a remote preset configuration。

---

### 工具调用轨迹对比

| 步骤 | 有注解 | 无注解 |
|------|--------|--------|
| Step 1 | `get_function_detail("common_params_parse_ex")` → 208行源码截断 + 注解 | 同左（无注解） |
| Step 2 | `get_function_detail("common_params_handle_remote_preset")` → 41行源码 + 注解 | 同左（无注解） |
| Step 3 | `get_file_functions("common/arg.cpp", limit=15)` → **函数列表+注解摘要** | `get_file_functions("common/arg.cpp")` → **函数列表（无注解，只有 fan 值和行号）** |

### 有注解回答（judge=0.7）

> 调用关系：`common_params_handle_remote_preset` 这个函数的 fan_in 为 1，说明它被 `common_params_parse_ex` 或其他函数调用（**具体调用者未详细检索到**，但一般逻辑上，在解析参数后如遇远程预设需求，会调用该函数进行应用）。两者在解析和应用配置的流程上有逻辑衔接。

### 无注解回答（judge=1.0）

> 从调用关系上看，`common_params_handle_remote_preset` 的 fan_in=1，意味着它**只被一个位置调用，很可能就是在 `common_params_parse_ex` 中调用**（需要确认调用细节，但合理逻辑上远程preset处理是参数解析流程的一部分）。
>
> `common_params_parse_ex` 作为参数解析的主入口函数，调用包括远程preset处理函数是合理的，它负责整体参数解析大流程，远程preset是其中步骤之一，符合职责划分。

### 分析

两个版本都没有调用 `get_callers` 来精确确认调用关系，结论都是"推断"而非"验证"。

关键差异在于 `get_file_functions` 的返回：
- **有注解版**拿到了所有函数的注解摘要，LLM 读到 `common_params_handle_remote_preset` 的注解"Fetches and applies a remote preset configuration"后，**认为功能已经解释清楚，对调用关系的描述变得模糊**（"未详细检索到"）
- **无注解版**只有函数名和 fan 值，LLM 没有注解可以用来"解释"功能，**反而更专注于推理结构关系**（fan_in=1 → 只有一个调用者 → 很可能是 parse_ex 调用的）

**模式**：注解把 LLM 的注意力引向了"这个函数做什么"，而问题真正考察的是"谁调用了谁"。注解的内容方向与问题方向**不对齐**，导致 LLM 在错误维度上花了精力。

---

## 总结：三种注解负面效应模式

| 模式 | Case | 描述 |
|------|------|------|
| **过早满足** | idx=77 | 注解让 LLM 认为信息足够，放弃追踪关键数字（fan_in=24 vs limit=10） |
| **细节遮蔽** | idx=45 | 注解高层摘要遮蔽了代码中的具体约束（断言条件、数据类型要求） |
| **方向偏移** | idx=68 | 注解内容（函数功能）与问题考察点（调用关系）不对齐，分散了 LLM 注意力 |

**共同特征**：这三种模式都不是注解内容"错误"导致的，而是注解改变了 LLM 的信息搜索行为——注解的存在让 LLM 在某个维度"满足"后停止了进一步推理。

**启示**：注解设计应考虑与查询类型的对齐——结构性问题（调用关系、影响范围）不应被功能摘要替代，注解应补充而非替代对结构信息的主动检索。
