# 0801 交接文档：Repository QA 研究进度

记录日期：2026-08-01

本文档用于交接当前 Repository QA 研究的进度，方便后续开发者快速了解现状并继续推进。

---

## 1. 当前核心方向

**Concept-Symbol + ReAct 混合调查**

核心思路：
1. **Concept-Symbol** 作为初始找到出发点的手段：用 Concept-Symbol retrieval 得到准确的函数位置（name, file_path, start_line, end_line）
2. **ReAct** 作为扩展方法：根据已有信息排除失败方向，重新定位出发点，逐步构建证据链

关键原则：
- 不是完全自主 ReAct，而是在 Concept-Symbol 准确基础上做有限扩展
- Agent 必须实际读取函数实现，不能凭经验猜测
- 找到相关文件后必须 read，不能只是知道存在就跳过

---

## 2. 已实现的功能

### 2.1 建图与抽象层

| 功能 | 文件 | 状态 |
|---|---|---|
| Tree-sitter 建图 | `src/ingestion/tree_sitter_pipeline.py` | ✅ |
| Module/Concept 层次聚类 | `scripts/analysis/experiment_hierarchical_abstraction.py` | ✅ |
| 语义关系提取（Temporal/Implicit） | `scripts/analysis/extract_semantic_relations.py` | ✅ |

### 2.2 Retrieval

| 功能 | 文件 | 状态 |
|---|---|---|
| Concept-Symbol retrieval | `scripts/qa/run_concept_symbol_qa.py` | ✅ |
| Module-Symbol retrieval | `scripts/qa/run_module_symbol_qa.py` | ✅ |
| Embedding 统一封装 | `src/core/embedding_client.py` | ✅ |

### 2.3 ReAct 调查

| 功能 | 文件 | 状态 |
|---|---|---|
| ReAct Agent（Concept-Symbol 初始 + 扩展） | `scripts/qa/run_react_concept_symbol_qa.py` | ✅ |
| Grep 调用链工具 | `src/qa/tools/grep_call_chain.py` | ✅ |
| 统一调用链接口 | `src/qa/tools/call_chain.py` | ✅ |

### 2.4 评估

| 功能 | 文件 | 状态 |
|---|---|---|
| Binary judge + Citation judge | `evals/eval_v2.py` | ✅ |
| 严格 citation judge（减少误判） | `prompts/citation_judge.txt` | ✅ |

---

## 3. 当前最佳结果

### Concept-Symbol（glm-5.2）

| 指标 | 结果 |
|---|---|
| Binary Judge | 98.0% |
| Full Coverage | 66.0% |
| Avg Coverage | 73.8% |

### ReAct（deepseek-v4-flash，冒烟测试 10 题）

| 版本 | 配置 | 覆盖率 |
|---|---|---|
| V11 | 无强制防循环， max_steps=20 | **56.0%** |
| V13 | 强制防循环 finish | 40.0% |
| V15 | 强制防循环 search_symbol, max_steps=20 | 48.0% |
| V17 | 文件相关性规则 | 52.0% |
| V18 | 强制 read 机制 | 验证中 |

**当前最佳：V11（无强制防循环 + max_steps=20）**

---

## 4. 已知问题与改进方向

### 4.1 已解决的问题

1. **Tree-sitter CALLS 边不精确**：改用 grep 实现 callers/callees
2. **read_function 找不到函数**：修复大括号匹配和文件路径带行号问题
3. **Agent 编造内容**：加入诚实原则，只能引用实际访问的文件
4. **Citation judge 误判**：严格 prompt，误判率从 28% 降低

### 4.2 未解决的问题

1. **初始函数质量**：Concept-Symbol 给的初始函数不相关时，Agent 会被带偏（如 posthoc_public_001 的 `compare_dev`）
2. **"找到相关文件必须 read"提示无效**：Agent 找到了 `common/chat-diff-analyzer.cpp` 但没读取（002, 005, 006）
3. **过早 finish**：Agent 在 3-4 步就 finish，没有充分探索（007）
4. **大文件读取**：需要按函数边界拆分读，当前 read_file 截断 5000 字符

### 4.3 正在验证的改进

- **强制 read 机制**：grep_callers/search_symbol 找到文件后，下一步必须 read_file（V18 验证中）
- **文件相关性规则**：只有读完整文件才能标记为不相关，读到一半发现相关可停下来标记为相关

---

## 5. 下一步计划

### 短期（本周）

1. **完成 V18 冒烟测试**：验证强制 read 机制是否有效
2. **实现大文件拆分读**：按函数边界拆分，用子 agent 读大文件
3. **跑完整 50 题 benchmark**：在冒烟测试确认效果好后跑全量

### 中期（下周）

1. **改进初始函数质量**：Concept-Symbol + 语义关系扩展，提高 retrieval 召回率
2. **混合 Concept-Symbol + ReAct**：Concept-Symbol 做主要 retrieval，ReAct 做有限扩展
3. **优化 citation judge**：进一步减少误判

### 长期

1. **Hierarchical Investigation Agent**：在 concept 层做 ReAct，再下钻到函数级
2. **Evidence Tree 导航**：利用 Temporal/Implicit 关系做图导航
3. **论文撰写**：整理所有实验结果，形成完整研究故事

---

## 6. 关键文件说明

### 核心脚本

- `scripts/qa/run_concept_symbol_qa.py`：Concept-Symbol QA（当前最佳）
- `scripts/qa/run_react_concept_symbol_qa.py`：ReAct QA（改进中）
- `scripts/qa/run_module_symbol_qa.py`：Module-Symbol QA
- `evals/eval_v2.py`：评估脚本

### 核心工具

- `src/qa/tools/grep_call_chain.py`：Grep 调用链工具
- `src/qa/tools/call_chain.py`：统一调用链接口
- `src/core/embedding_client.py`：Embedding 统一封装
- `src/core/llm_client.py`：LLM 统一调用层

### 文档

- `docs/research/0714/0714_experiment_summary.md`：早期实验总结
- `docs/research/0717/0717_graph_ingestion_scalability.md`：建图优化
- `docs/research/0718/`：Tree-sitter 和层次聚类实验
- `MANUAL_RUN.md`：手动运行手册

---

## 7. 快速开始

### 环境配置

```bash
cp .env.example .env
# 填写 Neo4j、REPO_ROOT、OPENAI_API_KEY、DEEPSEEK_API_KEY 等
```

### 运行 Concept-Symbol QA（当前最佳）

```bash
python scripts/qa/run_concept_symbol_qa.py --model glm-5.2 --workers 10
```

### 运行 ReAct QA（改进中）

```bash
python scripts/qa/run_react_concept_symbol_qa.py --model deepseek-v4-flash --max-steps 20 --workers 30
```

### 评估

```bash
python evals/eval_v2.py \
    --result results/qa_concept_symbol_glm-5.2.json \
    --benchmark datasets/benchmark_hard.json \
    --range all \
    --model gpt-4.1-mini \
    -o results/eval_concept_symbol_glm52.json \
    -w 20
```

---

## 8. 联系与协作

当前主要开发者：zzy

如有问题或建议，请通过 GitHub Issues 或邮件联系。

---

**最后更新：2026-08-01**
