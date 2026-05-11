# 检索组件增益原因深度分析

**模型**: gpt-4.1-mini | **基准**: emb+grep+graph

---

## 一、Embedding 增益 (125 道题)

### 1.1 检索特征

- baseline 有 embedding 召回: 125 / 125 题
- no_emb 完全无 embedding 召回: 125 / 125 题
- baseline 平均 embedding 召回: 20.0 个函数
- no_emb 平均 embedding 召回: 0.0 个函数
- no_emb 平均 graph 召回: 5.8 个函数
- no_emb 平均 fallback 召回: 1.5 个函数

### 1.2 核心规律

Embedding 去掉后，这些题目的检索质量严重下降，因为：

1. **语义召回不可替代**: 问题中的概念（如 `llama-quant`、`speculative`）无法通过精确匹配找到
2. **Grep+Graph 替代不足**: no_emb 配置下，这些题平均仅召回 5.8 个函数（graph）+ 1.5 个函数（fallback），远低于 baseline 的 20.0 个 embedding 召回

### 1.3 代表性题目 (前 20 道)

- **[7]** bert 直接依赖和间接依赖的其他模块或命名空间有哪些，它们之间的调用关系和依赖链路是怎样的？
  - baseline: emb=20 graph=10 fallback=0 | no_emb: emb=0 graph=10 fallback=2
- **[10]** 为什么在设计 unary-ops 时选择当前的边界划分和职责分配，这些设计决策如何权衡了系统的复杂度、可复用性和耦合性？
  - baseline: emb=20 graph=10 fallback=0 | no_emb: emb=0 graph=10 fallback=1
- **[13]** 为什么选择以当前方式划分和设计 http，其背后的设计目标和预期带来的系统价值是什么？
  - baseline: emb=20 graph=10 fallback=0 | no_emb: emb=0 graph=10 fallback=1
- **[14]** 为什么在系统设计中选择将特定功能归纳到 llama-quant，这种划分对整体架构的目标和系统演进有哪些关键影响？
  - baseline: emb=20 graph=10 fallback=0 | no_emb: emb=0 graph=10 fallback=1
- **[15]** 为什么在设计 speculative 时选择了当前的架构或实现方式，这对系统的性能有哪些具体影响和优化考虑？
  - baseline: emb=20 graph=10 fallback=0 | no_emb: emb=0 graph=10 fallback=1
- **[20]** 数据或控制流在代码中通过哪些关键位置进入和离开 exaone，这些流向在哪里具体体现？
  - baseline: emb=20 graph=10 fallback=0 | no_emb: emb=0 graph=10 fallback=2
- **[23]** 在哪里可以找到 value 中实现其主要功能的具体代码位置及相关调用入口？
  - baseline: emb=20 graph=10 fallback=0 | no_emb: emb=0 graph=10 fallback=1
- **[27]** 在系统整体架构中，ggml-openvino-extra 是如何实现内部功能协同和对外服务提供的，具体的工作流程和通信机制是什么？
  - baseline: emb=20 graph=10 fallback=0 | no_emb: emb=0 graph=10 fallback=2
- **[29]** 如何设计和实现 ggml-metal-ops 的整体架构，以保证其内部模块的协同高效运行，并支持系统的可扩展性和维护性？
  - baseline: emb=20 graph=10 fallback=0 | no_emb: emb=0 graph=10 fallback=2
- **[31]** 如何在 qwen3 中设计和实现关键算法的具体步骤和流程，以确保其逻辑正确性和执行效率？
  - baseline: emb=20 graph=10 fallback=0 | no_emb: emb=0 graph=10 fallback=1
- **[34]** 如何在 sampling 中利用现有的API或框架支持，实现其核心功能的集成与扩展？
  - baseline: emb=20 graph=10 fallback=0 | no_emb: emb=0 graph=10 fallback=2
- **[36]** ggml-cpu.c 中包含了哪些主要的代码元素及其层次结构，这些元素如何构成整体架构？
  - baseline: emb=20 graph=10 fallback=0 | no_emb: emb=0 graph=10 fallback=2
- **[38]** ggml-alloc.c 中的各个代码元素（如函数、类、变量等）之间存在哪些依赖关系，这些依赖关系如何影响代码的整体结构和行为？
  - baseline: emb=20 graph=10 fallback=0 | no_emb: emb=0 graph=10 fallback=2
- **[43]** 在 ggml-alloc.c 中，某个关键变量或数据的定义、赋值和使用分别位于哪些具体位置，它们之间的数据流向是怎样的？
  - baseline: emb=20 graph=10 fallback=0 | no_emb: emb=0 graph=10 fallback=1
- **[45]** 在 quants.c 中，某个特定功能的具体实现代码位于哪些位置？
  - baseline: emb=20 graph=10 fallback=0 | no_emb: emb=0 graph=10 fallback=1
- **[48]** 如何在 ggml-alloc.c 中设计和实现整体代码的组织结构，以支持模块化、可维护性和扩展性？
  - baseline: emb=20 graph=10 fallback=0 | no_emb: emb=0 graph=10 fallback=1
- **[51]** 在 ggml-alloc.c 中，某个算法是如何具体实现的，其核心步骤和处理流程是怎样设计的？
  - baseline: emb=20 graph=10 fallback=0 | no_emb: emb=0 graph=10 fallback=1
- **[52]** 如何在 ggml-alloc.c 中使用提供的API或框架接口来实现关键功能的调用和数据交互？
  - baseline: emb=20 graph=10 fallback=0 | no_emb: emb=0 graph=10 fallback=2
- **[58]** 什么是MMQ_X_Q4_0_RDNA1，它们的基本定义、主要功能及在代码中的典型应用场景有哪些？
  - baseline: emb=20 graph=0 fallback=1 | no_emb: emb=0 graph=0 fallback=1
- **[60]** 系统中各个GGUF_MAX_STRING_LENGTH之间存在哪些依赖关系，这些依赖是如何被建立、解析和维护的？
  - baseline: emb=20 graph=0 fallback=0 | no_emb: emb=0 graph=0 fallback=1

---

## 二、Grep 增益 (31 道题)

### 2.1 检索特征

- fallback 召回增加: 6 题
- fallback 召回相同: 25 题
- baseline 平均 fallback 召回: 0.3 个函数
- no_grep 平均 fallback 召回: 0.0 个函数

### 2.2 核心规律

Grep 是 Embedding 的"精确补丁"，主要解决：

1. **短符号召回**: 宏名、常量名（如 `GGUF_MAX_STRING_LENGTH`）embedding 难以区分
2. **精确位置定位**: "在哪里定义/引用"需要全文搜索所有出现位置
3. **问题含明确符号名**: 问题直接包含函数/变量名时，Grep 可直接命中

### 2.3 代表性题目

- **[7]** bert 直接依赖和间接依赖的其他模块或命名空间有哪些，它们之间的调用关系和依赖链路是怎样的？
  - baseline: emb=20 graph=10 fallback=0 | no_grep: emb=20 graph=10 fallback=0
- **[21]** 在代码库中，gemma 的核心功能具体实现分布在哪里，这些实现是如何被调用或引用的？
  - baseline: emb=20 graph=10 fallback=0 | no_grep: emb=20 graph=10 fallback=0
- **[43]** 在 ggml-alloc.c 中，某个关键变量或数据的定义、赋值和使用分别位于哪些具体位置，它们之间的数据流向是怎样的？
  - baseline: emb=20 graph=10 fallback=0 | no_grep: emb=20 graph=10 fallback=0
- **[47]** 在 ggml.c 中，某个特定标识符（如变量、函数或类）的定义和所有引用位置分别在哪里？
  - baseline: emb=20 graph=10 fallback=0 | no_grep: emb=20 graph=10 fallback=0
- **[48]** 如何在 ggml-alloc.c 中设计和实现整体代码的组织结构，以支持模块化、可维护性和扩展性？
  - baseline: emb=20 graph=10 fallback=0 | no_grep: emb=20 graph=10 fallback=0
- **[60]** 系统中各个GGUF_MAX_STRING_LENGTH之间存在哪些依赖关系，这些依赖是如何被建立、解析和维护的？
  - baseline: emb=20 graph=0 fallback=0 | no_grep: emb=20 graph=0 fallback=0
- **[62]** 为什么选择使用特定方式定义GGML_GELU_QUICK_FP16，这种设计决策背后的考量和权衡是什么？
  - baseline: emb=20 graph=0 fallback=0 | no_grep: emb=20 graph=0 fallback=0
- **[69]** 为什么在性能优化的背景下选择使用GGML_COMMON_IMPL_C来替代动态计算或函数调用，这种设计决策具体提升了哪些性能指标，同时可能带来了哪些潜在的性能风险？
  - baseline: emb=20 graph=0 fallback=0 | no_grep: emb=20 graph=0 fallback=0
- **[72]** 代码中GROUP_MAX_EPS_IQ3_XXS的定义和所有引用分别出现在什么位置？它们在程序的数据流或控制流中是如何传递和影响执行路径的？
  - baseline: emb=20 graph=0 fallback=1 | no_grep: emb=20 graph=0 fallback=0
- **[79]** 代码库中所有BUFFER_TO_APIR_CONTEXT的定义和引用分别出现在哪些文件或代码位置？这些位置如何影响BUFFER_TO_APIR_CONTEXT的作用范围和程序行为？
  - baseline: emb=20 graph=0 fallback=1 | no_grep: emb=20 graph=0 fallback=0
- **[81]** 系统中如何设计和实现GGML_FILE_VERSION的加载、解析与应用流程，以确保它们在编译或构建阶段能够正确生效并影响最终程序行为？
  - baseline: emb=20 graph=0 fallback=0 | no_grep: emb=20 graph=0 fallback=0
- **[95]** 函数 ggml_metal_get_buffer_id 中的核心逻辑 (ggml/src/ggml-metal/ggml-metal-ops.cpp:16) 依赖了哪些外部变量或函
  - baseline: emb=20 graph=10 fallback=0 | no_grep: emb=20 graph=10 fallback=0
- **[146]** 在哪里可以找到 llama_grammar_element 的完整定义及其所有使用和引用的代码位置？
  - baseline: emb=20 graph=0 fallback=0 | no_grep: emb=20 graph=0 fallback=0
- **[170]** 为什么 GgmlOvDecoder::create_weight_node 的实现方式选择了当前的算法或数据结构，这种选择对性能有哪些具体影响？
  - baseline: emb=20 graph=4 fallback=0 | no_grep: emb=20 graph=4 fallback=0
- **[181]** 系统中 ggml_conv_3d 是如何与其他组件交互以完成其功能的，整体的调用流程和数据流是怎样设计的？
  - baseline: emb=20 graph=4 fallback=0 | no_grep: emb=20 graph=4 fallback=0
- **[196]** ggml_set_f32_1d 依赖了哪些其他函数或模块，这些依赖之间是如何相互关联的？
  - baseline: emb=20 graph=2 fallback=0 | no_grep: emb=20 graph=2 fallback=0
- **[204]** 为什么 ggml_backend_vk_device_event_free 选择了当前的实现方式以优化性能，在不同场景下这种设计带来了哪些性能上的提升或潜在瓶颈？
  - baseline: emb=20 graph=0 fallback=0 | no_grep: emb=20 graph=0 fallback=0
- **[205]** ggml_backend_is_zdnn 在代码中被哪些地方调用或引用，这些调用点对程序的控制流程有何影响？
  - baseline: emb=20 graph=0 fallback=0 | no_grep: emb=20 graph=0 fallback=0
- **[209]** common_peg_parser_builder::python_number 在代码库中的定义、调用和引用具体分布在哪些位置？
  - baseline: emb=20 graph=10 fallback=0 | no_grep: emb=20 graph=10 fallback=0
- **[225]** iq2_entry_t.* map 的结构和组成部分包括哪些？
  - baseline: emb=20 graph=10 fallback=0 | no_grep: emb=20 graph=10 fallback=0
- **[233]** 为什么在设计 llama_grammar_element.value 时选择当前的可变性和初始化方式？这种设计决策对代码的安全性、性能和可维护性有哪些具体影响？
  - baseline: emb=20 graph=10 fallback=0 | no_grep: emb=20 graph=10 fallback=0
- **[242]** 系统中 iq3_entry_t.* neighbours 的赋值和读取操作分别出现在代码的哪些位置？它们之间的数据流向是如何传递和控制的？
  - baseline: emb=20 graph=0 fallback=2 | no_grep: emb=20 graph=0 fallback=0
- **[261]** 函数 ggml_cpy_f32_q4_0_sycl 的参数 ne11 在函数签名中具体指代什么，其定义和基本属性包括哪些方面？
  - baseline: emb=20 graph=0 fallback=0 | no_grep: emb=20 graph=0 fallback=0
- **[275]** 函数 ggml_mul_mat_p021_f16_f32_sycl 的参数 stream 在程序的哪些函数调用路径中被传递和使用，其数据流和控制流是如何沿调用链展开的？
  - baseline: emb=20 graph=10 fallback=0 | no_grep: emb=20 graph=10 fallback=0
- **[289]** 系统中 rms_norm_mul_rope_view_set_rows_edges {
    { 1, 0, 0 }, // mul->src 的定义位置和作用范围是什么？
  - baseline: emb=20 graph=10 fallback=0 | no_grep: emb=20 graph=10 fallback=0
- **[292]** batched_bench_output_jsonl 在程序中通常承载哪些类型的数据或信息？
  - baseline: emb=20 graph=0 fallback=0 | no_grep: emb=20 graph=0 fallback=0
- **[296]** HASH_THRESHOLD 依赖于哪些其他代码元素，其值的来源和影响链路是怎样的？
  - baseline: emb=20 graph=0 fallback=1 | no_grep: emb=20 graph=0 fallback=0
- **[305]** 为什么在性能优化的角度下，设计中选择以当前方式定义和使用 per_layer_proj_norm，其对程序执行效率和资源消耗有哪些具体影响？
  - baseline: emb=20 graph=1 fallback=0 | no_grep: emb=20 graph=1 fallback=0
- **[307]** rdna2_pipelines 在代码的哪些位置被定义、赋值或引用，其数据流向和控制流是如何传递的？
  - baseline: emb=20 graph=0 fallback=1 | no_grep: emb=20 graph=0 fallback=0
- **[312]** set_tensor_data_ud 在代码中具体在哪些位置被声明、初始化和引用？
  - baseline: emb=20 graph=0 fallback=0 | no_grep: emb=20 graph=0 fallback=0
- **[323]** 如何通过API或框架提供的接口来创建、访问和管理 mul_mat_id_param_count 的生命周期和状态？
  - baseline: emb=20 graph=0 fallback=2 | no_grep: emb=20 graph=0 fallback=0

---

## 三、Graph 增益 (29 道题)

### 3.1 检索特征

- graph 召回增加: 14 题
- graph 召回相同: 15 题
- baseline 平均 graph 召回: 3.9 个函数
- no_graph 平均 graph 召回: 0.0 个函数

### 3.2 核心规律

Graph 是 Embedding 的"关系补丁"，主要解决：

1. **跨文件调用链**: 模块依赖、数据流分析需要理解 caller→callee 关系
2. **间接依赖追踪**: "bert 依赖哪些模块"需要递归追踪调用链
3. **结构体/类型关系**: 类型定义和引用分散在不同文件
4. **设计意图推断**: 通过调用上下文理解为什么这样设计

### 3.3 代表性题目

- **[8]** commonpp 依赖了哪些外部资源或模块，这些依赖之间的调用关系和数据流是怎样的？
  - baseline: emb=20 graph=0 fallback=1 | no_graph: emb=20 graph=0 fallback=1
- **[17]** 为什么在设计 llama-hparams 时选择了当前的架构或实现方式，这些选择如何影响了系统的性能表现和资源利用效率？
  - baseline: emb=20 graph=10 fallback=0 | no_graph: emb=20 graph=0 fallback=0
- **[27]** 在系统整体架构中，ggml-openvino-extra 是如何实现内部功能协同和对外服务提供的，具体的工作流程和通信机制是什么？
  - baseline: emb=20 graph=10 fallback=0 | no_graph: emb=20 graph=0 fallback=0
- **[41]** 为什么在 quants.c 中采用了当前的代码结构和实现方式，这些设计决策背后的目的和预期效果是什么？
  - baseline: emb=20 graph=10 fallback=0 | no_graph: emb=20 graph=0 fallback=1
- **[43]** 在 ggml-alloc.c 中，某个关键变量或数据的定义、赋值和使用分别位于哪些具体位置，它们之间的数据流向是怎样的？
  - baseline: emb=20 graph=10 fallback=0 | no_graph: emb=20 graph=0 fallback=0
- **[47]** 在 ggml.c 中，某个特定标识符（如变量、函数或类）的定义和所有引用位置分别在哪里？
  - baseline: emb=20 graph=10 fallback=0 | no_graph: emb=20 graph=0 fallback=0
- **[60]** 系统中各个GGUF_MAX_STRING_LENGTH之间存在哪些依赖关系，这些依赖是如何被建立、解析和维护的？
  - baseline: emb=20 graph=0 fallback=0 | no_graph: emb=20 graph=0 fallback=0
- **[72]** 代码中GROUP_MAX_EPS_IQ3_XXS的定义和所有引用分别出现在什么位置？它们在程序的数据流或控制流中是如何传递和影响执行路径的？
  - baseline: emb=20 graph=0 fallback=1 | no_graph: emb=20 graph=0 fallback=1
- **[79]** 代码库中所有BUFFER_TO_APIR_CONTEXT的定义和引用分别出现在哪些文件或代码位置？这些位置如何影响BUFFER_TO_APIR_CONTEXT的作用范围和程序行为？
  - baseline: emb=20 graph=0 fallback=1 | no_graph: emb=20 graph=0 fallback=1
- **[81]** 系统中如何设计和实现GGML_FILE_VERSION的加载、解析与应用流程，以确保它们在编译或构建阶段能够正确生效并影响最终程序行为？
  - baseline: emb=20 graph=0 fallback=0 | no_graph: emb=20 graph=0 fallback=0
- **[95]** 函数 ggml_metal_get_buffer_id 中的核心逻辑 (ggml/src/ggml-metal/ggml-metal-ops.cpp:16) 依赖了哪些外部变量或函
  - baseline: emb=20 graph=10 fallback=0 | no_graph: emb=20 graph=0 fallback=0
- **[111]** 系统是如何通过 函数 postprocess_cpu_params 中的核心逻辑 (common/common.cpp:264) 实现其核心功能或业务流程的？
  - baseline: emb=20 graph=10 fallback=0 | no_graph: emb=20 graph=0 fallback=0
- **[133]** 为什么 * ggml_backend_metal_reg_t 的设计选择了当前的抽象层次和成员分布，这样的设计在实现系统目标和维护需求上起到了什么样的作用？
  - baseline: emb=20 graph=0 fallback=0 | no_graph: emb=20 graph=0 fallback=0
- **[137]** 为什么在设计 iq2_entry_t 时选择了当前的成员变量和方法实现方式，这些设计决策对系统性能有何具体影响及优化考量？
  - baseline: emb=20 graph=0 fallback=1 | no_graph: emb=20 graph=0 fallback=1
- **[177]** ggml_sycl_neg 的定义和实现分别位于代码库中的哪些位置？
  - baseline: emb=20 graph=1 fallback=0 | no_graph: emb=20 graph=0 fallback=0
- **[193]** ggml_opt_dataset_get_batch_host 在程序中承担的功能定位和结构组成是什么？
  - baseline: emb=20 graph=2 fallback=0 | no_graph: emb=20 graph=0 fallback=0
- **[204]** 为什么 ggml_backend_vk_device_event_free 选择了当前的实现方式以优化性能，在不同场景下这种设计带来了哪些性能上的提升或潜在瓶颈？
  - baseline: emb=20 graph=0 fallback=0 | no_graph: emb=20 graph=0 fallback=0
- **[225]** iq2_entry_t.* map 的结构和组成部分包括哪些？
  - baseline: emb=20 graph=10 fallback=0 | no_graph: emb=20 graph=0 fallback=2
- **[240]** iq2_entry_t.* grid 在代码中被哪些模块或函数引用和修改？它的数据流向和控制流路径是怎样的？
  - baseline: emb=20 graph=7 fallback=0 | no_graph: emb=20 graph=0 fallback=0
- **[261]** 函数 ggml_cpy_f32_q4_0_sycl 的参数 ne11 在函数签名中具体指代什么，其定义和基本属性包括哪些方面？
  - baseline: emb=20 graph=0 fallback=0 | no_graph: emb=20 graph=0 fallback=0
- **[277]** 函数 ggml_vk_find_memory_properties 的参数 * mem_props 在代码库中的哪些位置被定义、传递和引用？
  - baseline: emb=20 graph=0 fallback=0 | no_graph: emb=20 graph=0 fallback=0
- **[282]** 代码库中 函数 ggml_new_tensor_3d 的参数 * ctx 在哪些函数或模块的位置被声明、传递和调用？
  - baseline: emb=20 graph=10 fallback=0 | no_graph: emb=20 graph=0 fallback=0
- **[288]** 在当前使用的 API 或框架中，如何定义、传递和处理 函数 dequantize_mul_mat_vec_q8_0_sycl 的参数 nrows 以实现其自动校验和类型检查？
  - baseline: emb=20 graph=10 fallback=0 | no_graph: emb=20 graph=0 fallback=0
- **[290]** 系统架构中 type_traits_cpu 的组织结构和层级关系是怎样的？
  - baseline: emb=20 graph=2 fallback=0 | no_graph: emb=20 graph=0 fallback=0
- **[296]** HASH_THRESHOLD 依赖于哪些其他代码元素，其值的来源和影响链路是怎样的？
  - baseline: emb=20 graph=0 fallback=1 | no_graph: emb=20 graph=0 fallback=1
- **[304]** 为什么在性能优化方面选择以当前方式管理和使用 subgroup_size，其对程序执行效率有什么具体影响？
  - baseline: emb=20 graph=0 fallback=1 | no_graph: emb=20 graph=0 fallback=1
- **[307]** rdna2_pipelines 在代码的哪些位置被定义、赋值或引用，其数据流向和控制流是如何传递的？
  - baseline: emb=20 graph=0 fallback=1 | no_graph: emb=20 graph=0 fallback=1
- **[318]** 如何在算法实现中通过操作 subgroup_size_32 来保证数据的正确传递和计算流程？
  - baseline: emb=20 graph=0 fallback=1 | no_graph: emb=20 graph=0 fallback=1
- **[322]** 如何通过API或框架支持实现对 bool 的创建、访问和生命周期管理？
  - baseline: emb=20 graph=0 fallback=2 | no_graph: emb=20 graph=0 fallback=2

---

## 四、综合对比

| 维度 | Embedding | Grep | Graph |
|------|-----------|------|-------|
| **增益题数** | 125 | 31 | 29 |
| **核心机制** | 语义相似度召回 | fallback 精确匹配 | Graph CALLS 关系 |
| **答错主因** | 检索证据严重不足 | 未找到定义/位置 | 未找到调用关系 |
| **典型题型** | 设计决策、功能分析 | 宏定义、位置定位 | 依赖关系、数据流 |

---

*报告生成时间: 2026-05-05*
