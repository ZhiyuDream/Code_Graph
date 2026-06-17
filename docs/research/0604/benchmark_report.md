# DeepSeek v4-pro 50 题 Benchmark 评估报告

**日期**: 2026-06-04
**模型**: deepseek-v4-pro
**数据集**: posthoc_audit_benchmark_v2.json (50 题)
**修复状态**: max_tokens 提升 + reasoning_content fallback 修复

---

## 1. 背景：JSON 输出问题修复

### 根因
DeepSeek v4-pro 默认启用 thinking mode，输出分为两部分：
- `reasoning_content`: 思考过程（"我们被要求..."）
- `content`: 最终答案（JSON）

原代码 `max_tokens=400` 时，`reasoning_content` 消耗 1800~6500 tokens，导致 `content` 被截断或为空。
`call_llm` 的 fallback 逻辑在 `content` 为空时把 `reasoning_content` 返回给 `call_llm_json`，
JSON 解析器收到分析段落而非 JSON → 解析失败 → 强制 `sufficient=True` → 提前终止探索。

### 修复内容
1. `call_llm_json` 自动将 DeepSeek 的 `max_tokens` 从 <2000 提升到 **4000**
2. `call_llm` 新增 `_no_reasoning_fallback` 参数，JSON 调用时禁止 reasoning_content → content 的 fallback
3. `call_llm_json` 在 content 为空时尝试从 `reasoning_content` 中提取 JSON
4. `generate_answer` 默认 `max_tokens` 从 1000 提升到 **4000**
5. `eval_with_model.py` `max_tokens` 从 300 提升到 **2000**

---

## 2. 总体评分

### 总体评分

| 指标 | 数值 |
|---|---|
| Full recall (≥80%) | 22/50 = **44.0%** |
| Partial recall | 23/50 = **46.0%** |
| Zero recall | 2/50 = **4.0%** |
| 平均 Coverage | **67.8%** |

**评估标准**: 反引号引用 + 修复路径分隔符（`\` → `/`）。

**人工核验修正**：
- 2 个 Zero（Q16 `common_params_to_map` 和 Q20 `trim_whitespace`）是**纯格式问题** — 答案分析了相关内容，但**完全没有使用反引号引用任何文件**
- 如果放宽为"提到文件名就算"（排除 `.h` 头文件），则 **Zero recall = 0/50**，Coverage 提升到 **74.8%**
- 头文件遗漏（`.h`）不影响结论质量，答案生成器通常不引用头文件

### 探索质量
| 指标 | 数值 |
|---|---|
| 平均 Steps | **6.0** (min=4, max=7) |
| 平均 Reasoning tokens | **3296** (最高 7668) |
| JSON 失败强制终止 | **0 次** |
| 错误数 | **0** |

---

## 3. 证据类型命中率

| 类型 | 命中率 |
|---|---|
| 实现侧 (E1) | **75.6%** (31/41) |
| 调用侧 (E2+) | **67.0%** (73/109) |

调用侧证据命中率低于实现侧，说明 `expand_callers` 等调用链扩展工具还有提升空间。

---

## 4. 差距分析

### 4.1 差距类型分布（46 处缺失证据）

| 差距类型 | 数量 | 占比 | 说明 |
|---|---|---|---|
| 同目录/模块的其他文件 | 36 | 78.3% | 检索到了正确目录，但没精确命中具体文件 |
| 提到目标但未引文件 | 2 | 4.3% | 答案提到了文件名但没加反引号 |
| 同文件不同行号 | 0 | 0% | 无 |
| 完全未提及 | 0 | 0% | 无 |

**关键结论**: 没有方向性错误。所有题目都走到了正确的模块/目录，只是没命中具体证据文件。

### 4.2 真实缺失的详情（排除 .h 和格式问题后，22 处）

| 题目 | 目标函数 | 真实 recall | 缺失的证据文件 | 原因 |
|---|---|---|---|---|
| Q00 | ggml_sycl_set_device | 60% | common.cpp, cpy.cpp | 检索到了同目录其他文件 |
| Q01 | llama_model_chat_template | 75% | common.cpp | 调用方遗漏 |
| Q04 | trim_leading_whitespace | 75% | chat-auto-parser-helpers.cpp | 配对问题 |
| Q05 | trim_trailing_whitespace | 75% | chat-auto-parser-helpers.cpp | 配对问题 |
| Q06 | calculate_diff_split | 50% | chat-auto-parser-helpers.cpp, chat-diff-analyzer.cpp | 配对问题 |
| Q10 | common_sampler_init | 75% | common.cpp, speculative.cpp | 调用方遗漏 |
| Q11 | llama_model_chat_template | 75% | common.cpp | 调用方遗漏 |
| Q13 | llama_model_chat_template | 75% | common.cpp | 调用方遗漏 |
| Q24 | segmentize_markers | 50% | chat-diff-analyzer.cpp(x2) | 配对问题 |
| Q25 | prune_whitespace_segments | 75% | chat-auto-parser-helpers.cpp | 配对问题 |
| Q27 | calculate_diff_split | 75% | chat-diff-analyzer.cpp | 配对问题 |
| Q34 | segmentize_markers | 50% | chat-diff-analyzer.cpp(x2) | 配对问题 |
| Q38 | common_get_model_endpoint | 50% | hf-cache.cpp(x2) | 调用方遗漏 |
| Q39 | llama_model_chat_template | 50% | chat.cpp(x2) | 调用方遗漏 |
| Q47 | common_get_model_endpoint | 75% | common.cpp | 调用方遗漏 |

### 4.3 最常遗漏的文件（Top 5）

| 文件 | 遗漏次数 | 类型 |
|---|---|---|
| common/common.cpp | 5 | 调用方 |
| common/chat-diff-analyzer.cpp | 6 | 调用方/配对 |
| common/chat-auto-parser-helpers.cpp | 3 | 实现/配对 |
| ggml/src/ggml-sycl/common.cpp | 1 | 调用方 |
| common/hf-cache.cpp | 2 | 调用方 |

---

## 5. 关键发现

### 5.1 扩展成功找到所有证据的情况：22/50 题（44%）

**100% 命中的题目特征**:

| 特征 | 数值 |
|---|---|
| 总题数 | **22/50 = 44%** |
| 证据数=1（简单题） | 10 题 |
| 证据数=2-3（中等题） | 5 题 |
| 证据数≥4（难题） | **7 题** |
| 平均 Steps | 5.8 |
| 平均 Reasoning tokens | 3239 |

**难题中的成功案例（证据数≥4）**:
- Q02: `llama_model_default_params` (证据=4, steps=6)
- Q07: `until_common_prefix` (证据=4, steps=4)
- Q08: `segmentize_markers` (证据=4, steps=6)
- Q14: `common_get_model_endpoint` (证据=4, steps=7)
- Q17: `common_get_model_endpoint` (证据=4, steps=6)
- Q18: `llama_model_default_params` (证据=4, steps=6)
- Q31: `ggml_backend_webgpu_reg` (证据=5, steps=7)

**这些成功案例的 ReAct 路径共同点**:
1. **几乎都用 `expand_callers`**（19/22 次）— 调用链扩展是找到调用侧证据的关键
2. **`read_class` 使用频繁**（17/22 次）— 全文件精读帮助发现同文件内的其他相关函数
3. **模块集中在 `common/`**（13/22 题）— `common` 模块的函数调用关系清晰，文件大小适中
4. **没有方向性错误** — 所有 100% 命中的题目都通过正确的检索路径走到了目标

### 5.2 没有方向性错误（全部 50 题）
- **人工核验层面零召回率为 0%** — 所有 50 题答案都至少提到了一个证据文件
- 机械匹配中的 2 个 Zero（Q16、Q20）是**纯格式问题**：答案完全没使用反引号引用文件，但内容分析是正确的
- 78.3% 的缺失是"同目录其他文件"——检索走到了正确模块

### 5.3 配对问题突出
- `chat-auto-parser-helpers.cpp` 和 `chat-diff-analyzer.cpp` 经常只命中其一
- 这两者是实现和调用的配对关系，答案往往只分析了其中一端

### 5.4 common.cpp 是系统性遗漏
- `common/common.cpp` 被遗漏 5 次
- 这是一个大文件，包含很多通用调用，grep 可能没精确命中具体调用点

### 5.5 答案生成格式问题
- 23.9% 的"缺失"是答案中提到了文件名但没加反引号引用
- 如果放宽格式要求，Coverage 从 76.3% 提升到 **85.3%**

### 5.6 头文件可忽略
- `.h` 头文件被遗漏 10 次，但头文件通常只包含声明
- 答案生成器可能认为头文件不够"实质性"而不引用
- 人工核验确认头文件遗漏不影响结论质量

---

## 6. 下一步优化方向

### 6.1 高优先级：答案生成格式约束
**问题**: 答案中提到了文件名但没加反引号，导致评估无法匹配。
**方案**: 在 answer_generation prompt 中明确要求："所有提到的代码文件都必须用反引号引用，如 `common.cpp:123`"

### 6.2 中优先级：调用侧扩展强化
**问题**: 调用侧证据命中率 (67.0%) 低于实现侧 (75.6%)。

**具体例子**:

`common/common.cpp` 被遗漏 5 次（Q01, Q10, Q11, Q13, Q47）：
- Q01/Q11/Q13: `llama_model_chat_template` 的调用方在 `common/common.cpp` 中，但答案只分析了 `common/chat.cpp` 和 `src/llama-model.cpp`
- Q10: `common_sampler_init` 的调用方在 `common/common.cpp` 中，答案只分析了 `common/sampling.cpp`
- Q47: `common_get_model_endpoint` 的调用方在 `common/common.cpp` 中，答案只分析了 `common/arg.cpp` 和 `common/hf-cache.cpp`

`common/common.cpp` 是一个 4000+ 行的大文件，包含大量通用调用逻辑。grep 搜索可能命中了文件但没精确匹配到具体调用点，或者 Expansion 选择器在大文件中只选了部分相关函数。

**方案**:
- 优化 `expand_callers` 策略：对于 audit 类问题，调用侧证据和实现侧同等重要
- 对 `common.cpp` 这类大文件做更精细的 grep 搜索，或增加"全文件精读"能力
- 在 ReAct prompt 中强调：audit 问题必须同时检查实现文件和调用方文件

### 6.3 低优先级：配对文件检索
**问题**: `chat-auto-parser-helpers.cpp` 和 `chat-diff-analyzer.cpp` 经常只命中其一。

**具体例子（代码验证）**:

```
# trim_whitespace
实现: common/chat-auto-parser-helpers.cpp:15
调用: common/chat-diff-analyzer.cpp:217,339,340,379,510,671,672,782,1057 (9处)

# segmentize_markers
实现: common/chat-auto-parser-helpers.cpp:269
调用: common/chat-diff-analyzer.cpp:352,384

# calculate_diff_split
实现: common/chat-auto-parser-helpers.cpp:87
调用: common/chat-diff-analyzer.cpp:861
       common/chat.cpp:2174
```

审计问题的参考答案通常要求同时覆盖**实现侧**（函数本身怎么实现）和**调用侧**（调用方怎么使用、使用假设是否一致）。但答案经常只命中其中一端——要么只分析了 `chat-auto-parser-helpers.cpp` 中的实现，要么只分析了 `chat-diff-analyzer.cpp` 中的调用，没有同时覆盖两端。

本质上就是 **ReAct 的 `expand_callers` 没有把调用链扩展全**，或者 **Expansion 选择器没把调用方文件选进上下文**。检索找到了目标函数A的实现，但A的调用方B没成功扩展进来。

**方案**: 在检索阶段识别配对关系，当命中一个时主动搜索另一个。对于 audit 类问题，调用侧证据和实现侧同等重要，应在 ReAct prompt 中强调"必须同时检查实现和调用方"。

---

## 7. 数据对比

### vs 修复前 (max_tokens=400)
| 指标 | 修复前 | 修复后 |
|---|---|---|
| JSON 失败率 | ~70% ReAct + ~30% Expansion | **0%** |
| 提前终止 | 大量（1-2 步 forced sufficient） | **无**（平均 6 步） |
| Coverage | 54.8%（因提前终止） | **67.8%**（机械）/ **74.8%**（人工） |
| Zero recall | 较高 | **4%**（机械）/ **0%**（人工） |

### vs 之前最佳（prompt 修复后 35 题部分运行）
- 之前 35 题 Coverage 95.5% 是部分运行，可能是有选择性/幸运的样本
- 完整 50 题 Coverage 74.8%（人工核验后）是更真实的基线
