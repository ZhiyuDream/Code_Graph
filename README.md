# Code_Graph

面向大型 C++ 仓库（以 [llama.cpp](https://github.com/ggerganov/llama.cpp) 为主）的**事后审计问答（post-hoc repository audit QA）**研究框架：给定一个仓库级技术问题，Agent 在代码图上自主调查，产出带文件引用的答案。

## 当前最好成绩（V63，2026-08）

在 `datasets/hard_benchmark.json`（50 题人工标注 gold evidence）上：

| 指标 | 数值 |
|------|------|
| **det（确定性引用准确率）** | **91.0%** |
| 全引用 / 部分引用 / 零引用 | **41 / 9 / 0** |

模型：deepseek-v4-flash。相对早期 ReAct 基线（84.8%，38/9/3）+6.2pp，且零引用清零。

复现配置与逐版本演进见 `docs/research/0816/0816_version_postmortems.md`；证据文件 `results/qa_v63_full.json` + `results/eval_v63_full_det.json`。

## 系统流程（V63）

```
Question
  → 多路召回        embedding / 关键词 / concept / code-first 融合，约 150 个函数候选池
  → 内容分诊        子 agent 并发（4 批 × 24）读池内前 96 个候选的完整代码，
                    逐个打 0-10 相关性分，池按分重排；全场低分触发 HyDE 反查重新检索
  → ReAct 调查      主 agent 30 步预算，工具：read_function / find_callers / find_callees /
                    search_symbol / list_functions / search_codebase
                    · frontier 调查账本：发现的线索持久化，分诊 ≥8.5 分给 HIGH 优先级
                    · 文件侦察：首次进入文件自动附该文件完整函数清单（File Map）
                    · 自动跟进：HIGH 线索挂起 5 步未读，系统直接读
                    · 调查记忆：已读函数的摘要缓存，避免重复读
  → 答案生成        同族实现逐一覆盖 + 引用守卫（只引用真正读过的文件）
  → Answer（带文件引用）
```

设计要点：召回只负责"给 Agent 一个初始入口"，不负责猜对函数；真正的定位靠 Agent 在调用图上的结构化调查。分诊解决"gold 在池内但排名沉底"，frontier + 自动跟进解决"发现了但没读"。

## 评测标准

- 每题含人工标注的 `gold_evidence`（回答问题所需的文件集合）。
- 核心指标 **det**：答案引用的文件与 gold 的确定性匹配准确率（只认答案正文里真实引用、且调查中真正读过的文件）。
- 统计口径：全引用（gold 全覆盖）/ 部分引用 / 零引用。
- 漏引分解四类：C 没搜到 / A 搜到没读 / B 读了没引 / D 编造引用。

评测实现：`evals/eval_v2.py`。

## 目录结构

```
Code_Graph/
├── config.py                  # 统一配置加载（.env）
├── src/
│   ├── ingestion/             # Tree-sitter 代码图构建（全量/增量）
│   ├── core/                  # Neo4j 客户端、LLM 客户端、prompt 加载
│   ├── search/                # 语义检索、调用链扩展、grep、代码读取
│   └── qa/                    # ReAct agent、分诊、调查记忆、工具集
├── scripts/
│   ├── ingestion/             # 建图入口（ingest_code.py 等）
│   ├── qa/                    # QA 入口（run_react_concept_symbol_qa.py 为现役）
│   ├── eval/                  # 评测辅助脚本
│   └── analysis/              # 轨迹导出、漏引分解、漏斗分析等
├── prompts/                   # 全部 LLM prompt 模板
├── datasets/                  # benchmark（hard_benchmark.json 为现役 50 题）
├── evals/                     # det 评测逻辑
├── docs/research/             # 研究笔记（0816 为最新版本复盘）
├── data/                      # embedding 索引、module/concept 抽象、调查记忆缓存
└── results/                   # 实验结果（gitignored）
```

## 快速开始

### 1. 环境

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

复制 `.env.example` 为 `.env` 并填写：

- `REPO_ROOT`：目标仓库绝对路径，如 `/home/zzy/RUC/llama.cpp`
- `NEO4J_URI` / `NEO4J_USERNAME` / `NEO4J_PASSWORD`：Neo4j 连接（项目内实例见 `.neo4j/`）
- `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL`（或 `OPENAI_API_KEY` / `OPENAI_BASE_URL`）

### 2. 构建代码图

```bash
python scripts/ingestion/ingest_code.py        # Tree-sitter 全量摄取到 Neo4j
```

QA 依赖的缓存（首次运行会自动构建，也可用 `scripts/ingestion/build_*.py` 预建）：

- `data/qa_embedding_index.json`：函数 embedding 索引
- `data/module_abstraction.json` / `data/concept_abstraction.json`：Louvain 模块 / 概念抽象

### 3. 跑 QA（当前定版配置）

```bash
python scripts/qa/run_react_concept_symbol_qa.py \
  --model deepseek-v4-flash --max-steps 30 --workers 30 \
  --memory --file-recon --pool-triage \
  --output results/qa_run.json

# 失败子集冒烟（推荐先小集合验证再全量）
python scripts/qa/run_react_concept_symbol_qa.py ... \
  --qa-ids posthoc_public_001,posthoc_public_013 --output results/smoke.json
```

常用开关：`--sc N`（self-consistency 多跑取优）、`--oracle-level {function,file,directory}`（Oracle 粒度实验）、`--file-centric`（文件中心调查）。

### 4. 评测

```bash
python evals/eval_v2.py \
  --result results/qa_run.json \
  --benchmark datasets/hard_benchmark.json \
  --range all --mode citation --citation-mode det \
  -o results/qa_run_det.json
```

### 5. 失败分析

```bash
python scripts/analysis/missed_breakdown.py ...        # 漏引分解（C/A/B/D 四类）
python scripts/analysis/dump_failure_trajectories.py ...  # 逐题完整轨迹导出
python scripts/analysis/funnel_analysis.py ...         # 失败边界漏斗
```

## 重要研究文档

- `docs/research/0816/0816_version_postmortems.md`：**最新**——V48→V63 逐版本复盘、成绩表、未全引题逐题归因、bug 清单
- `docs/research/0808/0808_system_overview.md`：系统全流程、分支、工具调用分析
- `docs/research/0809/0809_failure_deep_dive.md`：零引用/部分引用题逐步轨迹深读

## 注意事项

- benchmark 的 013/044 已替换为 V2 问法（原件备份 `datasets/hard_benchmark.json.bak-0809`）。
- 单轮全量 det 有 ±3-5pp 采样噪声；版本对比以"同 run 配对子集 > 全量双轮均值 > 单轮"为准。
- 实验结果默认写入 `results/`（已 gitignore）。
