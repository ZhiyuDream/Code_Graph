# 注释对 QA 质量的影响分析

## 实验设置

- 数据集：llama_cpp_QA_v2.csv（100 题，基于真实 PR/Issue）
- 采样：15 题（每路由 5 题）
- 对比：同一问题分别用「无注释图」和「有注释图」回答
- 评分：二进制（0/1），由 LLM judge 判定

## 总体结果

| 指标 | 无注释 | 有注释 | 差值 |
|------|--------|--------|------|
| 正确率 | 33.3% | 46.7% | +13.3% |
| 正确题数 | 5/15 | 7/15 | +2 |
| 输入 tokens 均值 | 460 | 989 | +529 (+115%) |
| 延迟均值 (ms) | 5166 | 5838 | +671 (+13%) |

提升 2 题，持平 13 题，下降 0 题。

## 案例 1：注释帮上忙了（0→1）

### 题目 22：mtmd 调试工具设计缺陷

**问题**：为什么 mtmd 中会出现 PR #20508 描述的问题（mtmd: add llama-mtmd-debug binary）？这反映了什么设计缺陷？

**路由**：C（embedding 语义检索）

**无注释时的回答**（❌ 错误）：
> PR #20508 中新增的 `llama-mtmd-debug` 二进制文件，反映出 mtmd 模块存在设计上的调试和诊断能力不足的问题。具体表现在：调试工具缺失、代码复杂度和模块内聚性不足...

**有注释时的回答**（✅ 正确）：
> PR #20508 中新增 `llama-mtmd-debug` 二进制文件，反映出 mtmd 模块在调试和诊断方面设计不足。从代码图及函数调用关系来看，mtmd 的模型加载（`clip_model_loader`）、计算图构建（`clip_image_build_graph`）、计算元数据分配（`alloc_compute_meta`）等核心流程较为复杂...

**参考答案**：Add `llama-mtmd-debug` binary that facilitate debugging mtmd. It allows running the encode and preprocessing pass separately, with a pre-defined input that is reproducible on both cpp and pytorch code.

**Token 成本**：661 → 1258（+597 tokens，+90%）

**为什么注释帮上忙了？**
- 注释提供了 33 个函数的语义摘要，embedding 检索能匹配到 `clip_model_loader`、`clip_image_build_graph` 等具体函数名
- 无注释时只有函数签名，embedding 无法将"调试"语义与具体函数关联
- 有注释后，回答从"泛泛而谈"变成"引用具体函数名和调用关系"，judge 认为信息更完整

### 题目 25：Windows 测试兼容性

**问题**：为什么 tests 中会出现 PR #20483 描述的问题（tests: Fix test-jinja-py Windows failures by bypassing command-line args）？

**路由**：C（embedding 语义检索）

**无注释时的回答**（❌）：
> 测试框架或测试用例在 Windows 平台上对命令行参数的处理不兼容，导致测试失败。

**有注释时的回答**（✅）：
> 部分测试（如 test-jinja-py）在 Windows 平台上通过命令行传递参数时出现了兼容性或解析失败的问题。PR #20483 通过绕过命令行参数传递来修复了这一问题。

**Token 成本**：716 → 1227（+511 tokens，+71%）

**为什么注释帮上忙了？**
- 28 个函数的注释让 embedding 检索到了更相关的测试函数
- 注释中的 summary 描述了函数的具体行为，帮助 LLM 理解"绕过命令行参数"的修复策略
- 无注释时回答过于笼统，有注释后能给出更具体的修复机制描述

### 共同规律

两个提升案例都是 **C 路由（embedding 语义检索）**，注释的 summary 字段直接增强了 embedding 的语义匹配能力。

---

## 案例 2：注释没帮上忙（0→0）

### 题目 3：Issue #71 讨论内容

**问题**：Issue #71（Longer and infinite output）讨论的核心问题是什么？涉及哪些技术概念？

**路由**：B（结构化图查询）

**有注释函数数**：0（没有匹配到任何函数）

**无注释/有注释回答都是**：
> 代码图中未找到与 Issue #71 相关的明确流程起点或调用关系，证据不足...

**参考答案**：If we use `-n 1000000` to have a very long output, it stops generating quite fast, after around 30 lines, probably because of [this line of code]...

**根本原因**：
- Issue #71 是 llama.cpp 早期的 Issue（编号很小），讨论的是生成长度限制问题
- **图中没有 Issue 节点与函数的关联**——当时还没有 PR/Issue 节点
- 即使现在有了 Issue 节点，Issue #71 的 body 中提到的是代码行链接，不是函数名，MENTIONS 边也匹配不到
- **这类问题需要 Issue body 的全文语义理解**，不是图结构能解决的

### 题目 31：CI 优化 PR

**问题**：PR #20521（ci: try to optimize some jobs）做了什么性能优化？

**路由**：C（embedding 语义检索）

**有注释函数数**：4

**无注释/有注释回答都错**：
- 无注释：检索到 `is_running_on_efficiency_core`，答案围绕 CPU 核心检测展开
- 有注释：同样检索到 `is_running_on_efficiency_core`，答案更详细但方向仍然错

**参考答案**：I tried to switch some jobs to arm or ubuntu-slim as per my comment in #20446 for builds where it really doesn't matter.

**根本原因**：
- 这个 PR 改的是 **CI 配置文件**（.yml），不是 C++ 代码
- 图中只有 Function 节点，没有 CI 配置文件的节点
- embedding 检索到的 `is_running_on_efficiency_core` 是语义最近的函数，但完全不相关
- **注释再好也没用，因为问题根本不在函数层面**

### 题目 39：PR #20671 bug 修复

**问题**：PR #20671 修复了什么 bug？涉及哪些函数和文件？

**路由**：A（结构化图查询）

**有注释函数数**：31

**无注释回答**（❌）：检索到 `register_rpc_server_list`、`format_error_response` 等不相关函数

**有注释回答**（❌）：答案变成了"LoRA 适配器缓存更新问题"——**注释反而误导了**

**参考答案**：fix ctx checkpoint invalidation. We must not keep checkpoints that contain tokens with position beyond the position that we are about to generate next.

**根本原因**：
- A 路由用函数名/文件路径匹配，PR #20671 的标题是 "server : fix ctx checkpoint invalidation"
- 图中没有 PR 节点与函数的 TOUCHES 边（这个 PR 没有 changed_files 数据）
- 注释中某些函数的 summary 提到了 "cache" 相关概念，LLM 错误地将其与 LoRA cache 关联
- **这是注释"误导"的典型案例**：注释提供了更多信息，但 LLM 选错了关联方向

---

## 案例 3：注释前后都对（1→1）

### 题目 19：server PR #20671 问题

**路由**：C | **tokens**：380 → 892（+135%）

无注释时就答对了，有注释后答案更详细但结论一致。说明对于 embedding 检索已经能匹配到正确函数的情况，注释只是锦上添花，不改变正确性。

### 题目 89：repack.cpp 架构

**路由**：B | **tokens**：557 → 1497（+169%）

无注释时通过图结构查询就能回答架构问题，注释增加了 940 tokens 但没有改变结果。

**启示**：对于已经能答对的题，注释带来的是纯成本增加（+135%~169% tokens），没有额外收益。

---

## 总结：注释什么时候有用，什么时候没用

### 有用的场景
| 条件 | 原因 |
|------|------|
| C 路由（embedding 检索） | 注释 summary 直接增强语义匹配 |
| 问题涉及函数行为/设计意图 | 注释描述了函数"做什么"和"为什么" |
| 图中有相关函数但签名不够表达语义 | 注释补充了签名缺失的语义信息 |

### 没用的场景
| 条件 | 原因 |
|------|------|
| 问题涉及 CI/配置/非代码文件 | 图中没有这类节点，注释无从附着 |
| Issue body 中没有函数名 | MENTIONS 边匹配不到，注释无法被检索 |
| A/B 路由（结构化查询） | 这些路由不依赖语义，注释信息被忽略 |
| 注释与问题语义方向不一致 | 可能误导 LLM 选错关联方向 |

### 成本-收益
- 注释平均增加 +115% input tokens，+13% 延迟
- 只在 C 路由有效（5 题中 2 题提升，+40%）
- A/B 路由零收益但仍付出 token 成本
- **建议**：按路由类型决定是否注入注释，A/B 路由可以跳过注释以节省成本
