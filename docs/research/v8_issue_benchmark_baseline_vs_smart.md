# Issue Benchmark (419题) Baseline vs Smart Full-files 评估报告

> 分析日期: 2026-05-25  
> 核心发现: **Smart Full-files 在 Issue benchmark 上显著优于 Baseline（61.1% vs 54.2%，+6.9%），与 324 QA benchmark 的持平结果形成鲜明对比。**

---

## 1. 总体结果

| 策略 | 正确率 | vs Baseline |
|------|--------|-------------|
| **Simple Embedding** (已有结果) | **98.8%** (414/419) | — |
| **Simple Graph+Emb** (已有结果) | **98.6%** (413/419) | — |
| **React Search Baseline** | **54.2%** (227/419) | — |
| **Smart Full-files** | **61.1%** (256/419) | **+6.9%** |

### 交叉分析

| | Baseline CORRECT | Baseline INCORRECT |
|---|---|---|
| **Smart CORRECT** | 171 (40.8%) | 85 (20.3%) |
| **Smart INCORRECT** | 56 (13.4%) | 107 (25.5%) |

- **Smart 纠正了 85 道 Baseline 错题**（20.3%）
- **Smart 退化了 56 道 Baseline 对题**（13.4%）
- **净提升: +29 题 = +6.9%**

---

## 2. 为什么与 324 QA 结果完全不同？

### 2.1 324 QA: 持平 (83.3% vs 82.4%)

324 题是**开放式代码定位/设计推断**问题：
- 函数片段通常已包含足够信息
- 问题类型不需要跨文件理解
- 加完整文件引入信息干扰 > 帮助

### 2.2 Issue Benchmark: Smart 领先 (54.2% vs 61.1%)

Issue 题是**真实 GitHub issue 的 bug/构建/功能问题**：
- 需要理解**代码上下文**（变量定义、配置宏、编译条件）
- 需要**跨文件追溯**（bug 根因可能在调用链上游）
- 完整文件提供**实现细节**（函数内部逻辑、错误处理路径）

**Smart 的完整文件在这些场景下真正发挥了价值。**

---

## 3. 关键发现：简单检索器 98.8% 的"作弊"

### 3.1 检索对象完全不同

| 检索器 | 检索目标 | 索引文件 |
|--------|----------|----------|
| Simple Embedding | **函数 + Issue 节点** | `qa_embedding_index.json` |
| React Search | **仅函数** | `classic_rag_index.json` |

**验证**: `qa_emb_419.json` 的检索结果示例：
```
[embedding] issue: issue_21353
[embedding] issue: issue_18854
```

→ Simple Embedding **直接检索到了 issue 节点**，也就是参考答案的来源。这相当于"开卷考试"。

### 3.2 React Search 的设计不匹配

React Search 的流程是为**代码问答**设计的：
1. 语义搜索**函数**
2. Grep Fallback 搜索**函数**
3. LLM 决策扩展**调用链**（callers/callees）
4. 基于**函数片段**生成答案

但 Issue 问题中大量涉及：
- CI/CD 流程 (`bug_report_general`)
- 构建配置 (`build_install_*`)
- 模型格式 (`bug_report_model_gguf`)
- Docker/Nix 环境

这些问题的答案不在**函数调用链**中，而在**配置文件、CMakeLists、CI YAML** 中。React Search 的调用链扩展对这些问题完全无效。

### 3.3 修正后的公平对比

如果 Simple Embedding 也只能检索函数（不能检索 issue 节点），它的正确率也会大幅下降。

**React Search + Smart Full-files 才是真正的"闭卷考试"**——只给代码，让 LLM 自己从代码中推断 issue 的答案。

---

## 4. Smart 独对题分析（85 道）

### 4.1 按类型分布

| Issue 类型 | Smart 独对数 |
|-----------|-------------|
| bug_report_backend_hardware | 27 |
| bug_report_api_server | 24 |
| bug_report_model_gguf | 20 |
| bug_report_general | 6 |

→ **集中在 bug report 类问题**，这正是需要代码上下文理解的场景。

### 4.2 典型案例

**issue_21319**: "Windows CUDA 12 DLL 双重压缩"
- **Baseline**: 泛泛分析代码，未结合 CI/打包流程 → INCORRECT
- **Smart**: 查看 CMake 配置和 Makefile 完整文件，合理推断问题来自 CI → CORRECT
- **Smart 看了 2 个完整文件**

**issue_19579**: "WinGet 版本 8006 不支持 Qwen3"
- **Baseline**: 过度详细的代码分析，未指出版本过旧的核心原因 → INCORRECT
- **Smart**: 从完整源码确认架构支持情况，解释加载失败原因 → CORRECT
- **Smart 看了 3 个完整文件**

**issue_21333**: "macOS jinja 编译警告"
- **Baseline**: 分析了警告原因，但未提及维护者的修复尝试 → INCORRECT
- **Smart**: 查看完整 jinja 文件，分析 `[[noreturn]]` 属性使用 → CORRECT
- **Smart 看了 3 个完整文件**

### 4.3 模式总结

Smart 独对题的共性：
1. **需要查看非函数文件**（CMakeLists.txt、Makefile、CI 配置）
2. **需要理解函数在完整文件中的上下文**（宏定义、编译条件）
3. **需要跨文件关联**（bug 根因在 A 文件，表现在 B 文件）

---

## 5. Baseline 独对题分析（56 道）

### 5.1 典型案例

**issue_21429**: "Docker 不支持 CUDA 12.8"
- **Baseline**: 正确区分代码层面支持 vs Docker 镜像环境问题 → CORRECT
- **Smart**: 陷入代码细节分析，未明确结论 → INCORRECT
- **Smart 看了 1 个完整文件**

**issue_20575**: "llama-bench 错误使用 CPU"
- **Baseline**: 正确指出量化权重未映射到 GPU → CORRECT
- **Smart**: 分析缓冲区选择机制，未直接回答核心问题 → INCORRECT
- **Smart 看了 4 个完整文件**

### 5.2 退化原因

Smart 退化的典型模式：
1. **信息过载**：看了 4 个完整文件后，答案变成"代码目录"，失去焦点
2. **偏离问题**：完整文件中的实现细节分散了注意力，未回答 issue 的核心疑问
3. **过度推断**：基于完整代码做过多假设，反而偏离了参考答案的要点

---

## 6. 两者都错题分析（107 道）

### 6.1 典型案例

**issue_20237**: "性能回归 TG 从 37 降至 29"
- 两者都未定位到"寄存器溢出导致性能下降"的根因
- 只分析了模型结构和测试代码

**issue_19501**: "install target 失败 (KleidiAI)"
- 两者都分析了 CMake 代码，但未指出"KleidiAI 通过 FetchContent 下载后未正确排除在安装目标外"

### 6.2 根本原因

1. **检索未命中关键文件**：bug 根因在 CMake 配置或 CI 脚本中，但检索只命中了 C++ 函数
2. **需要项目维护知识**：某些答案需要知道"维护者 @pwilkin 尝试了什么"
3. **性能回归需要深度分析**：简单的代码查看不足以定位寄存器溢出等问题

---

## 7. 按难度分析

| 难度 | Baseline | Smart | 提升 |
|------|----------|-------|------|
| easy (243题) | 56.4% | 待补充 | — |
| medium (162题) | 48.8% | 待补充 | — |
| hard (14题) | 78.6% | 待补充 | — |

> hard 题正确率反而更高，可能是因为 hard 题通常涉及具体的代码 bug，完整文件更有价值。

---

## 8. 结论与建议

### 8.1 核心结论

1. **Smart Full-files 在 Issue benchmark 上有效**：+6.9%，净提升 29 题
2. **与 324 QA 形成鲜明对比**：Issue 问题需要代码上下文理解，QA 问题不需要
3. **简单检索器的 98.8% 是"作弊"**：直接检索 issue 节点，不是真实能力
4. **React Search 的设计需要改进**：应支持检索 issue、配置文件、CI 脚本等非代码节点

### 8.2 优化方向

1. **扩展检索范围**：React Search 应支持检索 issue 节点、CMakeLists、CI YAML 等
2. **减少信息过载**：Smart 看了 4 个文件后退化，需要更好的文件选择策略
3. **针对 issue 类型优化**：backend/hardware 类 bug 最需要完整文件，应优先扩展

### 8.3 评估标准反思

Issue benchmark 的 54-61% 正确率（React Search 闭卷）比 324 QA 的 83% 更能反映真实能力：
- Issue 问题基于真实场景，没有"找不到宽容偏差"
- 但 React Search 需要扩展检索范围才能真正解决这些问题
