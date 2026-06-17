# Code_Graph

面向大型 C++ 仓库（以 [llama.cpp](https://github.com/ggerganov/llama.cpp) 为主）的**事后审计问答（post-hoc repository audit QA）**研究框架。

## 当前研究目标

解决仓库级技术问题的端到端流程中，LLM 在**已到达正确代码区域后仍无法定位关键证据函数**的问题。

核心流程：

```
Question
  → Relevant Code Region     (file-level retrieval)
  → Candidate Symbol Set     (function-level candidate construction)
  → Evidence-bearing Symbol  (symbol discrimination / selection)
  → Expansion                (callers / callees / same-file neighbors)
  → Answer
```

## 关键发现

基于 `datasets/benchmark_hard.json`（50 题）和最近的分解实验：

| Stage | Recall / Coverage |
|-------|-------------------|
| File-level retrieval @10 | ~90% |
| Global function retrieval @10 | ~47% |
| Two-stage candidate construction (20 files × 5 funcs) | ~80% |
| LLM multi-selection recall | ~53% |
| Oracle expansion coverage | ~80% |

**结论**：
- **文件检索**已经不是主要瓶颈。
- **函数级候选构造**（从相关文件内召回函数）能显著提升上限。
- **候选判别**（从一堆语义相关函数中选出真正承载证据的函数）仍是核心难题。
- 调用图 expansion 本身天花板很高；问题在于 expansion 的起点选择。

## 评测标准

采用 **post-hoc audit benchmark**：

- 每题包含人工标注的 `gold_evidence`，即回答问题所需的关键代码位置（文件 + 行号区间）。
- 核心指标：**Coverage** = 模型召回的证据文件数 / 总 gold evidence 文件数。
  - `avg_coverage`：每题 coverage 的平均值。
  - `full_coverage_rate`：完全覆盖所有 gold evidence 的题目比例。
- 辅助指标：
  - `file_recall@k`：gold 文件是否进入 top-k 文件。
  - `symbol_recall@k`：gold 函数是否进入 top-k 函数候选。
  - `selection_recall`：LLM 是否从候选中选中 gold 函数。

详见 `evals/eval_v2.py` 和 `datasets/benchmark_hard.json`。

## 目录结构

```
Code_Graph/
├── config.py                  # 统一配置加载（.env）
├── src/
│   ├── ingestion/             # 基于 clangd LSP 的代码图构建
│   ├── core/                  # Neo4j 客户端、LLM 客户端、prompt 加载
│   ├── search/                # 语义检索、调用链扩展、grep、代码读取
│   └── qa/                    # QA agent、检索器、调查策略
├── scripts/
│   ├── ingestion/             # 建图入口
│   ├── github/                # Issue / PR 摄取
│   ├── qa/                    # QA 流水线入口
│   ├── eval/                  # 评测脚本
│   └── analysis/              # 各种分析脚本
├── experiments/               # 研究实验脚本
├── prompts/                   # LLM prompt 模板
├── datasets/                  # benchmark 数据
├── evals/                     # 评测逻辑
├── docs/research/             # 研究笔记、实验报告
└── results/                   # 实验结果（gitignored）
```

## 快速开始

### 1. 环境

```bash
conda create -n code_graph python=3.11
conda activate code_graph
pip install -r requirements.txt
```

复制 `env.example.yml` 为 `.env` 并填写：
- `REPO_ROOT`：目标仓库绝对路径，如 `/data/users/zzy/RUC/llama.cpp`
- `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`
- `OPENAI_API_KEY`, `OPENAI_BASE_URL`
- `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`

### 2. 构建代码图

目标仓库需先生成 `compile_commands.json`：

```bash
cd $REPO_ROOT
mkdir -p build && cd build
cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON ..
```

然后运行摄取脚本（需要 clangd 20+）：

```bash
python scripts/ingestion/ingest_code.py
```

### 3. 运行实验

以当前核心实验为例：

```bash
# 检索 vs 选择分解实验
python experiments/run_retrieval_selection_decomposition.py --range 0,15 -w 5

# 文件 / 函数召回分解
python experiments/run_file_symbol_recall_decomposition.py --range 0,15

# 端到端函数级 pipeline
python experiments/run_end_to_end_function_pipeline.py --range 0,15 -w 5
```

### 4. 评测

```bash
python evals/eval_v2.py -i results/<output>.json -o results/<output>_eval.json
```

## 重要研究文档

- `docs/research/0616/repository_audit_function_localization.md`：最新研究方向
- `docs/research/0613/oracle_coverage_gap_analysis.md`：oracle expansion 分析
- `docs/research/0610/hard_benchmark_retrieval_failure_analysis.md`：困难 benchmark 检索失败分析

## 注意事项

- 跨文件调用解析依赖 **clangd 20+**；clangd 14–19 会产生空 CALLS 边。
- 实验结果默认写入 `results/`（已 gitignore）。
- 当前活跃分支：`feat/navigation-architecture`。
