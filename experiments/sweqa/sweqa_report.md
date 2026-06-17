# SWE-QA Benchmark 评测报告

## 1. 评测结果总览

15 个 Python 项目，Graph-Agent (Graph+RAG Hybrid) 评测结果：

| 项目 | Total | Correctness | Completeness | Relevance | Clarity | Reasoning |
|------|-------|-------------|--------------|-----------|---------|-----------|
| conan | **41.77** | 8.30 | 8.20 | 8.90 | 8.00 | 8.40 |
| requests | **41.50** | 8.50 | 6.70 | 9.50 | 8.60 | 8.20 |
| astropy | **41.42** | 8.20 | 7.30 | 9.50 | 8.10 | 8.30 |
| reflex | **41.02** | 8.10 | 8.00 | 8.90 | 7.90 | 8.20 |
| pylint | **40.58** | 8.20 | 6.60 | 9.50 | 8.40 | 8.00 |
| sympy | **40.33** | 8.00 | 6.60 | 9.40 | 8.40 | 8.00 |
| xarray | **40.25** | 8.10 | 6.80 | 9.40 | 8.10 | 7.90 |
| pytest | **40.19** | 8.10 | 6.20 | 9.50 | 8.40 | 7.90 |
| flask | **39.94** | 7.83 | 6.56 | 9.04 | 8.50 | 8.00 |
| django | **39.85** | 8.15 | 6.48 | 9.33 | 8.23 | 7.67 |
| scikit-learn | **39.45** | 8.10 | 5.90 | 9.30 | 8.40 | 7.70 |
| streamlink | **39.40** | 7.70 | 7.60 | 8.40 | 7.70 | 8.00 |
| sphinx | **37.92** | 7.56 | 5.81 | 9.04 | 8.10 | 7.40 |
| matplotlib | **37.65** | 7.70 | 6.10 | 8.70 | 7.80 | 7.40 |
| sqlfluff | **37.12** | 7.52 | 5.83 | 8.90 | 7.67 | 7.21 |
| **平均** | **39.89** | **7.99** | **6.62** | **9.12** | **8.17** | **7.92** |

---

## 2. 维度分析

| 维度 | 平均分 | 最高项目 | 最低项目 |
|------|--------|----------|----------|
| **Total** | **39.89** | conan (41.77) | sqlfluff (37.12) |
| Relevance | **9.12** | requests/astropy/pytest (9.50) | streamlink (8.40) |
| Clarity | **8.17** | requests (8.60) | sqlfluff (7.67) |
| Correctness | **7.99** | requests (8.50) | sqlfluff (7.52) |
| Reasoning | **7.92** | conan (8.40) | sqlfluff (7.21) |
| Completeness | **6.62** | conan (8.20) | sqlfluff (5.83) |

**结论**:
- **Relevance 最优**（9.12/10）：Graph+RAG 混合检索能精准找到相关代码
- **Completeness 最低**（6.62/10）：主要短板，Agent 回答详尽程度有待提升

---

## 3. 项目分布

```
50+ ┤
45  ┤
40  ┤                          ●●●●●●●●●●●●●●
    ┤                  ●●●●●●●●●●●●●●●●●●●●●●●●●●
35  ┤  ●●●●●●
    ┤
30  ┤
    └─────────────────────────────────────────────
       conan req astropy reflex pylint sympy xarray ...
```

- **40 分以上**: 8 个项目（conan, requests, astropy, reflex, pylint, sympy, xarray, pytest）
- **39-40 分**: 4 个项目（flask, django, scikit-learn, streamlink）
- **38 分以下**: 3 个项目（sphinx, matplotlib, sqlfluff）

---

## 4. 方法论

Graph-Agent 采用 **Graph+RAG Hybrid** 架构：

1. **Graph Retrieval**: 从 Neo4j 代码知识图谱检索相关函数、类及其调用关系（CALLS, HAS_METHOD, CONTAINS 等）
2. **RAG Retrieval**: 从语义索引检索相关代码片段
3. **Hybrid Fusion**: 融合两种检索结果作为 LLM 上下文
4. **LLM**: 使用 gpt-4.1-mini 生成答案

---

## 5. 性能指标

### 时延（671 条问题统计）

| 指标 | 值 |
|------|-----|
| 平均时延 | 22.2s |
| P50 时延 | 19s |
| P90 时延 | 45s |
| 最小时延 | 0s |
| 最大时延 | 145s |

### Token 消耗（Flask 3 条问题采样）

| 指标 | 值 |
|------|-----|
| 平均 Total Tokens | ~5,758 |
| 平均 Prompt Tokens | ~5,476 |
| 平均 Completion Tokens | ~282 |

> 注：Token 消耗与问题复杂度高度相关，复杂问题（如需要多步检索）token 消耗可达 13,000+，简单问题约 2,000。

---

## 6. 结论

- Graph-Agent 在 15 个 Python 项目上平均 **39.89/50**
- Relevance 维度表现最优（9.12），说明结构化知识检索能精准匹配问题
- Completeness 是主要短板（6.62），后续可从 multi-step retrieval 和答案展开策略入手优化

## 7. 后续改进方向

1. **Completeness 提升**: 增加 multi-step retrieval 深度，引导 Agent 展开更多细节
2. **LLM 升级**: 换用 GPT-4o 或 Claude-3.5 可能带来显著提升
3. **图查询优化**: 增强 CALLS/HAS_METHOD 关系的利用效率
4. **失败案例分析**: 针对低分项目（sqlfluff、matplotlib）深入分析检索和回答策略

---

*评测时间: 2026-04-06 ~ 2026-04-07*
*结果文件: experiments/sweqa/summary.json*
