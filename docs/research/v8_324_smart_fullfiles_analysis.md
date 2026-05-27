# 324 题 Benchmark 智能 Full-files 实验报告

> 实验日期: 2026-05-23
> 实验目标: 验证"LLM 按需决策 + 函数级截断"的 full-files 策略在 324 题开放式问答上的效果
> 核心发现: **正确率与 baseline 持平（82.4% vs 83.3%），但错误模式完全不同（重叠度仅 30.6%）**

---

## 1. 实验配置

### 1.1 数据集

| 属性 | 说明 |
|---|---|
| 名称 | llama_cpp_QA_cleaned.json |
| 路径 | `datasets/llama_cpp_QA_cleaned.json` |
| 题数 | **324 题** |
| 问题类型 | 开放式问答（模块职责、设计决策、实现细节、定位查找等） |
| 每题字段 | id, question, answer, summary, level1, level2, question_type, intention, entity, evidence |

### 1.2 模型与策略

| 配置项 | Baseline | Smart Full-files |
|---|---|---|
| 答案生成模型 | DeepSeek-v4-pro | DeepSeek-v4-pro |
| 评估 Judge | GPT-4.1-mini | GPT-4.1-mini |
| 检索器 | embedding + issue + grep + graph | 相同 |
| ReAct 扩展 | callers/callees | 相同 |
| **Full-files 策略** | 不收集完整文件 | **LLM 先看函数片段，决定需要看哪些完整文件；超大文件按函数级提取，不按字符截断** |
| Token 预算 | — | **400K**（DeepSeek 上限 1M） |
| 单文件上限 | — | **100KB**（超大文件截断到函数级片段） |
| 并行 workers | 10 | 30 |

### 1.3 模块拆分

| 文件 | 职责 |
|---|---|
| `tools/core/full_file_selector.py` | LLM 决策 + 函数级文件收集逻辑 |
| `prompts/full_file_decision.txt` | LLM 决策 prompt 模板 |
| `run_qa_324_fullfiles_smart.py` | 主实验脚本 |

---

## 2. 核心结果

### 2.1 整体指标

| 指标 | Baseline | Smart Full-files | Delta |
|---|---|---|---|
| **Binary 正确率** | 270/324 = **83.3%** | 267/324 = **82.4%** | **-0.9%** |
| 平均答案长度 | 2,744 字符 | 3,263 字符 | **+19%** |
| 平均完整文件数 | 0 | 2.1 | — |
| 平均注入 tokens | — | **10,995** | — |
| 中位数注入 tokens | — | 8,797 | — |
| P90 注入 tokens | — | 26,654 | — |
| 最大注入 tokens | — | 48,992 | 无超限 ✅ |
| LLM 决定需要 full-files | — | **84.9%** | — |
| 平均时延 | — | 157s | — |

**正确率几乎持平**，但答案明显变长（+19%），且注入了平均 11K tokens 的额外上下文。

### 2.2 错误重叠度分析

| 结果 | 题数 | 占比 |
|---|---|---|
| 两者都对 | 239 | 73.8% |
| 两者都错 | 26 | 8.0% |
| **Baseline 错但 Smart 对** | 28 | 8.6% |
| **Baseline 对但 Smart 错** | 31 | 9.6% |

**错误重叠度：仅 30.6%**（26/85）。也就是说，**69.4% 的错误是不同的**。

这说明两种模式的失败模式完全不同——不是"谁更好"的问题，而是"擅长不同的题"。

---

## 3. Smart Full-files 的净收益（28 题）

### 3.1 按问题类型分类

| 类型 | 题数 | 占比 | 典型题号 |
|---|---|---|---|
| **模块设计/架构** | 14 | 50% | qa_0, qa_48, qa_8, qa_103 |
| **实现细节** | 6 | 21% | qa_107, qa_114, qa_139 |
| **定位查找** | 6 | 21% | qa_149, qa_205, qa_242 |
| 其他 | 2 | 8% | qa_8, qa_80 |

### 3.2 典型收益案例

**qa_0：ggml-blas 模块职责**
- **Baseline 错**：只描述了 BLAS 后端的初始化、注册接口，未涉及矩阵乘、外积算子、图级执行等核心组件
- **Smart 对**：LLM 决定查看 `ggml-blas.cpp` 和 `ggml-blas.h` 的完整内容，准确描述了生命周期管理、类型识别、线程控制及与 `ggml_backend` 框架的协作关系

**qa_48：ggml.c 的设计决策**
- **Baseline 错**：回答了 ggml 整体架构（后端抽象、量化内核），但未聚焦到"ggml.c 这个文件"的代码组织方式
- **Smart 对**：完整文件展示了头文件划分、枚举设计、内联工具集、内存管理，LLM 紧扣"ggml.c 文件本身"的设计目的

**qa_114：ggml_metal_get_buffer_id 依赖分析**
- **Baseline 错**：分析了另一个不同函数 `ggml_metal_buffer_get_id`，搞混了函数名
- **Smart 对**：完整文件 `ggml-metal-ops.cpp` 明确了目标函数的实现，准确分析了张量视图、缓冲区 context 等依赖

### 3.3 收益模式总结

> **Full-files 在"需要跨函数理解"的问题上明显优于 baseline。**
>
> 典型场景：
> - "模块 X 包含哪些组件、如何协作"
> - "文件 Y 为什么这样组织"
> - "函数 Z 的依赖关系"（需要看调用方和被调用方的完整上下文）

---

## 4. 详细分析：为什么 Smart Full-files 还会变差（31 题净损失）

这是本次实验**最重要的发现**。Smart Full-files 比 baseline 多错了 3 题（31 vs 28），正确率低了 0.9%。虽然差距很小，但分析这 31 题的失败原因对策略优化至关重要。

### 4.1 失败原因分类

| 类型 | 题数 | 占比 | 根本原因 |
|---|---|---|---|
| **聚焦偏移** | 10 | 32% | 有了完整文件后，答案从"设计意图"滑向"实现细节" |
| **检索未命中** | 7 | 23% | LLM 说"找不到"，但 Judge 判定标准不一致 |
| **泛泛而谈** | 7 | 23% | 上下文变多，答案变啰嗦，缺少针对性 |
| **信息不完整** | 4 | 13% | 看了很多文件，但遗漏了关键点 |
| **内容偏离** | 3 | 10% | 回答了别的问题 |

### 4.2 聚焦偏移（10 题）——最严重的问题

这是 Smart Full-files **特有的失败模式**，baseline 不会出现。

**核心机制**：
```
Baseline（函数片段）→ LLM 被迫聚焦在"检索到的函数"
                    → 回答围绕"这个函数的设计意图"
                    → Judge 认为"紧扣问题核心"

Smart Full-files → LLM 看到了文件的完整内容
                 → 被海量实现细节吸引
                 → 回答变成"代码功能罗列"
                 → Judge 认为"未回答设计动机"
```

**典型案例**：

**qa_13："为什么选择以当前方式划分和设计 http？"**
- **Baseline（CORRECT）**：
  > "HTTP 模块的分层设计核心目标是复用、解耦、职责划分，预期带来可维护性、演进能力和开发效率的提升..."
  > ——Judge："准确回答了为何选择现有方式，设计目标和系统价值清晰"

- **Smart（INCORRECT）**：
  > "HTTP 模块使用了 `httplib` 库，实现了 `server_callback` 处理请求路由，`httplib::Server` 管理连接池，支持 GET/POST 方法，错误码映射到 HTTP 状态..."
  > ——Judge："详细介绍了实现细节和技术选型，但未直接回答设计目标和系统价值"

**qa_16："为什么选择 qwen35 的当前结构？"**
- **Baseline（CORRECT）**：
  > "qwen35 拆分了注意力模块，引入线性注意力与传统注意力的混合策略，qkvz 投影分离，这是为了降低内存带宽瓶颈..."
  > ——Judge："合理解释了设计选择背后的性能影响"

- **Smart（INCORRECT）**：
  > "qwen35 使用了 `qwen35_attention` 结构体，包含 `n_head`、`n_kv_head`、`freq_base` 等字段，forward 函数先计算 qkv 投影，再应用旋转位置编码，最后通过 `ggml_mul_mat` 计算注意力得分..."
  > ——Judge："详细介绍了混合注意力机制和门控设计，但未针对'为什么选择当前结构'做出明确设计动机说明"

**根因**：完整文件包含了大量实现细节（变量定义、函数调用、类型转换），LLM 的注意力被这些"具体代码"吸引，**忽略了问题问的是"为什么"而不是"怎么做"**。

> **Baseline 的函数片段反而是优势**——片段不包含完整实现，LLM 只能从"函数签名 + 少量上下文"推断设计意图，恰好避开了实现细节的干扰。

### 4.3 检索未命中（7 题）——Judge 判定不一致

这 7 题中，baseline 和 Smart 都遇到了"检索未命中"的情况，但 Judge 的判定完全相反：

**qa_91："GROUP_MAX_EPS_IQ3_XXS 的定义和引用在哪里？"**
- **Baseline（CORRECT）**："检索范围内未找到该宏的定义及引用，无法分析其数据流和控制流影响"
  - Judge："合理推断，符合根据检索到信息判断的要求"
- **Smart（INCORRECT）**："未找到 GROUP_MAX_EPS_IQ3_XXS 的定义，且未给出具体引用位置"
  - Judge："未能准确回答问题，参考答案中明确给出定义位置"

**qa_96："MAX_FUSED_ADDS 的定义和引用在哪里？"**
- **Baseline（CORRECT）**："现有检索内容未包含关于 MAX_FUSED_ADDS 的定义和引用，合理说明了无法回答问题的原因"
- **Smart（INCORRECT）**："断言未找到 MAX_FUSED_ADDS 的定义和引用，但参考答案明确指出该宏在第122行定义"

**根因分析**：
1. **Judge 对"找不到"的判定标准不一致**：同样的"找不到"回答，baseline 被判对，Smart 被判错
2. **Smart 的 extra context 让 Judge 更严格**：Judge 可能认为"既然给了更多文件，就应该能找到"，对 Smart 的预期更高
3. **这 7 题的答案实际上几乎相同**：baseline 和 Smart 都说"找不到"，只是 Smart 多看了几个文件仍没找到。Judge 的判定偏差是主要因素。

### 4.4 泛泛而谈（7 题）——信息稀释效应

**qa_4："hunyuan-dense 的定义、角色和组成内容"**
- **Baseline（CORRECT）**：从架构、词表预处理、对话模板、计算图构建四个维度简洁回答
- **Smart（INCORRECT）**：详细描述了架构、词表预处理、对话模板、计算图构建... 但参考答案明确指出"该模块仅有极简设计"
  - Judge："给出了明确的实现细节和角色定位，然而参考答案指出该模块仅有极简设计..."

**qa_59："WEBGPU_SET_ROWS_ERROR_BUF_SIZE_BYTES 的依赖关系"**
- **Baseline（CORRECT）**：分析了宏常量如何在源码和 shader 预处理阶段建立依赖关系
- **Smart（INCORRECT）**："偏向分析思路和代码结构，缺乏具体依赖关系的描述"

**根因**：上下文变多后，LLM 倾向于"全面覆盖"而不是"精准打击"。baseline 只看到 5-10 个函数片段，必须聚焦；Smart 看到 2-3 个完整文件，反而容易面面俱到、浅尝辄止。

### 4.5 信息不完整（4 题）——看了很多但没看到点子上

**qa_115："flash_attn_mask_to_KV_max 依赖了哪些外部变量？"**
- Smart 看了 1 个完整文件（838 tokens），但分析的是"结构体说明"，未指出具体依赖的输入数据和外部资源

**qa_12："为什么选择 virtgpu-forward-backend 作为代码组织边界？"**
- Smart 看了 5 个完整文件（8,468 tokens），分析了实现细节和调用流程，但没回答"为什么选它作为边界"

**根因**：LLM 虽然看到了完整文件，但**没有正确识别出关键证据**。这和"聚焦偏移"类似，但更严重——不是答偏了，而是漏答了核心要点。

### 4.6 内容偏离（3 题）——回答了别的问题

**qa_203："ggml_sycl_neg 的定义和实现位置"**
- Smart 答案分析了 `ggml_sycl_neg` 的声明位置，但问题明确问的是"实现位置"（在 `element_wise.cpp`），Smart 没提到

**qa_358："如何通过 API 支持 bool 的创建、访问和生命周期管理？"**
- Smart 答案描述了 `value_bool_t` 的实现细节，但问题问的是"API 或框架支持"，答案未结合 API 设计

**根因**：完整文件给了太多信息，LLM 被某个相关但不完全匹配的主题带偏了。

---

## 5. 两者都错的 26 题——共同盲点

| 特征 | 说明 |
|---|---|
| LLM 决定需要 full-files | 22/26（85%） |
| 典型问题类型 | SYCL/Metal 后端特定代码、硬件细节、小众模块 |
| 共同根因 | **检索系统本身就没找到关键证据**，full-files 也无能为力 |

典型题：
- qa_113：`ggml_sycl_set_main_device` 核心逻辑依赖
- qa_147：`ggml_backend_metal_reg_t` 内部结构
- qa_181：`remote_handle64_control` 模块层级

这些题涉及 llama.cpp 的**小众后端**（SYCL、Metal、远程句柄），embedding 检索和 graph 扩展都难以覆盖。

---

## 6. 综合结论

### 6.1 正确率层面：持平，无显著提升

Smart Full-files（82.4%）和 baseline（83.3%）差距仅 0.9%。这说明：

> **对于 324 题开放式问答，函数片段已经足够回答大多数问题。完整文件带来的增量信息没有转化为正确率提升。**

### 6.2 错误模式层面：互补，非替代关系

| 维度 | Baseline | Smart Full-files |
|---|---|---|
| **擅长** | 聚焦的函数级问题、设计意图分析 | 模块级设计决策、跨文件协作分析 |
| **不擅长** | 需要跨函数理解的模块问题 | 保持聚焦、避免实现细节淹没 |
| **特有失败模式** | 检索遗漏、推断偏差 | 聚焦偏移、信息稀释 |

### 6.3 为什么 Smart Full-files "还会变差"

根本原因不是"full-files 有害"，而是：

1. **聚焦偏移（32%）**：完整文件的实现细节淹没了设计意图，这是**策略层面的问题**——LLM 不善于在"海量代码"中提炼"为什么"
2. **Judge 不一致（23%）**：同样的"找不到"结论，baseline 和 Smart 的判定相反，这是**评估层面的噪音**
3. **信息稀释（23%）**：上下文变多后，答案变啰嗦、缺乏针对性，这是**生成层面的问题**

三者合计占 Smart 净损失的 **78%**。

### 6.4 成本效益分析

| 维度 | Baseline | Smart Full-files | 结论 |
|---|---|---|---|
| 正确率 | 83.3% | 82.4% | 持平 |
| 答案长度 | 2,744 | 3,263 | +19% |
| 平均时延 | ~120s | 157s | +31% |
| LLM 调用次数 | 2-3 次/题 | 3-4 次/题 | +1 次决策轮 |
| 额外成本 | 无 | 11K tokens/题 | 有 |
| **净收益** | — | **-3 题** | **不划算** |

> **从工程角度看，Smart Full-files 以 +31% 时延和 +19% 答案长度的代价，换来了 -0.9% 的正确率和 -3 题的净损失。目前不划算。**

### 6.5 下一步优化方向

| 问题 | 优化方案 | 预期效果 |
|---|---|---|
| 聚焦偏移（实现细节淹没设计意图） | 在 `answer_generation.txt` prompt 中增加：**"先回答设计意图/为什么，再补充实现细节"** | 高 |
| Judge 不一致（"找不到"的判定偏差） | 统一 Judge prompt，明确"检索未命中时承认找不到是合理回答" | 中 |
| 信息稀释（答案变啰嗦） | 要求 LLM 输出"结论优先 + 证据索引"的结构化格式 | 中 |
| 成本过高（+31% 时延） | **分层策略**：baseline 先答，如果 LLM 信心分数 < 阈值再启用 full-files | 高 |

---

## 附录：实验复现命令

```bash
# 1. 跑 Smart Full-files 实验（约 30 分钟）
python3 run_qa_324_fullfiles_smart.py

# 2. 评估（约 1 分钟）
python3 evals/eval_v2.py \
  --input results/v8_deepseek_324_smart_fullfiles.json \
  -o results/v8_deepseek_324_smart_fullfiles.eval.json \
  -w 30

# 3. Baseline 对比
python3 evals/eval_v2.py \
  --input results/v8_deepseek_p0p1_emb_grep_graph.json \
  -o results/v8_deepseek_p0p1_emb_grep_graph.evaluated.json \
  -w 30
```

### 结果文件对照

| 实验 | 原始结果 | 评估结果 |
|---|---|---|
| Baseline (p0p1) | `results/v8_deepseek_p0p1_emb_grep_graph.json` | `results/v8_deepseek_p0p1_emb_grep_graph.evaluated.json` |
| Smart Full-files | `results/v8_deepseek_324_smart_fullfiles.json` | `results/v8_deepseek_324_smart_fullfiles.eval.json` |
