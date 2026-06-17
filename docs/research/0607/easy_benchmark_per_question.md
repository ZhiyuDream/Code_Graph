# Easy Benchmark 逐题分析（Symbol Fast Path，排除 .h/.hpp）

> 结果文件：`benchmark_symbol_fastpath_20260607_131010.json`
> 
> 分析口径：只统计 `.cpp` / `.c` 文件，`.h` / `.hpp` 不参与。

---

## 逐题详情

| 题目 | Evidence(.cpp) | 检索覆盖 | 引用覆盖 | 缺失文件 | 根因 |
|------|---------------|----------|----------|----------|------|
| posthoc_audit_001 | 3 | 100% | 100% | - | 全引用 |
| posthoc_audit_002 | 3 | 100% | 100% | - | 全引用 |
| posthoc_audit_003 | 4 | 100% | 100% | - | 全引用 |
| posthoc_audit_004 | 2 | 100% | 100% | - | 全引用 |
| posthoc_audit_005 | 2 | 100% | 100% | - | 全引用 |
| posthoc_audit_006 | 2 | 100% | 100% | - | 全引用 |
| posthoc_audit_007 | 3 | 100% | 100% | - | 全引用 |
| posthoc_audit_008 | 2 | 100% | 100% | - | 全引用 |
| posthoc_audit_009 | 2 | 100% | 100% | - | 全引用 |
| posthoc_audit_010 | 2 | 100% | 100% | - | 全引用 |
| posthoc_audit_011 | 3 | 100% | 67% | `common/speculative.cpp` | **搜到未引** |
| posthoc_audit_012 | 3 | 100% | 67% | `common/common.cpp` | **搜到未引** |
| posthoc_audit_013 | 1 | 100% | 100% | - | 全引用 |
| posthoc_audit_014 | 3 | 100% | 67% | `common/common.cpp` | **搜到未引** |
| posthoc_audit_015 | 3 | 100% | 100% | - | 全引用 |
| posthoc_audit_016 | 3 | 100% | 67% | `common/preset.cpp` | **搜到未引** |
| posthoc_audit_017 | 2 | 100% | 100% | - | 全引用 |
| posthoc_audit_018 | 3 | 100% | 100% | - | 全引用 |
| posthoc_audit_019 | 4 | 100% | 100% | - | 全引用 |
| posthoc_audit_020 | 3 | 67% | 67% | `common/arg.cpp` | **检索失败** |
| posthoc_audit_021 | 2 | 100% | 100% | - | 全引用 |
| posthoc_audit_022 | 2 | 100% | 100% | - | 全引用 |
| posthoc_audit_023 | 2 | 100% | 100% | - | 全引用 |
| posthoc_audit_024 | 2 | 100% | 100% | - | 全引用 |
| posthoc_audit_025 | 2 | 100% | 100% | - | 全引用 |
| posthoc_audit_026 | 2 | 100% | 100% | - | 全引用 |
| posthoc_audit_027 | 2 | 100% | 100% | - | 全引用 |
| posthoc_audit_028 | 3 | 100% | 100% | - | 全引用 |
| posthoc_audit_029 | 2 | 100% | 100% | - | 全引用 |
| posthoc_audit_030 | 2 | 100% | 100% | - | 全引用 |
| posthoc_audit_031 | 1 | 100% | 100% | - | 全引用 |
| posthoc_audit_032 | 1 | 100% | 100% | - | 全引用 |
| posthoc_audit_033 | 1 | 100% | 100% | - | 全引用 |
| posthoc_audit_034 | 4 | 100% | 100% | - | 全引用 |
| posthoc_audit_035 | 2 | 100% | 100% | - | 全引用 |
| posthoc_audit_036 | 1 | 100% | 100% | - | 全引用 |
| posthoc_audit_037 | 2 | 100% | 50% | `common/arg.cpp` | **搜到未引** |
| posthoc_audit_038 | 1 | 100% | 100% | - | 全引用 |
| posthoc_audit_039 | 3 | 100% | 100% | - | 全引用 |
| posthoc_audit_040 | 3 | 100% | 100% | - | 全引用 |
| posthoc_audit_041 | 2 | 100% | 100% | - | 全引用 |
| posthoc_audit_042 | 2 | 100% | 100% | - | 全引用 |
| posthoc_audit_043 | 2 | 100% | 100% | - | 全引用 |
| posthoc_audit_044 | 1 | 100% | 100% | - | 全引用 |
| posthoc_audit_045 | 2 | 50% | 100% | - | 全引用（evidence 中 1 个 .h 被排除，实际只要求 1 个 .cpp） |
| posthoc_audit_046 | 1 | 100% | 100% | - | 全引用 |
| posthoc_audit_047 | 2 | 100% | 100% | - | 全引用 |
| posthoc_audit_048 | 3 | 100% | 100% | - | 全引用 |
| posthoc_audit_049 | 1 | 100% | 100% | - | 全引用 |
| posthoc_audit_050 | 1 | 100% | 100% | - | 全引用 |

---

## 汇总

| 指标 | 数值 |
|------|------|
| 总题数 | 50 |
| 检索全召回 | **48/50** |
| 引用全 | **44/50** |
| 引用部分 | **6/50** |
| 引用零 | **0/50** |

---

## 引用部分题目的根因分类

### 检索失败导致（1 题）

| 题目 | 缺失文件 | 说明 |
|------|----------|------|
| posthoc_audit_020 | `common/arg.cpp` | 检索阶段未召回 `arg.cpp`，答案只分析了 `common.cpp` 和 `sampling.cpp` |

### 搜到了但没引用（5 题）

| 题目 | 缺失文件 | 说明 |
|------|----------|------|
| posthoc_audit_011 | `common/speculative.cpp` | 检索到了但未在答案中分析 |
| posthoc_audit_012 | `common/common.cpp` | 检索到了但未在答案中分析 |
| posthoc_audit_014 | `common/common.cpp` | 检索到了但未在答案中分析 |
| posthoc_audit_016 | `common/preset.cpp` | 检索到了但未在答案中分析 |
| posthoc_audit_037 | `common/arg.cpp` | 检索到了但未在答案中分析 |

---

## 关键洞察

1. **检索召回极强**：48/50 题全召回（96%），只有 `audit_020`（`arg.cpp` 没搜到）和 `audit_045`（1 个 .cpp 没搜到）检索不全。

2. **引用问题集中在两类文件**：
   - `common/common.cpp` — 2 题搜到未引（audit_012, audit_014）
   - `common/arg.cpp` — 1 题检索失败（audit_020）+ 1 题搜到未引（audit_037）
   - `common/preset.cpp` — 1 题搜到未引（audit_016）
   - `common/speculative.cpp` — 1 题搜到未引（audit_011）

3. **"搜到未引"是主要问题**（5/6），而非"检索失败"（1/6）。说明检索阶段已经做得很好，答案生成阶段需要增强对 `common.cpp`、`arg.cpp`、`preset.cpp`、`speculative.cpp` 等文件的引用约束。

4. **audit_045 特殊**：原始 evidence 有 2 个文件（1 个 .cpp + 1 个 .h），排除 .h 后只剩 1 个 .cpp 需要引用。检索只召回了这个 .cpp，所以引用覆盖率 100%。如果把 .h 算进来，检索覆盖率是 50%（1/2）。

---

*分析时间：2025-06-07*
*结果文件：benchmark_symbol_fastpath_20260607_131010.json*
*口径：排除 .h / .hpp 头文件*
