# Code_Graph 实验改进历程

> 本文档记录从 DeepSeek 53.1% 到 77.2% 的完整实验改进过程。

## 四版本演进

| 版本 | 核心配置 | 正确率 | 相比上一版 | 关键洞察 |
|------|---------|--------|-----------|---------|
| V1 | 1500 tokens, 无 ReAct | **53.1%** | — | 基线 |
| V2 | 8192 tokens, 无 ReAct | **67.3%** | **+14.2pp** | max_token 限制压抑了 14pp，不是简单截断而是"压缩扭曲" |
| V3 | 8192 + ReAct(修复) + file_exp 50 | **75.6%** | **+8.3pp** | ReAct 决策因 max_tokens 太小一直返回 null，修复后真有效果 |
| V4 | 8192 + ReAct + top_k=20 | **77.2%** | **+1.6pp** | **召回充分是根基**，top_k=5→20 让 file expansion 彻底不需要 |

与 GPT-5.4 差距：87.0% → 77.2% = **9.8pp**

## 关键发现

### 1. max_tokens 不是截断问题，是压缩扭曲问题

- 1500 tokens 版平均 1524 字，8192 版平均 2429 字
- 截断题目正确率反而更高（58.0% vs 49.2%）
- 8 题对照实验：1500=87.5%，4000=75.0%
- **真正的问题**：token 压力下模型被迫快速下结论，改变分析方向

### 2. ReAct 决策曾完全失效

`call_llm_json` 默认 `max_tokens=500`，DeepSeek reasoning 占 400+ tokens，JSON 没空间输出 → 代码 fallback 到 reasoning_content（非 JSON）→ `json.loads` 失败 → 返回 `null` → 系统默认 `sufficient`

**修复方案**：
- `response_format={"type": "json_object"}` 强制 JSON
- `json_repair` 4 层兜底修复
- DeepSeek 自动增大 `max_tokens` 到 1200
- JSON 模式下不 fallback 到 reasoning_content

### 3. file expansion 是兜底而非核心

| 配置 | 正确率 | 说明 |
|------|--------|------|
| 默认 file_exp 50 | 75.6% | 暴力扩展，噪音与信号并存 |
| file_exp 后移为 ReAct action | 71.9% | 初始召回不足时彻底断粮 |
| top_k=20（召回充足） | **77.2%** | file_exp 完全不需要触发 |

**结论**：问题的根源不是"file expansion 好不好"，而是**初始检索召回率太低**。

### 4. Prompt 目录化

创建 `prompts/` 目录管理所有 prompt，提升可维护性：
- `react_decide.txt` — ReAct 决策 prompt
- `answer_generation.txt` — 答案生成 prompt
- `judge_binary.txt` — 二分类评判 prompt
- `react_actions.json` — ReAct action 定义

## 基础设施改进

- ✅ Prompt 目录化
- ✅ ReAct 修复（response_format + json_repair）
- ✅ 轨迹记录（每题完整检索路径）
- ✅ Action 扩展（caller/callee/same_file/same_class）
- ✅ 选择性 file expansion（仅当召回不足时触发）

## 下一步方向

1. **初始检索质量**：embedding 对代码理解不够好，开放问题召回率低
2. **Issue 反向解析**：Issue 里的函数名/文件名没有被利用
3. **sufficient 结构性校验**：模型经常在信息不足时自信地停止
4. **答案生成 prompt 工程**：如何让 DeepSeek 更聚焦、更少推断

> 如果只能改一件事：优先改进**答案生成的 prompt**——即使检索到了 24 个函数，DeepSeek 仍然会把注意力放在错误的函数上。
