# 阶段 2 实现方案与 Code_Graph 目录约定

本文档说明：(1) 阶段 2（流程理解与 Workflow 入库）会如何实现；(2) 项目推荐的目录结构，便于各阶段代码不散不乱。

---

## 一、阶段 2 实现思路

### 1.1 输入与产出

- **输入**：Neo4j 中已有的 Function、Class、CALLS、CONTAINS（阶段 1 写入）；入口候选来源为图结构（无 CALLS 入边的 Function），不定死规则；后续可接入 agent（读文档、grep）再筛或增补。
- **产出**：Neo4j 中新增 **Workflow** 节点；**WORKFLOW_ENTRY**（入口 Function → Workflow）、**PART_OF_WORKFLOW**（参与该流程的 Function → Workflow）；可选 **Workflow.summary_text**（LLM 生成的流程描述）。

### 1.2 子步骤拆解

| 步骤 | 内容 | 实现要点 |
|------|------|----------|
| **2.1 入口候选发现** | 从图中得到「可能入口」列表，供后续展开 | 见下 1.3 |
| **2.2 调用图展开** | 从每个入口沿 CALLS 做 BFS/DFS，收集子图（节点 + 边） | Neo4j Cypher 或 Python 从 Neo4j 读 CALLS 后在内存建子图；可限制深度或节点数 |
| **2.3 子图序列化** | 把子图转成文本或结构化描述（函数名、调用链、文件路径） | 供 LLM 或直接展示；格式可为「入口 → 调用链层级」或边列表 |
| **2.4 LLM 总结（可选）** | 对子图描述做 prompt，生成自然语言流程摘要 | 用 .env 的 OPENAI_* 调用 LLM，结果写入 Workflow.summary_text |
| **2.5 写回 Neo4j** | 创建 Workflow 节点、WORKFLOW_ENTRY、PART_OF_WORKFLOW | 与阶段 1 的 neo4j_writer 风格一致，可单独写一个 workflow_writer 或扩展现有 writer |

### 1.3 入口候选发现（2.1 具体做法）

- **当前实现**：仅用图结构——在 Neo4j 中查「没有任何 CALLS 指向的 Function」作为候选：
  ```cypher
  MATCH (f:Function) WHERE NOT (():Function)-[:CALLS]->(f) RETURN f.id, f.name, f.file_path
  ```
  不定死任何命名或路径规则。
- **后续扩展**：可接入 **agent**（读仓库文档、grep 代码、或结合用户问题）产出或过滤入口列表，再交给展开与写库；阶段 3 拉取 Issue/PR 后，也可从正文识别入口表述并匹配到 Function。

### 1.4 调用图展开（2.2 具体做法）

- 对每个入口候选的 `Function.id`，在 Neo4j 中沿 CALLS 出边做 BFS（或 DFS），直到达到深度上限或节点数上限。
- 在内存中维护：本 Workflow 涉及的 `function_ids`、`calls` 边列表；可选同时收集经过的 Class（通过 Function 的 file_path 与 Class 的 file_path 同文件推断）。
- 子图结构可存为：`{ "entry_id": "...", "function_ids": [...], "edges": [(from_id, to_id), ...] }`，便于 2.3 序列化和 2.5 写 PART_OF_WORKFLOW。

### 1.5 Workflow 节点与关系

- **Workflow** 节点建议属性：`id`（唯一，如 `workflow_{entry_id}` 或 `workflow_{流程名}`）、`entry_function_id`、`summary_text`（可选）、`depth_limit` / `node_count`（展开时用的参数，便于复现）。
- **WORKFLOW_ENTRY**：`(Function)-[:WORKFLOW_ENTRY]->(Workflow)`，表示该 Function 是此 Workflow 的入口。
- **PART_OF_WORKFLOW**：`(Function)-[:PART_OF_WORKFLOW]->(Workflow)`，表示该 Function 参与此流程（被展开进子图）。

### 1.6 与现有代码的衔接

- **配置**：继续用 `config.py` 读 .env（Neo4j、OpenAI 等）；入口不定死规则，由图结构或后续 agent 决定。
- **Neo4j**：阶段 1 的 `clear_code_graph` 不删 Workflow（已注明保留）；阶段 2 只新增 Workflow 与边，不动的 Function/CALLS 保持不变。
- **执行入口**：单独脚本如 `run_stage2.py`，顺序：发现入口候选 → 对每个候选展开 → 可选 LLM 总结 → 写 Workflow 与边。

---

## 二、推荐目录结构（保持清晰、不散不乱）

当前 Code_Graph 根目录下文件较多（config、多个 parser、多个 run_*.py、文档混在一起），建议按职责分层，便于阶段 2/3/4 扩展后仍易读。

### 2.1 推荐布局

```
Code_Graph/
├── .env
├── README.md
├── requirements.txt
├── env.example.yml
│
├── config/                    # 可选静态配置（非 .env）
│
├── docs/                       # 设计/说明文档，与代码分离
│   ├── DESIGN.md               # 整体设计（可从根目录移入或保留根目录一份链接）
│   ├── STAGE2_实现与目录约定.md # 本文档
│   └── clangd20升级说明.md
│
├── src/                        # 核心逻辑（可被 run 脚本 import）
│   ├── __init__.py
│   ├── config.py               # 从 .env 读配置
│   ├── ast_parser.py           # 阶段 1：libclang 解析
│   ├── graph_builder.py        # 阶段 1：建图
│   ├── neo4j_writer.py         # 阶段 1：写 Neo4j
│   ├── clangd_client.py        # 阶段 1：clangd LSP 客户端
│   ├── clangd_parser.py        # 阶段 1：clangd 解析
│   │   # 阶段 2 新增（示例）：
│   ├── entry_candidates.py     # 入口发现（图结构；可接 agent）
│   ├── workflow_expand.py      # 调用图展开
│   ├── workflow_llm.py         # 可选 LLM 总结
│   └── workflow_writer.py      # 写 Workflow / WORKFLOW_ENTRY / PART_OF_WORKFLOW
│
└── scripts/                    # 可执行入口，薄层调用 src
    ├── run_stage1.py           # 阶段 1 libclang
    ├── run_stage1_clangd.py    # 阶段 1 clangd
    └── run_stage2.py           # 阶段 2 流程理解
```

### 2.2 说明

- **config/**：可选静态配置（非 .env）；`.env` 仍放根目录（含敏感信息、不提交）。
- **docs/**：DESIGN、升级说明、本文档集中放这里，根目录只保留 README，便于一眼看到「怎么跑」。
- **src/**：所有被复用的解析、建图、写库、入口发现、展开、LLM、workflow 写回，都放在 `src/`，统一用 `from src.xxx import ...` 或 `import src.xxx`（若包名不用 `src` 也可用 `code_graph` 等）。
- **scripts/**：只做「读配置、调 src、打印进度」的薄脚本，便于区分「入口」和「库逻辑」。

### 2.3 迁移注意

- 若采纳本布局，需要：把现有 py 文件挪到 `src/`，把 run_*.py 挪到 `scripts/`，并修改各处的 `import`（如 `from config import ...` → `from src.config import ...`，或在 scripts 里 `sys.path.insert` 指向项目根再 `import`）。
- 文档是否迁入 `docs/` 可一步到位或逐步做；README 中可加一句「设计文档见 `docs/`」。

---

## 三、实现顺序建议（阶段 2）

1. **入口发现**：实现 `entry_candidates.py`（读 Neo4j：图结构「无 CALLS 入边」→ 输出候选列表）；不定死规则，后续可接 agent。
2. **调用图展开**：实现 `workflow_expand.py`（从 Neo4j 读 CALLS，从每个入口 BFS/DFS，输出子图结构）。
3. **写回 Neo4j**：实现 `workflow_writer.py`（Workflow 节点 + WORKFLOW_ENTRY + PART_OF_WORKFLOW）。
4. **串联脚本**：`run_stage2.py` 调用上述三步；可选再接入 LLM 总结（`workflow_llm.py`）并写入 `Workflow.summary_text`。
5. **目录整理**：若采纳第二节的目录结构，可在阶段 2 开发前或开发中做一次迁移，避免后续文件更多时再挪更乱。

---

*本文档作为阶段 2 实现与目录约定的唯一说明，后续实现按此执行；若调整以本文档更新为准。*
