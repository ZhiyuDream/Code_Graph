# llama.cpp Graph-Agent Good Case 与 Bad Case 分析报告

## 1. 实验概述

- **数据来源**: `judge_20260403_232050.json` + `graph_agent_20260403_232050.json`
- **评测时间**: 2026-04-03 23:20
- **评测方法**: Graph+RAG Hybrid + Embedding Fallback
- **总题目数**: 360
- **ag_score平均**: 0.7257 (72.6%)
- **正确率(>=0.5)**: 77.8% (280/360)

---

## 2. 按类别统计

| 类别 | 题数 | 平均分 | 正确率 |
|------|------|--------|--------|
| **文件级** | 72 | 0.8299 | 88.9% |
| **变量级** | 108 | 0.7546 | 82.4% |
| **模块级** | 36 | 0.7153 | 75.0% |
| **代码级** | 72 | 0.6736 | 70.8% |
| **函数级** | 72 | 0.6354 | 68.1% |

**发现**: 题目粒度越细，正确率越低。函数级正确率最低(68.1%)。

---

## 3. Good Case 示例

### 案例1：满分 - 模块级 Namespace（Index: 4, Score: 1.0）

**Question:**
```
什么是 hunyuan-dense，它在整个代码体系中扮演什么角色，包含哪些主要内容？
```

**Agent Answer:**
```
hunyuan-dense 是 llama.cpp 代码库中位于 src/models 目录下的一个模型实现，核心代码位于文件
src/models/hunyuan-dense.cpp 中。该文件中主要包含一个构造函数 llm_build_hunyuan_dense，
它负责根据传入的模型参数和图参数构建 Hunyuan-dense 模型的计算图。

主要内容包括：
1. 输入层的构建（词嵌入、位置编码）
2. 多层 Transformer 结构的堆叠，每层包括：
   - 归一化层（norm）
   - 自注意力机制（Q、K、V计算，带LoRA）
   - 旋转位置编码（RoPE）处理
```

**Reference:**
```
从现有注释可知模块 hunyuan-dense 仅显式包含符号 llm_build_hunyuan_dense::llm_build_hunyuan_dense；
除名称与作用域外无实现或接口信息，无法确定其职责、用法与依赖关系。
```

**分析**: Agent回答详细解释了模型架构，Reference反而信息很少。Agent回答质量超越Reference。

---

### 案例2：满分 - 模块级（Index: 5, Score: 1.0）

**Question:**
```
什么是 glm4-moe，它包含哪些主要的功能或组件，这些功能或组件之间的关系如何？
```

**Agent Answer:**
```
glm4-moe 是 llama.cpp 代码库中的一个模型实现，基于 GLM-4 模型架构，并结合了 MoE
（Mixture of Experts，专家混合）机制。

主要功能：
1. 输入嵌入构建（build_inp_embd, build_inp_pos）
2. Transformer 层的堆叠
3. 多头自注意力机制（Q、K、V线性变换，带LoRA加速）
4. 多种 RoPE 编码支持
```

**Reference:**
```
当前片段仅指明模块glm4-moe及其唯一函数入口。模块围绕该函数组织，但缺少实现与接口细节，
无法判断内部组件与其交互关系。
```

**分析**: Agent解释了MoE架构细节，Reference信息匮乏。Agent大幅超越Reference。

---

## 4. Bad Case 示例

### 案例1：低分 - 模块级（Index: 0, Score: 0.25）

**Question:**
```
这个 ggml-blas 主要包含哪些核心功能或组件，它们之间是如何组织和协作的？
```

**Agent Answer:**
```
根据语义搜索结果，llama.cpp 代码库中没有明确叫做 "ggml-blas" 的独立模块或目录，
但在 ggml/src/ggml-cpu/llamafile/sgemm.cpp 文件中实现了一个名为 tinyBLAS 的组件，
核心功能是执行小型矩阵乘法。
```

**Reference:**
```
该模块以后端接口形式暴露少量关键函数，覆盖矩阵乘与外积两类算子、图级执行、后端标识与资源释放。

[使用场景] 提供矩阵乘算子：mul_mat 作为核心数值运算 [行号: 2]
[使用场景] 提供外积算子：out_prod 覆盖外积运算需求 [行号: 3]
[设计意图] 图级执行入口：graph_compute 负责整体计算调度 [行号: 6]
```

**Reason:**
```
Graph-Agent 答案对 ggml-blas 的定位较为模糊，未能准确覆盖核心函数和组件细节，
仅提及 tinyBLAS，缺乏对算子和图级执行的分层协作描述。
```

**根因**: Agent检索命中偏差，把"ggml-blas"误解为"tinyBLAS"，而正确答案是ggml后端接口（mul_mat、out_prod等）。

---

### 案例2：低分 - 模块级（Index: 1, Score: 0.25）

**Question:**
```
apertus 包含哪些子模块或子组件，它们之间的层级关系和职责划分是怎样的？
```

**Agent Answer:**
```
apertus 目录在 llama.cpp 代码库中不存在或无子节点，因此无法直接获取其子模块或子组件的层级关系。
请确认 apretus 的名称是否正确。
```

**Reference:**
```
从给定代码仅能确认 apertus 模块暴露了 llm_build_apertus 作用域下的同名函数。
可归纳的最小层级为：模块 -> 作用域 -> 函数

[实现细节] 最小可见层级：apertus 模块 -> llm_build_apertus 作用域 -> llm_build_apertus 函数 [行号: 1,2]
```

**Reason:**
```
Agent 明确表示找不到 apertus，但参考答案是能找到其函数定义的。
说明 Agent 检索深度不够，仅做了表层搜索就放弃。
```

**根因**: Agent过早放弃，检索策略不当。Reference指出可以找到函数定义，但Agent直接放弃。

---

### 案例3：低分 - 模块级（Index: 3, Score: 0.25）

**Question:**
```
什么是 concat 在代码结构中的作用和定位？
```

**Agent Answer:**
```
concat 在代码中主要用于张量拼接操作。从 llama.cpp 库来看，concat 相关功能主要实现
张量沿着某个维度的拼接操作...
```

**Reference:**
```
在该结构中，concat 模块仅通过注释暴露两个函数入口：elem_size 与 ggml_sycl_op_concat，
指向尺寸查询与拼接算子实现，体现出以后端为中心的精简职责边界。

[设计意图] 模块职责聚焦于拼接相关功能，由 concat 统一承载 [行号: 1]
[设计意图] 以 elem_size 作为尺寸查询的辅助能力，支撑主拼接算子 [行号: 2]
```

**Reason:**
```
Agent 对 concat 的解释偏向通用概念，未能结合代码中具体的函数入口（elem_size, ggml_sycl_op_concat）
说明其实现定位。
```

---

### 案例4：低分 - Why类问题（Index: 12, Score: 0.25）

**Question:**
```
为什么选择以 virtgpu-forward-backend 作为代码组织的边界，这种划分对系统的可维护性和扩展性带来了什么具体好处？
```

**Agent Answer:**
```
virtgpu-forward-backend 作为一个代码组织边界，可能是因为它将虚拟GPU前向传播相关的功能
聚合在一起，便于维护和扩展...
```

**Reference:**
```
以 virtgpu-forward-backend 为边界，仅暴露 current_time_ms 与 apir_backend_graph_compute 两个入口，
形成窄接口，隔离上层与后端细节。该划分便于在统一入口做性能计时与调度，
但时间工具与后端耦合、接口语义收窄可能影响复用与未来扩展。

[设计意图] 选取 virtgpu-forward-backend 作为边界，意图以模块名界定前向路...
```

**Reason:**
```
Agent 的回答较为笼统，未能具体指出两个入口函数（current_time_ms, apir_backend_graph_compute），
也未能分析接口收窄对复用性的影响。
```

---

## 5. 问题根因分析

### 5.1 检索命中偏差

```
表现：问题关键词匹配到语义相似但实际不同的内容
案例：ggml-blas → tinyBLAS（实际是ggml后端接口 mul_mat/out_prod）

根因：检索系统没有校验检索结果与问题主题的相关性
```

### 5.2 检索过早放弃

```
表现：第一次检索失败后直接返回"未找到"
案例：apertus 模块案例

根因：缺乏检索失败后的重试机制（如换同义词、换检索点）
```

### 5.3 Why类问题回答笼统

```
表现：回答停留在概念层面，未结合代码中的具体实现
案例：virtgpu-forward-backend 问题

根因：Agent未能从Reference中提取关键函数名和分析要点
```

---

## 6. 改进建议

1. **检索结果语义校验**
   - 检索后让LLM先判断结果是否与问题相关
   - 不相关则换检索策略

2. **防止过早放弃**
   - 检索失败后自动尝试同义词/别名
   - 增加最大检索步数

3. **Reference信息利用**
   - 训练Agent从Reference中提取关键函数名和分析要点
   - 避免回答停留在概念层面

---

*评测时间: 2026-04-03 23:20*
