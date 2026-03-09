# Code_Graph

面向**大型 C++ 仓库的代码理解**：构建代码图（code graph），支持仓库级问答与执行流程/工作流类问题。

## 目标

- **节点**：函数、文件、类（可选）
- **边**：函数调用关系、定义关系
- **用途**：辅助阅读、RAG 检索、**工作流/流程类问题**（如：关机流程、初始化顺序、请求到达时的执行路径），以及基于调用图的流程重建（entry → 沿调用图展开 → 汇总为 workflow）。

## 前置条件

1. **compile_commands.json**  
   目标仓库（如 llama.cpp）需能生成该文件。llama.cpp 已开启 `CMAKE_EXPORT_COMPILE_COMMANDS ON`，在仓库根目录执行例如：
   ```bash
   mkdir -p build && cd build && cmake ..
   ```
   会在 `build/compile_commands.json` 生成编译数据库。解析脚本需要指向该文件或将其复制/链接到约定路径。

2. **clangd（可选，用于 IDE 解析）**  
   若希望编辑器/IDE 用 clangd 解析全部 C++ 文件，需在系统上安装 clangd，并让 clangd 使用上述 `compile_commands.json`（例如在项目根放置或通过 `-compile-commands-dir` 指定）。  
   当前服务器若未安装，见项目根目录 `项目编写历史.md` 中的安装说明。

3. **图提取脚本的依赖**  
   本目录下的调用图/代码图提取脚本计划基于 **Clang AST**（如 Python `libclang`）或 **clangd LSP** 之一实现，依赖 `compile_commands.json` 与对应运行时（见 README 或脚本内说明）。

## 目录与脚本（规划）

- **阶段 1（已实现）**：  
  - `run_stage1.py`：基于 **libclang** 批量解析，构建 Repository、Directory、File、Function、Class、**Variable** 及 CONTAINS、CALLS、**REFERENCES_VAR**（Function→Variable，含引用行号），写入 Neo4j。**Variable 与“变量在哪里用到”仅由本脚本产出**；clangd 版暂不采集变量。  
  - `run_stage1_clangd.py`：基于 **clangd LSP**（documentSymbol + call hierarchy），与 IDE 行为一致，跨文件 CALLS 解析更好；需本机安装 clangd，首次运行可能较慢。**若结果中 CALLS=0**：`callHierarchy/outgoingCalls` 在 **clangd 20** 才加入（PR #77556，2024-11-26 合并进 main），**clangd 14～19 均不支持**；可升级到 clangd 20+ 或改用 `run_stage1.py`（libclang）获取调用边。  
  均采用方案 B（有 File 与目录节点）。
- **阶段 2（已实现）**：`run_stage2.py` 从 Neo4j 发现入口候选（图结构：无 CALLS 入边的 Function），沿 CALLS 展开得到调用子图，创建 Workflow 节点及 WORKFLOW_ENTRY、PART_OF_WORKFLOW 写入 Neo4j。入口不定死规则，后续可接入 agent。详见下方「使用说明（阶段 2）」与 `docs/STAGE2_实现与目录约定.md`。
- **阶段 3（已实现）**：`run_stage3.py` 使用 `.env` 中的 **GITHUB_TOKEN** 调用 GitHub API，拉取仓库 Issue 与 Pull Request，写入 Neo4j（Issue、PullRequest 节点；PR 含 changed_paths；FIXES 边 PR→Issue）。查询「该 PR 涉及哪些函数」时用 Function.file_path 与 PR.changed_paths 匹配。详见下方「使用说明（阶段 3）」。
- **依赖**：`requirements.txt`（neo4j、python-dotenv、libclang、requests）。若使用 conda，可创建环境后安装，见 `env.example.yml` 或下方使用说明。
- **环境变量**（.env）：`NEO4J_*`；`REPO_ROOT`（仓库根目录）；可选 `COMPILE_COMMANDS_DIR`；阶段 3 需 `GITHUB_TOKEN`，可选 `GITHUB_REPO=owner/repo`（未设置时从 REPO_ROOT 的 git remote 推导）。

## 与整体 pipeline 的关系

```
仓库（如 llama.cpp）
  → compile_commands.json（CMake 生成）
  → 本目录脚本：解析 → 代码图（函数/文件/类 + 调用/定义边）
  → 后续：函数摘要与 embedding、RAG 检索、workflow 问题下的 call-graph 展开与 LLM 汇总
```

详细需求与方案选择见根目录 `项目编写历史.md`。

---

## 使用说明（阶段 1）

1. **准备 compile_commands.json**  
   在待解析仓库（如 llama.cpp）根目录执行：`mkdir -p build && cd build && cmake ..`，得到 `build/compile_commands.json`。

2. **配置 .env**  
   在 `Code_Graph/.env` 中设置：
   - `REPO_ROOT`：仓库根目录的绝对路径（例如 `/data/yulin/RUC/llama.cpp`）。
   - `NEO4J_URI`、`NEO4J_USERNAME`、`NEO4J_PASSWORD`、`NEO4J_DATABASE`（若尚未配置）。

3. **（可选）创建 conda 环境**  
   若需独立环境，可在本机执行：
   ```bash
   conda create -n code_graph python=3.11
   conda activate code_graph
   pip install -r requirements.txt
   ```
   若系统缺少 libclang 动态库，可安装：`conda install -c conda-forge libclang` 或系统包（如 `libclang-14-dev`），并按需设置 `LIBCLANG_PATH`。

4. **运行阶段 1**  
   - **libclang 版**（默认，批量解析、不依赖 clangd 进程）：
     ```bash
     python run_stage1.py
     ```
   - **clangd 版**（通过 LSP documentSymbol + call hierarchy，与 IDE 一致，跨文件调用解析更好；首次可能较慢，llama.cpp 规模约数分钟到十几分钟）：
     ```bash
     python run_stage1_clangd.py
     ```
   两种方式都会清空 Neo4j 中现有代码图、写入新图并更新 `Repository.last_processed_commit`。

**clangd 版本与 CALLS**：LSP 的 `callHierarchy/outgoingCalls`（用于采集“谁调谁”）在 **clangd 20** 才实现（[PR #77556](https://github.com/llvm/llvm-project/pull/77556)，2024-11-26 合并）。**clangd 14～19 均无此能力**，跑 `run_stage1_clangd.py` 时 Function/Class 会有，但 CALLS 会一直是 0。若需用 clangd 采调用边，请安装 **clangd 20+**（如 [LLVM 官网](https://releases.llvm.org/) 或包管理器）；否则用 `run_stage1.py`（libclang）即可得到 CALLS。

---

## 使用说明（阶段 2）

1. **前置**：先完成阶段 1（`run_stage1.py` 或 `run_stage1_clangd.py`），Neo4j 中已有 Function、CALLS 等。

2. **运行阶段 2**：
   ```bash
   python run_stage2.py
   ```
   脚本会：从图中发现入口候选（无 CALLS 入边的 Function）→ 沿 CALLS BFS 展开 → 清空已有 Workflow 后写入新 Workflow 节点及 WORKFLOW_ENTRY、PART_OF_WORKFLOW。

3. **展开参数**：默认深度 5、单 Workflow 节点数上限 500；可通过环境变量覆盖：
   - `WORKFLOW_DEPTH_LIMIT`（默认 5）
   - `WORKFLOW_NODE_LIMIT`（默认 500）

实现细节与推荐目录结构见 **`docs/STAGE2_实现与目录约定.md`**。

---

## 使用说明（阶段 3）

1. **配置**：在 `.env` 中设置 **GITHUB_TOKEN**（GitHub 个人访问令牌）。若仓库不是通过 REPO_ROOT 的 git remote 推导，可设置 **GITHUB_REPO=owner/repo**（例如 `ggerganov/llama.cpp`）。

2. **运行阶段 3**：
   ```bash
   python run_stage3.py
   ```
   脚本会拉取该仓库全部 Issue 与 PR（含 PR 的变更文件列表），清空 Neo4j 中已有 Issue/PullRequest 后写入，并建立 FIXES 边（PR body 中 fixes #n / closes #n 解析为 PR→Issue）。

3. **查询示例**：该 PR 涉及哪些函数（通过 file_path 匹配）：在应用层用 `Function.file_path IN pr.changed_paths` 或路径前缀匹配；或 Cypher 中先取 PR 的 changed_paths，再 `MATCH (f:Function) WHERE f.file_path IN $paths`。

---

## 使用说明（阶段 5 / QA 流水线）

1. **题目与策略**：见 **`docs/llama_cpp_QA_题目分类与检索策略.md`**。题目来源为项目根目录 **`llama_cpp_QA.csv`**（可由 `export_qa_to_csv.py` 从 xlsx 导出）。
2. **运行**：先完成阶段 1（Neo4j 中已有代码图），再执行：
   ```bash
   python run_qa.py [--csv PATH] [--limit N] [--output PATH] [--no-llm] [--eval] [--workers N]
   ```
   加 `--workers 8` 可并行处理 8 道题，加快全量测试（默认 4）。
   默认读 `llama_cpp_QA.csv`，输出 **JSON**（`qa_retrieval_results.json`），每条含：具体问题、意图、实体名称、路由类型、检索结果、参考答案、生成答案。加 `--eval` 会用 LLM 打 **0–1 分**并写入 `评价分数`、`评价说明`。**类型 A**：实体相关函数 + CALLS；**类型 B**：实体相关函数 + 沿 CALLS 1 跳邻域（架构/流程）；**类型 C**：实体相关函数经 **embedding 相似度**取 top-10 再查 CALLS（.env 需 `EMBEDDING_MODEL`、`OPENAI_API_KEY`）。
3. **当前实现**：按意图/问题类型路由到类型 A/B/C；类型 A 已用 Cypher 查「实体相关函数 + 调用关系」，类型 B/C 为占位。默认会调用 LLM 根据检索结果生成答案。

---

## 整体方案（流程理解 + 入库 + PR/Issue + 增量更新）

已在本目录撰写 **`DESIGN.md`**，包含：

- **Neo4j 图模型**：Repository / Commit / File / Function / Class / Workflow / Issue / PullRequest 及关系（CALLS, CONTAINS, PART_OF_WORKFLOW, CHANGED_IN, REFERENCES 等），支持版本与增量。
- **Pipeline 分阶段**：代码图采集 → 流程理解与入库 → Issue/PR 采集与关联 → 仓库变更时的增量更新。
- **流程理解方式**：基于调用图展开、LLM 总结、与 Issue/文档结合等，可组合使用。
- **PR/Issue**：数据来源（如 GitHub API）、与代码的关联方式、更新策略。
- **增量更新**：基于 `last_processed_commit` 与 `git diff` 只重算变更文件，更新 Neo4j。

实现顺序建议与需您确认的选项见 `DESIGN.md` 第八节。配置（Neo4j、OpenAI、LLM/Embedding 模型等）从项目根目录 `.env` 读取。
