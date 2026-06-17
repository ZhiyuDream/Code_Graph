# Eval 评估方法

## 核心原则

**不要自己写正则提取答案中的文件引用。直接调用 LLM 判断。**

原因：
- LLM 答案中的引用格式千变万化（中文冒号、多段行号、Unicode 连字符、无反引号等）
- 正则永远有漏网之鱼，导致数据严重失真
- LLM 可以直接理解语义，判断"这个答案是否覆盖了这些 gold evidence"

## 标准评估流程

### 1. 输入

对每道题目，向评估 LLM 提供：
- **原始问题**
- **Gold Evidence**（全部，但标注哪些可以忽略）
- **参考答案**
- **生成答案**

### 2. 评估 Prompt

```
你是一位严格的代码审查评估专家。请对比以下信息，评估 AI 生成答案的质量。

【原始问题】
{question}

【参考答案】
{reference_answer}

【Gold Evidence】
以下是需要被覆盖的关键证据（排除 .h/.hpp 头文件，只看 .cpp/.c 实现文件）：
{gold_evidence}

【生成答案】
{generated_answer}

---

请从以下维度评估：

1. **检索覆盖率**：生成答案是否提到了 gold evidence 中的每个文件？
   - 只要答案中明确引用了该文件（无论是否带行号），就算覆盖
   - 忽略 .h/.hpp 头文件
   - 如果 gold evidence 中有多个条目指向同一文件的不同行号，只要引用了该文件就算覆盖

2. **引用完整性**：生成答案的结论是否与 gold evidence 支持的结论一致？
   - 不只是"提到了文件"，而是"基于这些文件得出了正确结论"

3. **缺失分析**：如果某 gold 文件未被引用，分析原因：
   - A. 检索失败（答案中完全没提到该文件）
   - B. 搜到未引（答案中提到了该文件但没有将其作为核心证据）
   - C. 答案本身不需要该文件也能得出正确结论

返回 JSON：
{
  "retrieval_coverage": "0%-100%",
  "citation_coverage": "0%-100%",
  "missing_files": ["file1.cpp", "file2.cpp"],
  "missing_reason": "A|B|C",
  "conclusion_consistent": true|false,
  "notes": "简短说明"
}
```

### 3. 批量评估脚本

使用 `evals/eval_v2.py` 或类似的脚本，并行调用 LLM 评估每道题。

关键参数：
- 排除 `.h` / `.hpp` 头文件
- 多 gold 条目指向同一文件时，去重后计算覆盖率
- 使用便宜的模型（如 gpt-4o-mini）做评估即可

### 4. 输出格式

评估结果保存为 JSON，每道题包含：
```json
{
  "qa_id": "posthoc_audit_001",
  "retrieval_coverage": 1.0,
  "citation_coverage": 1.0,
  "missing_files": [],
  "missing_reason": "",
  "conclusion_consistent": true,
  "eval_notes": ""
}
```

## 历史数据（权威结果）

### Easy Benchmark（`benchmark_symbol_fastpath_20260607_131010.json`）

**人工评估结果**（`easy_benchmark_per_question.md`）：
| 指标 | 数值 |
|------|------|
| 检索全召回 | **48/50 (96%)** |
| 引用全 | **44/50 (88%)** |
| 引用部分 | **6/50** |

6 道引用不全的题目：
- audit_020：检索失败（arg.cpp 没搜到）
- audit_011：搜到未引（speculative.cpp）
- audit_012：搜到未引（common.cpp）
- audit_014：搜到未引（common.cpp）
- audit_016：搜到未引（preset.cpp）
- audit_037：搜到未引（arg.cpp）

**LLM 评估结果**（`eval_citation_llm.py`，gpt-4.1-mini）：
| 指标 | 数值 |
|------|------|
| 引用全 | **46/50 (92%)** |
| 引用部分 | **3/50** |
| 引用零 | **1/50** |
| 平均覆盖率 | **95.0%** |

LLM 与人工评估的差异（LLM 更宽松）：
- LLM 把 audit_012、014、016、020 算成全引用（人工认为部分）
- LLM 把 audit_011 算成 0%（人工认为 67%）
- LLM 额外发现 audit_035、045 为部分覆盖（人工认为全引用）

> **建议**：以 LLM 评估为主，人工抽检为辅。LLM 对"引用"的语义理解更接近人类审稿标准。

### Hard Benchmark（`benchmark_hard_20260607_200601.json`）

| 指标 | 数值 |
|------|------|
| 检索全召回 | **29/50 (58%)** |
| 引用全 | **8/50 (16%)** |
| Binary Judge 正确率 | **46/50 (92%)** |

## 常见错误（不要重复）

❌ **自己写正则提取答案中的文件路径** → 永远会漏掉中文冒号、多段行号、无反引号等格式  
✅ **调用 LLM 判断** → 语义理解，准确率远高于正则

❌ **统计所有 gold evidence 条目** → 同一文件的多行条目会被重复计算  
✅ **去重到文件级别** → 只看 unique 文件，排除 .h/.hpp

❌ **只看"有没有提到文件名"** → 可能提到但没作为证据使用  
✅ **让 LLM 判断"是否作为核心证据引用"** → 更准确
