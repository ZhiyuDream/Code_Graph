# 0620 研究笔记：从选择失败到探索失败

本目录汇总了最近在 llama.cpp 仓库级代码问答上的实验发现。研究叙事已经从"检索/选择是瓶颈"转变为"系统性探索是瓶颈"。

## 快速总结

| 方法 | 完整覆盖率 | 平均覆盖率 | 平均读取文件数 |
|--------|--------------:|-------------:|---------------:|
| Naive ReAct (top20, 10 步) | 22% | 50.3% | 3.3 |
| 读完 Top20 | 60% | 77.5% | 17.4 |
| Top20 + 目录扩展 | 78% | 87.3% | 25.6 |
| 读完 Top50 | 78% | 87.3% | 39.9 |
| 读完 Top100 | 86% | 91.0% | 75.1 |
| 读完 Top200 | 94% | 95.5% | 135.3 |
| **确定性 Worklist** (top20 + 2-hop 目录扩展) | **86%** | **91.0%** | **50.8** |
| 查询扩展 Worklist | 88% | 93.0% | ~55 |
| **符号图 Worklist** | **90%** | **95.0%** | ~62 |
| 探索感知 Agent | 62% | 80.8% | 36.0 |
| 检索天花板 (文件级 Top200) | 98% | - | - |

**核心结论：** 一个系统性扩展搜索边界的确定性 worklist 能以比读完 Top100 少 32% 的文件达到 86% 覆盖率，符号图扩展进一步推到 90%。而当前 LLM 驱动的探索 agent 仍不如确定性遍历。

## 本目录文档

- [`retrieval_and_selection_decomposition.md`](retrieval_and_selection_decomposition.md) — 早期分解实验：池召回率、gold 符号排名、选择准确率、成对/锦标赛、命名偏差。
- [`worklist_investigation.md`](worklist_investigation.md) — 确定性和混合 worklist agent：读完所有、目录扩展、查询扩展、符号图。
- [`search_budget_curve.md`](search_budget_curve.md) — 覆盖率随原始文件读取预算的变化曲线。
- [`exploration_aware_agent.md`](exploration_aware_agent.md) — LLM 引导的探索感知 agent 及其表现不佳的原因。
- [`key_findings_and_implications.md`](key_findings_and_implications.md) — 综合与下一步建议。

## 结果文件

实验结果保存在 `results/` 下：

- `top20_react_investigation_hard_0_50.json`
- `readall_top{20,50,100}_baseline_hard_L100.json`
- `direxp_top20_baseline_hard_L100_E2_M10.json`
- `worklist_det_hard_L100_K20_E5_M30_H2.json`
- `qexp_worklist_hard_L100_K20_Q4_E5_M15_H2.json`
- `symgraph_worklist_hard_L100_K20_E5_M15_H2_S5.json`
- `exploration_aware_hard_S20_R5_F15_L100.json`
- `search_budget_curve_hard_L100.json`
- `WORKLIST_EXPERIMENTS_SUMMARY.md`

## 日期

2026-06-20
