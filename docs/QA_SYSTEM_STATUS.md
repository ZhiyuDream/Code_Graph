# QA 系统现状与根因分析报告

> 最后更新：2026-06-03  
> 适用模型：DeepSeek v4-pro（主要验证对象）

---

## 1. 系统架构

### 1.1 Pipeline 四阶段

```
问题输入
    │
    ▼
┌─────────────────┐     ┌──────────────┐     ┌──────────┐     ┌──────────┐
│ 1. Initial      │────▶│ 2. ReAct     │────▶│ 3. Exp.  │────▶│ 4. Gen.  │
│    Search       │     │    Loop      │     │    ansion│     │    erate │
│ (并行召回)       │     │ (LLM 决策)   │     │ (展开代码) │     │ (生成答案) │
└─────────────────┘     └──────────────┘     └──────────┘     └──────────┘
```

| 阶段 | 职责 | 关键配置 |
|------|------|----------|
| **Initial Search** | 并行调用所有 enabled retrievers，合并去重 | top_k=10/retriever |
| **ReAct Loop** | LLM 逐轮决策 action → 执行 → 循环 | max_steps=5 |
| **Expansion** | LLM 判断哪些函数值得展开 → 读取完整实现 | top 10 展开 |
| **Generate** | 构建完整上下文 → 调用 LLM 生成答案 | max_tokens=8000 |

### 1.2 Retriever 矩阵

| Retriever | 召回内容 | 优点 | 缺点 |
|-----------|---------|------|------|
| **Grep** | 关键词匹配的单行/短片段 | 精确、快速 | 无 end_line，无法展开完整函数；召回大量噪音 |
| **Embedding** | 语义相似的函数摘要 | 语义覆盖 | 索引中 body 只有摘要文本（~120 字符），非完整代码 |
| **Graph** | Neo4j 中存储的函数元数据 | 有调用关系 | 边关系不全，callees 召回率低 |

### 1.3 ReAct 决策循环

**7 个可用 action：**

```python
_ACTIONS = {
    "grep_search":     "用新的关键词进行 grep 代码搜索",
    "semantic_search": "用新的查询进行 embedding 语义搜索",
    "expand_callers":  "扩展目标函数的调用者（上游）",
    "expand_callees":  "扩展目标函数的被调用者（下游）",
    "read_class":      "读取目标函数所在类/文件的完整实现",
    # "read_full_file": "已禁用——文件太大，挤占上下文预算",
    "sufficient":      "信息已足够，可以生成答案",
}
```

**决策 prompt 中每个函数只显示：**
- 函数名 + 文件路径 + score + source
- 签名（180 字符以内）
- **没有 body preview**

这导致 DeepSeek 只能基于函数名"瞎猜"哪些值得扩展。

---

## 2. 渐进式代码展开

### 2.1 展开级别

```
SIGNATURE ──expand()──▶ BODY ──expand()──▶ CLASS ──expand()──▶ FULL_FILE
(仅签名)                (完整函数实现)      (完整类)            (完整文件)
```

### 2.2 关键修复：BODY 预标记 Bug（2026-06-03 修复）

**Bug 描述：**
- `from_retrieval_result()` 中，embedding retriever 返回的函数被**预标记为 BODY**
- 但 body 实际只有 embedding 索引中的摘要文本（~120 字符）
- `build_body_context()` 调用 `expand()` 时，因 `expand_level >= BODY` 直接跳过
- **结果：BODY 级别的函数永远不会加载完整代码**

**修复前 vs 修复后（index=33）：**

| 指标 | 修复前 | 修复后 |
|------|--------|--------|
| generate prompt tokens | 5,558 | **23,829** (↑4.3x) |
| context 实际字符数 | 9,136 | **56,989** |
| `load_all_data` body 长度 | 0（被跳过） | **12,253**（完整实现） |

---

## 3. 预算配置

| 参数 | 值 | 说明 |
|------|-----|------|
| `build_body_context` budget | 60,000 chars | 函数实现上下文预算 |
| `build_full_context` budget | 100,000 chars | 总上下文预算（含 issue） |
| `CodeExpander.body_budget` | 40,000 | 展开时单函数预算 |
| generate `max_tokens` | 8,000 | DeepSeek reasoning 需要 |

**注意：** 即使 budget 设到 100k，实际 context 仍受限于函数数量。36 个函数 × 平均 300 字符 = ~10k 已占满大部分预算。

---

## 4. DeepSeek v4-pro 特性

| 特性 | 值 | 影响 |
|------|-----|------|
| **Reasoning tokens** | 占 completion 的 70-90% | 需要 8k max_tokens，否则 content 被截断 |
| **JSON mode 稳定性** | 不稳定 | ReAct step 2+ 经常返回自然语言而非 JSON，导致 fallback `sufficient` |
| **Temperature** | 未设置（API 默认） | 同一 prompt 多次运行结论可能不同 |
| **上下文窗口** | ~64k | 23.8k prompt + 8k completion = 31.8k，有余量 |

---

## 5. 实验结果对比

### 5.1 三种输入方式对比（index=33）

| 输入方式 | 函数数 | Prompt 大小 | DeepSeek 结论 | 分析质量 |
|----------|--------|-------------|--------------|----------|
| **QA Pipeline（自动召回）** | 36 | 23.8k tokens | "不闭合" | ❌ 误判 lambda 泄漏 |
| **参考答案精简证据（4行）** | 0 | ~200 tokens | "证据不足，无法判断" | ⚠️ 诚实但过于保守 |
| **参考答案完整实现（E1-E4）** | 4 | 3.8k tokens | "不闭合" | ✅ **发现真正问题** |

### 5.2 核心发现：信噪比 > Context 大小

**反直觉：context 不是越大越好。**

QA Pipeline 召回了 36 个函数，其中 **72% 是噪音**（测试文件、单行调用点、无关后端实现）。DeepSeek 被淹没在噪音中，分析质量反而低于只给 4 个精选函数的情况。

**真正相关的只有 4-5 个函数：**
- `ggml_backend_free`（核心释放函数）
- `llama_model_loader::load_all_data`（资源管理主逻辑）
- `~ggml_backend_meta_context`（析构路径）
- RPC 服务端清理函数

---

## 6. 参考答案 vs DeepSeek 分析

### 6.1 参考答案的局限

参考答案（index=33）结论为"闭合"，但：
- 只列出 4 个证据点（每行一个 `free` 调用）
- **未包含资源申请路径**
- **未检查所有失败返回路径**

### 6.2 DeepSeek 发现参考答案遗漏的问题

当给予 E1-E4 的**完整函数实现**时，DeepSeek 发现了两个参考答案完全没提到的问题：

1. **`load_all_data` 中 progress_callback 取消路径泄漏**
   ```cpp
   if (!progress_callback(...)) {
       return false;  // ← 跳过所有清理代码
   }
   ```

2. **RPC 服务端 accept 失败路径泄漏**
   ```cpp
   if (client_socket == nullptr) {
       return;  // ← 不释放 backends
   }
   ```

**结论：DeepSeek 作为审计工具，比参考答案更严格、更细致。** 当前评估逻辑（"结论必须等于参考答案"）本质上是在惩罚 DeepSeek 的严谨性。

---

## 7. 已知问题汇总

### 7.1 已修复 ✅

| 问题 | 修复 | 效果 |
|------|------|------|
| BODY 预标记 bug | 删除 `from_retrieval_result` 中的 BODY 预标记 | Context 从 9k → 57k chars |
| Budget 太小 | 20k → 100k chars | 完整代码可加载 |
| `read_full_file` 挤占预算 | 禁用 action | 避免单文件吃光预算 |
| Prompt 硬编码 | 全部外置到 `prompts/*.txt` | 可热更新 |

### 7.2 待修复 ❌

| 问题 | 根因 | 优先级 |
|------|------|--------|
| **ReAct prompt 缺少 body preview** | `react_decide.txt` 只显示签名 | 🔴 高 |
| **DeepSeek JSON mode 不稳定** | API 偶尔返回自然语言 | 🔴 高 |
| **召回噪音过高（72%）** | Grep 召回大量无关单行匹配 | 🟡 中 |
| **函数排序不稳定** | 多 retriever 结果合并后顺序变化 | 🟡 中 |
| **评估标准偏差** | 参考答案可能遗漏真正问题 | 🟡 中 |
| **Embedding 索引不含完整代码** | `build_index` 时截断到 3k 字符 | 🟢 低 |

---

## 8. 运行方式

### 8.1 单题调试

```bash
# 用 DeepSeek 跑单题
python scripts/eval/debug_run_single.py --index 33 --model deepseek-v4-pro

# 指定题目关键词
python scripts/eval/debug_run_single.py --keyword "ggml_backend_free" --model deepseek-v4-pro
```

### 8.2 批量跑 50 题（posthoc_audit_qa.json）

```bash
# 全量跑并保存结果
python experiments/run_v2_benchmark.py \
    --dataset datasets/posthoc_audit_qa.json \
    --model deepseek-v4-pro \
    --output results/audit_deepseek_v4_$(date +%Y%m%d_%H%M%S).json
```

### 8.3 与参考答案对比评估

```bash
python evals/eval_v2.py \
    --results results/audit_deepseek_v4_xxx.json \
    --dataset datasets/posthoc_audit_qa.json \
    --output results/audit_deepseek_v4_xxx_judged.json
```

---

## 9. 文件清单

| 文件 | 职责 |
|------|------|
| `src/qa/pipeline.py` | 主 Pipeline 编排 |
| `src/qa/agent_loop.py` | ReAct 决策循环 |
| `src/qa/expansion.py` | 渐进式代码展开 |
| `src/qa/prompts.py` | Prompt 构建器 |
| `src/qa/models.py` | 数据模型（RetrievedFunction, QAResult 等） |
| `src/qa/trace.py` | 实验追踪记录器 |
| `src/core/llm_client.py` | 统一 LLM 调用层 |
| `src/core/model_config.py` | 模型注册表 |
| `prompts/react_decide.txt` | ReAct 决策 prompt |
| `prompts/answer_generation.txt` | 答案生成 prompt |
| `prompts/expansion_decide.txt` | 展开决策 prompt |
| `scripts/eval/debug_run_single.py` | 单题调试脚本 |
| `experiments/run_v2_benchmark.py` | 批量 benchmark |
| `evals/eval_v2.py` | 评估脚本 |

---

## 10. 下一步建议

1. **改 ReAct prompt 加 body preview** —— 让 DeepSeek 基于代码而非函数名做决策
2. **JSON 解析加固** —— 自然语言返回时，用正则提取 JSON 块，不直接 fallback
3. **召回过滤** —— 降低 grep 噪音（过滤测试文件、过滤其他后端实现）
4. **改评估标准** —— 从"结论一致"改为"证据命中率 + 结论方向性"
5. **补全参考答案** —— 把 DeepSeek 发现的真正问题补充进参考答案
