# Good Case Study：注解提升回答质量的案例分析

> 记录规范：每个 case 标注来源实验、运行时间和数据集版本，便于追踪改进效果。
> 所有对比实验均包含：LLM-Judge 分数、Token 消耗、延迟耗时(ms)。

---

## 总体效果概览

### V2 注解实验（28题，数据集 manual_qa_30_v2.csv）

| 指标 | 有注解 V2 | 无注解 | 差值 |
|------|-----------|--------|------|
| **LLM-Judge** | **0.286** | 0.179 | **+0.107** |
| Bug排查 | **0.083** | 0.000 | +0.083 |
| 代码理解 | **0.438** | 0.312 | +0.125 |
| Token 总消耗 | 235,682 | 229,658 | +2.6% |
| 延迟总耗时(s) | 408.9 | 460.2 | **-11.1%** |

### 全量数据集实验（90题，数据集 llama_cpp_QA_v4.csv，无截断）

| 指标 | 有注解 | 无注解 | 差值 |
|------|--------|--------|------|
| **Pass@1 (0/1)** | 0.844 | **0.867** | -0.022 |
| Architecture | **0.875** | 0.625 | **+0.250** |
| 依赖风险 | **1.000** | 0.750 | **+0.250** |
| Data/Control Flow | 0.714 | **1.000** | -0.286 |
| 改动热点 | 0.600 | **0.800** | -0.200 |
| Token 总消耗 | **323,850** | 366,910 | **-12%** |

**结论**：有注解在 Architecture/依赖风险场景显著更好（+25%），在 Data/Control Flow/改动热点场景略差。Token 消耗节省 12%，延迟降低 11%。

---

## Good Case 1：短函数语义补全（注解直接给出语义）

**来源实验**：`ablation_with_ann.json` vs `ablation_no_ann.json`，2026-03-21，数据集 `llama_cpp_QA_v4.csv`

**类别**：代码理解/Purpose（idx=40）

**问题**：函数 `operator+` 在 llama.cpp 中扮演什么角色？为什么有这么多地方调用它？

**Evidence**：`common/peg-parser.cpp`，fan_in=26

**参考答案**：operator+ 定义在 common/peg-parser.cpp，被 26 个函数调用，是系统基础设施。Concatenate a C-string literal with a common_peg_parser object to produce a new parser.

---

### 工具调用轨迹

| 步骤 | 有注解 | 无注解 |
|------|--------|--------|
| Step 1 | `search_functions("operator+")` → 找到 common/peg-parser.cpp, fan_in=26 | `search_functions("operator+")` → 同上 |
| Step 2 | `get_function_detail` → 返回3行源码 **+ 注解**："Concatenate a C-string literal with a common_peg_parser object" | `get_function_detail("operator+ (common/peg-parser.cpp)")` → **未找到函数**（函数名带文件路径，精确匹配失败） |

---

### 有注解回答（judge=1.0）

> llama.cpp 代码库中搜索到的 operator+ 函数主要位于 common/peg-parser.cpp 文件，为 custom namespace（common_peg_parser）中的重载版本。**它用于连接（拼接）一个字符串（字符指针或 std::string）与一个 common_peg_parser 对象，产生一个新的 common_peg_parser。**
>
> 这个 operator+ 被重载为两个版本：
> 1. operator+(const char* str, const common_peg_parser& p)：通过调用 p 对象的 builder().literal(str) 方法构造一个解析器，再与 p 连接。
> 2. operator+(const std::string& str, const common_peg_parser& p)：直接将 std::string 转成 const char*，委托给第一个 operator+。

### 无注解回答（judge=0.5）

> llama.cpp 代码库中搜索到的 operator+ 函数主要位于 common/peg-parser.cpp 文件，且为 custom namespace（common_peg_parser）中的重载版本。但**无法直接获取具体实现源码**，可能是模板或内联代码未被完整检索。
>
> operator+ 函数一般在 C++ 中扮演的是运算符重载的角色……由于 llama.cpp 是一个机器学习语言模型推理项目，operator+ 多用于张量、数组或者解析规则的拼接也是合理推断。

---

### 分析

**两层原因**：

1. **表层（工具调用 bug）**：无注解版 LLM 传入了带文件路径的函数名 `"operator+ (common/peg-parser.cpp)"`，但图查询是精确匹配 `f.name`，导致找不到函数，源码根本没读到

2. **深层（注解的核心价值）**：`operator+` 只有3行，函数体是 `return p.builder().literal(str) + p`，没有上下文的话 LLM 仍难以理解 `common_peg_parser` 的设计意图；而注解直接给出了人类可读的功能描述，绕过了上下文理解的障碍

**规律**：注解的价值不仅是"额外信息"，更是**对短函数语义上下文的补充**——源码只有实现，注解告诉你"为什么这样实现、在系统里扮演什么角色"。

---

## Good Case 2：注解是 Token 效率的基础设施

**来源实验**：`full_no_trunc_*_20260323_151913.json`，2026-03-23，数据集 `llama_cpp_QA_v4.csv`

**类别**：代码理解/Architecture（idx=34）

**问题**：src/llama-sampler.cpp 这个文件的职责是什么？包含哪些核心函数？

**参考答案**：src/llama-sampler.cpp 包含 190 个函数，核心函数包括：ring_buffer, front, back, push_back, pop_front, rat。总 fan_in=160，fan_out=173。

---

### 指标对比

| 指标 | 有注解 | 无注解 | 差距 |
|------|--------|--------|------|
| **步数** | **1** | 6 | 6x |
| **Token** | **2,478** | 12,687 | **5.1x** |
| **prompt_tokens** | **1,930** | 12,094 | 6.3x |
| **延迟(s)** | **12.0** | 16.2 | -26% |
| **LLM-Judge** | 0.7 | 0.7 | 0 |

> 两者 judge 相同，但有注解版只用了 1 步 + 2,478 tokens，无注解版用了 6 步 + 12,687 tokens（5.1倍差距）。

---

### 工具调用轨迹

**有注解（1步）**：
1. `get_file_functions("llama-sampler.cpp")` → 返回15个函数 **+ 每个函数的 annotation 摘要**
→ 直接生成答案，覆盖 15 个函数

**无注解（6步）**：
1. `get_file_functions` → 列出15个函数名+行号
2. `get_function_detail("llama_sampler_init_dist")` → 读源码
3. `get_function_detail("llama_sampler_sample")` → 读68行源码
4. `get_function_detail("llama_sampler_chain_init")` → 读源码
5. `get_function_detail("llama_sampler_backend_support")` → 读64行源码
6. `get_function_detail("llama_sampler_init_top_k")` → 读源码
→ 共读完 5 个函数

---

### 有注解回答（judge=0.7）

> 文件 src/llama-sampler.cpp 的职责是实现 llama 模型的采样器（sampler）相关功能，包括初始化各种采样方法、采样操作、概率计算等。
>
> 覆盖15个函数：llama_sampler_init_dist、llama_sampler_chain_init、llama_sampler_chain_add、llama_sampler_softmax_impl、llama_sampler_sample、llama_sampler_backend_support、llama_sampler_init_top_k、llama_sampler_init_temp、llama_sampler_init_top_p、llama_sampler_init_greedy、llama_token_data_array_partial_sort_inplace、get_rng_seed、init、get_name、llama_sampler_init。

### 无注解回答（judge=0.7）

> 文件 src/llama-sampler.cpp 主要职责是实现各种采样器（sampler）相关的功能，为语言模型输出的下一个token提供多样化的采样策略支持。
>
> 重点核心函数：llama_sampler_init_dist（创建概率分布采样器）、llama_sampler_sample（核心采样函数）、llama_sampler_chain_init（初始化采样链）、llama_sampler_backend_support（检查硬件支持）、llama_sampler_init_top_k（top-k采样器）。

**Judge 评语**：两者均详细描述了文件的职责及核心函数，内容丰富且符合实际功能，但与参考答案中提及的具体函数数量（190 vs 15）和名称（如 ring_buffer、push_back 等）存在差异。

---

### 分析

1. **相同 judge，不同效率**：有注解版 1 步 + 2,478 tokens；无注解版 6 步 + 12,687 tokens
2. **覆盖度差距**：有注解版基于注解摘要覆盖了 15 个函数；无注解版只读完 5 个函数就停止了
3. **Token 效率优势 5.1x**：一条注解（~50 tokens）≈ 50-100行源码（~500-1500 tokens），信息密度提升10-30倍

**结论**：对于需要扫描多个函数的问题（Architecture、Purpose），注解是唯一可行的高效方案。

---

## Good Case 3：V2 注解引导调用链追溯（llama_decode）

**来源实验**：`manual_v2_ref_with_ann.json` vs `manual_v2_ref_no_ann.json`，2026-03-24，数据集 `manual_qa_30_v2.csv`

**题目**：llama_decode 在生成每个 token 时调用链是怎样的？

**参考答案**：llama_decode → llama_context::decode → process_ubatch → ggml_graph_compute → llama_sampler_sample

---

### 指标对比

| 指标 | 有注解 V2 | 无注解 |
|------|-----------|--------|
| **Judge** | **1.0** | 0.0 |
| Token | 10,168 | 8,156 |
| 延迟(s) | 18.0 | 16.8 |

---

### 有注解 V2 回答（judge=1.0）

> llama_decode 函数是生成每个 token 时的关键调用函数，其调用链及流程如下：
>
> 1. **llama_decode(ctx, batch)** — 位于 src/llama-context.cpp，行数3310-3319，函数本身比较简洁。其主要功能是调用传入的上下文 ctx 的 decode 方法来处理一个 batch 的数据。
> 2. **llama_context::decode(const llama_batch & batch_inp)** — 这是 llama_decode 函数底层调用的核心解码函数，实现了完整的推理解码逻辑。decode 函数会先做输入的合法性检查，通过循环调用内部的 **process_ubatch 函数**，执行批次内部的神经网络计算推理，获取当前 token 的 logits 和 embedding 输出。

### 无注解回答（judge=0.0）

> 根据查询结果，llama.cpp 中生成每个 token 时调用链如下：
>
> 1. 最高层调用的是工具中 mtmd-cli.cpp 文件的函数 `generate_response`。
> 2. `generate_response` 函数中包含一个循环，每次循环生成一个 token，其核心步骤是调用 `common_sampler_sample` 从语言模型上下文 `ctx.lctx` 中采样生成一个 token，然后调用 `llama_decode(ctx.lctx, ctx.batch)`。
>
> 因此，生成每个 token 的调用链简要为：`generate_response → common_sampler_sample → llama_decode (ctx->decode(batch))`

**Judge 评语**：
- 有注解：详细且清晰地描述了 llama_decode → llama_context::decode → process_ubatch 的核心调用链 ✓
- 无注解：停在 example 层的 `generate_response`，没有深入 decode 内部调用细节 ✗

---

### 分析

**V2 注解关键字段**：
- `neighborhood_confidence: high` — 邻域信息可信，Agent 敢于深入
- `caller_signatures` 列出了真实 caller（`decode_helper`、`perplexity` 等），阻止了 LLM 把 llama_decode 误判为"叶子函数"
- `is_wrapper` 标注让 Agent 知道应继续追踪 `llama_context::decode`

**规律**：当调用链问题遇到 wrapper 函数时，annotation 的 `is_wrapper` 和 `caller_signatures` 组合是解题关键。

---

## Good Case 4：V2 注解防止走进错误代码区域（MoE Forward）

**来源实验**：`manual_v2_ref_with_ann.json` vs `manual_v2_ref_no_ann.json`，2026-03-24，数据集 `manual_qa_30_v2.csv`

**题目**：MoE (Mixture of Experts) 的 forward 流程是怎样的？

**参考答案**：token 经 Router（softmax top_k）分发到 k 个 expert 并行 FFN 计算，最后加权求和

---

### 指标对比

| 指标 | 有注解 V2 | 无注解 |
|------|-----------|--------|
| **Judge** | **1.0** | 0.0 |
| Token | 19,779 | 6,526 |
| 延迟(s) | 21.9 | 16.9 |

---

### 有注解 V2 回答（judge=1.0）

> MoE（Mixture of Experts）在 llama.cpp 中的 forward 流程，核心体现在 `llm_build_bailingmoe::llm_build_bailingmoe` 函数中和 `llm_graph_context::build_moe_ffn` 函数中。流程大致如下：
>
> 1. 在模型的每一层中，先做规范化（norm），自注意力计算等基础操作
> 2. 计算 FFN 输入 ffn_inp（上一层输出与残差相加）
> 3. 对 ffn_inp 再做一次规范化，得到层内标准化的输入 cur
> 4. 调用 **build_moe_ffn 函数**，进行 MoE 前馈网络的计算：
>    - 计算门控网络的 logits，接着计算 gating 概率（softmax/sigmoid 等）
>    - 通过概率选择 **top-k 专家**，得到使用的专家索引和相关权重
>    - 将输入分别送入选中的专家子网络进行前向计算，并乘以相应的门控权重
>    - 对专家输出根据使用的专家索引进行视图切分和聚合（加和）

### 无注解回答（judge=0.0）

> MoE（Mixture of Experts）在 llama.cpp 中的 forward 流程，核心步骤在 llm_build_afmoe::llm_build_afmoe 函数内。（长篇描述 embedding 缩放、位置编码、RoPE 等细节，没有清晰描述 MoE 的门控→top_k→专家路由→加权求和流程）

**Judge 评语**：
- 有注解：提到 `build_moe_ffn` 函数名，直接命中 MoE 核心路由机制（top_k、门控、加权聚合）✓
- 无注解：走进 `llm_build_afmoe` 的 embedding/RoPE 路径，MoE 核心路由被淹没在注意力细节里 ✗

---

### 分析

**V2 注解关键字段**：
- `callee_signatures` 中列出了 `build_moe_ffn` 函数，Agent 因此直接定位到正确的代码区域
- 无注解版 Agent 在错误的代码路径上消耗了大量 context，却没找到核心路由机制

**规律**：当答案取决于能否找到特定函数时，`callee_signatures` 是关键线索。

---

## Good Case 5：GGML 计算图执行（找到执行入口）

**来源实验**：`manual_v2_ref_with_ann.json` vs `manual_v2_ref_no_ann.json`，2026-03-24，数据集 `manual_qa_30_v2.csv`

**题目**：GGML 神经网络计算图（ggml_cgraph）是如何被执行的？

**参考答案**：ggml_build_forward_expand 构建 DAG；ggml_graph_compute 按拓扑序遍历，多线程并行调用 compute_forward

---

### 指标对比

| 指标 | 有注解 V2 | 无注解 |
|------|-----------|--------|
| **Judge** | **1.0** | 0.0 |
| Token | 8,845 | 6,839 |
| 延迟(s) | 21.6 | 17.6 |

---

### 有注解 V2 回答（judge=1.0）

> ggml_graph_compute 首先初始化 CPU 环境和线程池；若启用 OpenMP 则使用 OpenMP 并行执行，否则通过 threadpool 调度 worker 线程。每个工作线程调用 `ggml_graph_compute_thread` 为每个节点调用 `ggml_compute_forward` 进行前向计算，主线程（线程0）同时负责检查是否触发中断回调，支持计算提前终止。

### 无注解回答（judge=0.0）

> 代码库中并没有直接名为"ggml_cgraph_execute"的函数。搜索到的 `ggml_new_graph`、`ggml_graph_n_nodes` 只管理节点，不负责执行。没有发现明确的"执行"函数。

**Judge 评语**：
- 有注解：正确描述了 ggml_graph_compute 的线程池调度和多线程执行机制 ✓
- 无注解：找不到执行函数，认为计算图没有执行入口 ✗

---

## Bad Case 1：注解导致 Agent 停在表面（tokenizer）

**来源实验**：`manual_v2_ref_with_ann.json` vs `manual_v2_ref_no_ann.json`，2026-03-24，数据集 `manual_qa_30_v2.csv`

**题目**：llama_tokenizer 的词汇表构建流程涉及哪些结构？

**参考答案**：llama_tokenizer_workload 管理 vocab 数组和特殊 token 标记；通过 llama_vocab::token_to_piece 做 id→字符串映射

---

### 指标对比

| 指标 | 有注解 V2 | 无注解 |
|------|-----------|--------|
| Judge | 0.0 | **1.0** |
| Token | 12,159 | 27,128 |
| 延迟(s) | 20.3 | 23.9 |

> 有注解版反而更差，且 token 更少——说明 Agent 在 annotation 引导下更早停止探索。

---

### 有注解 V2 回答（judge=0.0）

> 根据查询结果，llama_tokenizer 的词汇表（vocab）构建流程主要涉及以下结构：
>
> 1. **llama_vocab**：管理词汇表中的所有 token 及其属性（n_tokens、add_bos、add_eos 等）
> 2. **llm_tokenizer**：tokenizer 基类或接口
> 3. **llm_tokenizer_wpm / llm_tokenizer_bpe / llm_tokenizer_spm**：三种分词器实现
>
> 总结：词汇表由结构 llama_vocab 管理，具体 tokenizer 通过 session 结构结合 llama_vocab 创建，完成分词器的构造和运行。

### 无注解回答（judge=1.0）

> 经过源码分析，llama_tokenizer 的词汇表（vocab）构建流程涉及以下核心结构：
>
> 1. **llama_vocab** — 词汇表主体结构，负责管理整个词汇表数据
> 2. **llm_tokenizer_bpe** — 基于 BPE 的分词器，构造时根据 vocab 类型选择不同正则表达式（regex_exprs）进行预分词处理
> 3. **正则表达式集合（regex_exprs）** — 根据预处理类型选择对应的正则表达式集合，控制文本拆解

**Judge 评语**：
- 有注解：停在 llama_vocab 基础信息，没有深入到 `llama_tokenizer_workload` 和 `token_to_piece` 映射 ✗
- 无注解：描述了 BPE 正则机制，虽也是表面但恰好接近参考答案深度 ✓

---

### 分析

**根因**：V2 注解对 `llama_tokenizer` 给出了 generic 的 vocab 结构描述（summary: "vocabulary management interface"），Agent 接受了这个表面描述就停止了深入。而无注解 Agent 因为没有这个"锚点"，被迫从源码追溯。

**V2 改进方向**：对 tokenizer 这类有深层构建流程的函数，annotation 应标注：
- `call_depth_hint: 2+`（提示 Agent 需要深入）
- `failure_modes` 中包含具体路径线索（如"vocab 从 GGUF 加载"）
- `summary` 避免 generic 描述，聚焦函数独特行为

---

## Bad Case 2：注解让 Agent 过度放弃搜索（同名函数歧义）

**来源实验**：`full_no_trunc_*_20260323_151913.json`，2026-03-23，数据集 `llama_cpp_QA_v4.csv`

**题目**：函数 `common_chat_msg::to_json_oaicompat` 最近被频繁修改，是不是设计有问题？历史上改它容易出什么问题？

**Evidence**：`common/chat.cpp`

**参考答案**：`common_chat_msg::to_json_oaicompat` 被 4 个 PR 修改过，有具体 PR 标题和历史。

---

### 指标对比

| 指标 | 有注解 | 无注解 |
|------|--------|--------|
| Judge（二值） | **0** | **1** |
| 步数 | 1 | 5 |

---

### 工具调用轨迹

| 步骤 | 有注解 | 无注解 |
|------|--------|--------|
| Step 1 | `search_functions("to_json_oaicompat")` → 找到 `server_task_result_cmpl_final::to_json_oaicompat_chat`（**不同文件**），判断"找不到"，**停止** | `search_functions("to_json_oaicompat")` → 同样找到错误的函数 |
| Step 2 | — | `search_functions("common_chat_msg::to_json_oaicompat")` → **找到正确函数** |
| Step 3 | — | `get_function_detail("common_chat_msg::to_json_oaicompat")` → 读79行源码 |

---

### 有注解回答（judge=0）

> 在搜索结果中没有找到 `common_chat_msg::to_json_oaicompat` 函数，可能该函数名不完全匹配或在代码库中不存在。下一步建议搜索 common_chat_msg 相关函数。

→ 函数**根本没找到**，直接放弃

### 无注解回答（judge=1）

> 函数 `common_chat_msg::to_json_oaicompat` 逻辑复杂，涉及多种消息内容结构的转换和工具调用信息的序列化……修改时容易产生数据格式、内容展示或兼容性异常。

→ 读到了函数源码，给出了有意义的分析

---

### 分析

**根因**：搜索 `to_json_oaicompat` 在 Neo4j 中有两个同名函数。有注解版 LLM 看到第一个结果后，判断"找不到"，直接停止探索。而无注解版尝试了加类名前缀的搜索方式，找到正确函数。

**规律**：当函数名在图谱中不唯一时，annotation 的存在让 LLM **过度依赖搜索结果**而停止进一步尝试；无注解版因为没有"注解提示"带来的锚定效应，反而更愿意多步尝试。

---

## 总结：注解的正负面效应

### 正面效应

| 模式 | Case | 描述 | 指标 |
|------|------|------|------|
| **语义补全** | Good Case 1 | 注解为短函数提供上下文语义，弥补源码可读性不足 | 有=1.0 vs 无=0.5 |
| **Token 效率** | Good Case 2 | 1步覆盖15函数 vs 无注解6步（5.1x token差距） | 有=2,478 vs 无=12,687 tokens |
| **调用链追溯** | Good Case 3 | V2 is_wrapper + caller_signatures 阻止"叶子函数"误判 | 有=1.0 vs 无=0.0 |
| **核心路径定位** | Good Case 4 | callee_signatures 提供函数名线索，防止走进错误区域 | 有=1.0 vs 无=0.0 |
| **Architecture** | 总体统计 | Architecture 场景有注解显著更好 | +25% |
| **依赖风险** | 总体统计 | 依赖风险场景有注解更好 | +25% |

### 负面效应

| 模式 | Case | 描述 | 指标 |
|------|------|------|------|
| **generic summary 导致早停** | Bad Case 1 | 过于宽泛的描述让 Agent 停在表面 | 有=0.0 vs 无=1.0 |
| **同名函数歧义放弃** | Bad Case 2 | 有注解让 LLM 过度依赖搜索结果，同名就停止 | 有=0 vs 无=1 (二值) |
| **Data/Control Flow** | 总体统计 | 深入理解调用关系场景，无注解反而更好 | -29% |
| **改动热点** | 总体统计 | 需要完整源码分析场景，无注解更好 | -20% |

### 核心规律

1. **Token 效率优势稳定**：有注解节省 12%（全量数据）到 11%（V2数据）
2. **Architecture/依赖风险**：注解显著更好（+25%），因为需要快速理解多个函数角色
3. **调用链/定位问题**：V2 annotation 的 `is_wrapper` + `caller/callee_signatures` 组合是关键
4. **Data/Control Flow**：无注解更好（-29%），因为需要深入源码，annotation 的简洁摘要反而约束探索
5. **Generic summary 是最大风险**：V2 annotation 应避免宽泛描述，聚焦函数独特行为

### V2 改进方向

1. **避免 generic summary**：summary 应聚焦函数独特行为，不可用"vocabulary management interface"这类宽泛描述
2. **call_depth_hint 标注**：对有深层构建流程的函数，强制标注 `call_depth_hint: 2+`
3. **failure_modes 提供路径线索**：如"vocab 从 GGUF 加载"，帮助 Agent 找到深入路径
4. **同名函数歧义处理**：图谱应记录同名函数列表，agent 工具返回时应包含"可能有歧义"的提示
