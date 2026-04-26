# 实验: V8 无 Callers/Callees 扩展

## 日期
2026-04-22

## 改进内容
**从 Agent 的可选动作中完全删除 expand_callers/expand_callees**，而非硬编码跳过。

与 V7 Minimal 的关键区别：
| | V7 Minimal | 本实验 |
|---|---|---|
| 移除方式 | 硬编码 `skipped_expansion`（Agent选择扩展但执行空操作） | 从Agent动作列表中完全删除 |
| ReAct步骤 | 走到最大步数(4步)，但步骤空转 | 仅2步：initial_search + sufficient停止 |
| 空转步骤 | 697次 `skipped_expansion` | 0次 |

## 假设
1. V7 Minimal 的 65.6% 准确率主要受硬编码 skip 导致的步骤空转影响
2. 正确移除 callers/callees 后，准确率下降应显著小于 8.5%

## 实验配置
- 数据集: 360题 (results/qav2_test.csv)
- 并行: 20 workers
- 文件扩展: 启用 (FILE_EXPANSION_MAX=50)
- 评估: 单线程稳定评估（避免多线程丢失）

## 结果

### 准确率对比
| 配置 | 准确率 | vs Baseline |
|---|---|---|
| V8 Baseline (有 callers/callees) | **74.4%** | - |
| V7 Minimal (硬编码skip) | **65.6%** | -8.8% |
| **V8 No CC + FileExp (本实验)** | **67.2%** | **-7.2%** |

### 题目级对比
| 类别 | 数量 | 说明 |
|---|---|---|
| 两者都对 | 213 | 基础能力重叠 |
| 两者都错 | 63 | 共同缺陷 |
| Baseline独有正确 | **55** | callers/callees 的价值 |
| NoCC独有正确 | **29** | file expansion 的价值 |

### 完整指标对比

| 指标 | V8 Baseline (有CC) | V8 No CC + FileExp | 变化 |
|---|---|---|---|
| **准确率** | **74.4%** (268/360) | **67.2%** (242/360) | **-7.2%** |
| 平均延迟 | 64.1s | 70.5s | +10.0% |
| 延迟中位数 | 56.2s | 64.3s | +14.4% |
| 延迟P90 | 78.6s | 87.3s | +11.1% |
| 延迟范围 | 24.4s - 232.3s | 18.1s - 217.2s | — |
| 平均召回函数 | 6.9 | 48.2 | **+597%** |
| 召回范围 | 5 - 15 | 5 - 50 | — |
| **平均步骤** | **3.0** | **2.0** | **-33%** |
| 步骤分布 | 1步: 2题, 3步: 358题 | 2步: 360题 | — |
| 平均文件扩展 | 0 | 42.7 | — |

> **说明**: 当前框架未记录 token 消耗，故不估算。

## Good Cases (NoCC做对、Baseline做错)

**[2] llama-grammar 的内部结构**
- Baseline召回: 5函数 → 信息不足，无法回答
- NoCC召回: 50函数 → 同文件函数提供了完整的模块结构

**[62] quants.c 中标识符的定义和引用位置**
- Baseline召回: 7函数 → 遗漏了关键定义位置
- NoCC召回: 50函数 → 同文件扩展捕获了所有引用

## Bad Cases (Baseline做对、NoCC做错)

**[1] apertus 子模块层级关系**
- Baseline召回: 11函数（含callers/callees链）→ 正确识别了层级
- NoCC召回: 50函数 → 大量噪声淹没了关键结构信息

**[43] ggml.c 代码元素依赖关系**
- Baseline: 通过调用链识别依赖关系
- NoCC: file expansion召回了无关函数，LLM无法聚焦

**共性**: 涉及"依赖关系"、"控制流"、"设计决策"的问题，callers/callees提供不可替代的调用链信息

## 关键发现

### 1. 硬编码skip确实是V7 Minimal失败的主因
- V7 Minimal 准确率: 65.6%
- 正确移除后准确率: 67.2% (+1.6%)
- 差距从 8.8% 缩小到 7.2%

### 2. callers/callees 仍有显著价值
- 55题baseline能做对而noCC做不对
- 主要价值领域: 依赖关系、控制流、设计决策类问题
- 单次调用效率虽低(0.47 funcs/call)，但对特定问题是不可替代的

### 3. file expansion 的双刃剑效应
- 召回提升 597% (6.9→48.2)
- 帮助29题（结构、实现类问题）
- 但也引入噪声，导致55题做错（依赖关系类问题）

## 结论

**不采纳完全移除 callers/callees 的策略**

正确方向:
1. **条件触发**: 只在问题涉及"依赖"、"调用链"、"控制流"时启用callers/callees
2. **噪声控制**: file expansion的50函数上限可能过高，需要重排序过滤
3. **混合策略**: file expansion提供广度，callers/callees提供深度，按需组合
