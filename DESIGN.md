# Code_Graph 整体方案设计

面向**单一大型 C++ 仓库**的流程理解与持久化：把代码图、工作流理解、PR/Issue 等写入 Neo4j，并支持仓库持续变更时的**增量更新**。  
配置（Neo4j、OpenAI、GITHUB_TOKEN 等）从本目录 `.env` 读取，不在此文档中写死凭证。

---

## 一、目标与约束

- **首要目标**：能够**回答问题**——对仓库级代码问题的自动问答。问题类型可参考 **SWE-QA** 等 benchmark：例如实体声明与调用（Entity Declaration and Call）、实体间的交互与关系（Interacting Entities）、跨文件推理、多跳依赖分析、意图理解等。图结构与 pipeline 以“能支撑这类问题的检索与推理”为准，对具体用哪种节点/层级无固定偏好。
- **范围**：仅考虑**单一 C++ 代码仓库**的解析与理解，不涉及多仓库统一建模。
- **持久化**：将代码图、流程摘要、以及 PR/Issue 等存入 **Neo4j**，便于检索、多跳推理和后续 RAG/LLM 使用。
- **PR / Issue**：把 PR、Issue 纳入图，并与代码实体（文件/函数）建立关联；使用 `.env` 中的 **GITHUB_TOKEN** 通过 GitHub API 拉取。
- **仓库会持续修改**：设计上支持**增量更新**，只维护**当前最新一张图**，怎么方便怎么实现。

---

## 二、Neo4j 图模型（已采用方案 A）

当前目标就是**能回答问题**（可参考 SWE-QA 的问题类型：声明与调用、实体交互、跨文件/多跳推理等）。图里需要**函数 / 类**与**调用关系**，以及和 PR/Issue/Workflow 的关联；文件与目录**不作为节点**，由 Function/Class 的 `file_path` 及其前缀推导。**已采用方案 A**：不建 Repository、Directory、**File** 节点，只保留 Function、Class（带 file_path）和 CALLS 等边，图更小、实现与增量更简单。root_path、last_processed_commit 放在运行配置或 Neo4j 的 Config 节点。下面 2.1–2.3 保留两种方案的对比理由供查阅；**实现按 2.5 方案 A 的节点与关系执行**。

**File 和 Directory 的语义区别**（供区分方案 B 或后续扩展时参考）：  
- **File** = 源文件（.cpp/.h），对应磁盘上的一个文件；是“装函数/类的容器”。  
- **Directory** = 目录（文件夹），只包含子目录或文件，不直接包含函数/类。  
方案 A 不建 Directory、也不建 File；“文件/目录”只作为 Function/Class.file_path 及其前缀出现。

---

### 方案 A：极简——仅 Function、Class（无 Repository、无 Directory、无 File）

**结构**：  
- 节点：`Function`（name, signature, file_path, start_line, end_line）、`Class`（name, file_path, …）；后续阶段再加 `Workflow`、`Issue`、`PullRequest`。**不建 Repository、Directory、File 节点**（root_path、last_processed_commit 放配置或 Config 节点）。  
- 边：Class -CONTAINS→ Function（可选）；Function -CALLS→ Function。  
- “在哪个文件/目录”由 **Function.file_path、Class.file_path** 推导，查询用 `WHERE file_path = '...'` 或 `STARTS WITH '...'`。

**这样做的理由**：

1. **单一事实来源**  
   目录信息只存在于“路径”里。所有“某目录下的文件/函数”都通过同一份 `path` 算出来，不会出现“图上目录和实际路径不一致”的问题。建了 Directory 节点的话，就要同时维护“路径字符串”和“图上的 CONTAINS 边”，重命名/移动时两处都要改，容易漏。

2. **增量更新更省事**  
   仓库持续修改时，我们只关心“哪些**文件**变了”，然后重解析这些文件、更新对应 file_path 下的 Function/Class 节点和 CALLS。若没有 Directory/File 节点，文件移动只需更新该 path 下所有 Function/Class 的 file_path，或删除旧 path 的节点并写入新 path 的节点。若有 Directory/File，就要额外维护目录/文件节点与边，逻辑更重。

3. **节点和边更少**  
   大仓里目录数量不少（几十到几百级），每个目录一个节点、再加 CONTAINS 边，图会大不少。不做 Directory 时，图主要是“文件 + 函数 + 类 + CALLS”，更贴近“我们真正要查的东西”（谁调谁、哪个文件有哪些函数），目录只是查询时的过滤条件。

4. **查询仍然能表达“按目录”**  
   “src/models 下有哪些函数” = `MATCH (f:Function) WHERE f.file_path STARTS WITH 'src/models/'`（或 Class 同理）。树形展示时，可在应用层用 `DISTINCT file_path` 或路径前缀建树（一次计算，可缓存），不需要图上有 Directory/File 节点。

**代价**：  
- 树形导航、按目录聚合都要在应用层从 path 算，不能“从根 Directory 节点往下遍历”；若你非常依赖“在图里从某目录节点展开子节点”这种交互，会多一层从 path 构建树的逻辑。

---

### 方案 B：显式层级——Directory（多级）→ File → Class/Function

**结构**：  
- 节点：`Repository`、`Directory`（path, name，可多级）、`File`、`Function`、`Class`；再加 Workflow、Issue、PullRequest。  
- 层级边：Repository -CONTAINS→ 根 Directory，Directory -CONTAINS→ 子 Directory 或 File，File -CONTAINS→ Function/Class，Class -CONTAINS→ Function（可选）。

**这样做的理由**：

1. **和仓库的真实结构一一对应**  
   磁盘上就是“目录 → 子目录 → 文件”，图上也有一条从根目录一路 CONTAINS 到某个函数的路径，和“在 IDE 里从项目根点进某个函数”的体验一致。问“src/models 下有哪些函数”就是：找到 Directory(path='src/models')，沿 CONTAINS 到 File 再 to Function，不需要写 path 前缀匹配。

2. **按目录做聚合、统计更自然**  
   “这个目录下有多少函数”“这个目录被多少 PR 改过” = 从该 Directory 出发的 CONTAINS 遍历，在图模型里就是一层关系，而不是“先查出所有 path 以某前缀开头的 File 再聚合”。若后面要做“按目录做 RAG 粗筛”（先选目录再选文件/函数），从 Directory 出发的遍历也很直接。

3. **树形 UI 友好**  
   前端或脚本要做“可展开的目录树”时，每个节点就是图里的 Directory/File，展开 = 查该节点的 CONTAINS 出边，不需要在应用层用 path 拼树、处理重名等。

**代价**：  
- 目录结构要在图里维护：首次全量要建所有 Directory 节点和 CONTAINS；文件/目录移动或重命名时，要更新或新建 Directory 及边，增量逻辑更复杂。  
- 图更大（多一批 Directory 节点和边），但若目录数量相对文件数量不算特别多，通常仍可接受。

---

### 2.4 小结与建议（由你选）

| 维度 | 方案 A（无 Directory，目录由 path 推导） | 方案 B（显式 Directory 层级） |
|------|----------------------------------------|-----------------------------|
| 事实来源 | 只有 path，单一 | path + 图上的目录结构，两处要一致 |
| 增量/移动 | 只更新该 path 下 Function/Class，简单 | 要维护 Directory/File 与 CONTAINS |
| 图规模 | 更小 | 多 Directory 节点与边 |
| “某目录下有哪些…” | 用 path 前缀查 | 从 Directory 遍历，自然 |
| 树形展示 | 应用层用 path 建树 | 图上直接按 CONTAINS 展开 |

- **若你更看重：实现简单、增量省事、图小、少维护一种“结构”** → 选 **方案 A**，原设计就是按这个思路来的，有道理。  
- **若你更看重：和图上的“目录节点”一一对应、按目录遍历/聚合自然、树形 UI 直接绑在图结构上** → 选 **方案 B**，你之前的“函数/类/文件/目录/更大的目录”就是这种。

方案 A 仅保留 **Function、Class**（无 File/Directory）；方案 B 保留 Directory、File、Function、Class。下面 2.5、2.6 给出两种方案各自的节点与关系简表，便于对照。**首版实现为保持工作连贯性采用方案 B（有 File 与 Directory 节点）**，见 `run_stage1.py` 与相关模块。

### 2.5 方案 A 的节点与关系（无 Repository、无 Directory、无 File）

| 节点 | 主要属性 |
|------|----------|
| Function | name, signature, file_path, start_line, end_line |
| Class | name, file_path, start_line, end_line |
| Workflow / Issue / PullRequest | （同前，略） |

关系：Class -CONTAINS→ Function（可选，表示成员函数）；Function -CALLS→ Function；Workflow/Issue/PR 与 Function（及可选 Class）的关联同前。**“在哪个文件/目录”由 Function.file_path、Class.file_path 推导**（如 `WHERE file_path = '...'` 或 `STARTS WITH '...'`）。PR“改了哪些函数”可用 PR 上的路径列表（如 changed_paths）与 file_path 匹配。  
**不建 Repository、Directory、File 节点**：root_path、last_processed_commit 放配置或 Config 节点；图里只保留“函数 + 类 + 边”。

### 2.5.1 是否保留 File 节点？（与“是否要目录节点”同一类问题）

去掉目录节点后，可以再问一层：**代码问答是否必须要有 File 节点？** 理论上也可以再扁平一步：只保留 **Function、Class**，在它们身上带 `file_path`（及 name、signature、start_line 等），“哪个文件有哪些函数”用 `WHERE file_path = '...'` 或 `WHERE file_path STARTS WITH '...'` 即可，不再建 File 节点和 CONTAINS 边。

- **若不要 File 节点**：图更小（少掉“文件数”那么多的节点和 CONTAINS 边）；“某文件下的函数”“某目录下的函数”都靠 Function/Class 的 `file_path` 过滤；PR“改了哪些文件”可在 PR 节点上存 `changed_paths: ['path1','path2']`，查“这个 PR 涉及哪些函数”时用 `fn.file_path IN pr.changed_paths` 或路径前缀匹配。对**只做“函数级”问答**（谁调谁、哪个函数干啥、多跳沿 CALLS）来说，这样已经够用。
- **若保留 File 节点**：多一层“文件”实体，适合这些需求：  
  - **文件级元数据**：例如给“整个文件”做摘要或 embedding，需要有一个节点挂这些信息，否则只能挂在某个 Function 上（不自然）或单独建“虚拟文件”节点。  
  - **PR/Issue 关联更直观**：PR -CHANGED_IN→ File 表示“改动了这个文件”，再沿 CONTAINS 到 Function；若没有 File，就要在 PR 上存路径列表，查询时再和 Function.file_path 做匹配。  
  - **“列出所有文件”“这个文件有多少函数”**：有 File 时是遍历 CONTAINS；没有时用 `DISTINCT Function.file_path` 或按 path 聚合，也能做，只是写法不同。

**建议（已采纳）**：与去掉 Directory 的理由一致，**File 节点也可以不建**。只保留 **Function、Class**（带 file_path、name、signature、行号等）和 CALLS、CONTAINS（Class→Function）等边；PR/Issue 的“改了哪些文件”用路径列表（如 PR.changed_paths）与 Function.file_path 匹配即可。若后续要做文件级摘要/embedding 或希望图上有“文件”实体，再引入 File 节点不迟。**方案 A 采用“无 File 节点”**，见 2.5。

### 2.6 方案 B 的节点与关系（显式 Directory 层级）

| 节点 | 主要属性 |
|------|----------|
| Repository | id, root_path, last_processed_commit |
| Directory | path, name（可多级） |
| File | path, name, language |
| Function | name, signature, file_path, start_line, end_line |
| Class | name, file_path, start_line, end_line |
| Workflow / Issue / PullRequest | （同前，略） |

关系：Repository -CONTAINS→ 根 Directory；Directory -CONTAINS→ 子 Directory 或 File；File -CONTAINS→ Function, Class；Class -CONTAINS→ Function（可选）；Function -CALLS→ Function；PR 可 -CHANGED_IN→ File 或 Directory；其余同前。

版本与增量：**只维护当前最新图**（方式 A）。增量时用 `last_processed_commit` + git diff 得到变更文件，只重解析这些文件并更新图中对应节点与边，再更新 last_processed_commit。

---

## 三、整体 Pipeline 分阶段

```
┌─────────────────────────────────────────────────────────────────────────┐
│  1. 代码图采集（含版本）                                                  │
│  仓库 + compile_commands.json → 解析 (AST) → Function/Class + CALLS       │
│  → 写入 Neo4j，记录 last_processed_commit                                 │
└─────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────┐
│  2. 流程/工作流理解与入库                                                 │
│  入口函数列表 + 调用图展开 → 收集相关函数/路径 → LLM 总结 → Workflow 节点   │
│  + PART_OF_WORKFLOW / WORKFLOW_ENTRY 写入 Neo4j                          │
└─────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────┐
│  3. Issue / PR 采集与关联                                                 │
│  GitHub API 或本地 clone 解析 → Issue、PullRequest 节点                  │
│  → 通过 diff / 正文解析 / embedding 相似度 → REFERENCES、CHANGED_IN、FIXES │
└─────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────┐
│  4. 增量更新（仓库有新提交时）                                             │
│  git diff last_processed_commit..HEAD → 变更文件列表                       │
│  → 仅重解析变更文件 → 更新/删除 Neo4j 中对应节点与边                        │
│  → 可选：仅对受影响入口重新做 workflow 总结；更新 last_processed_commit    │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 四、流程理解的几种做法（可组合）

1. **基于调用图展开（规则）**  
   给定若干“入口函数”（如 main、请求处理入口、关机入口），沿 `CALLS` 边做 BFS/DFS，收集子图；将路径或子图序列化为文本/结构化描述，供 LLM 或直接展示。  
   → 产出：与入口绑定的“调用子图”，可挂到 `Workflow` 节点（WORKFLOW_ENTRY + PART_OF_WORKFLOW）。

2. **LLM 总结流程**  
   对上述子图或对“检索到的相关函数片段”做 prompt，让 LLM 生成自然语言流程描述，存到 `Workflow.summary_text`。  
   → 便于回答“关机流程是什么”“初始化顺序”等自然语言问法。

3. **从 Issue/文档反推流程**  
   从 Issue 标题/正文或文档中识别“流程类问题”，用 embedding 检索相关 Function/File，再沿 CALLS 扩展或交给 LLM 总结，生成 `Workflow` 并可与对应 Issue 关联。

4. **人工标注入口 + 自动展开**  
   维护一个“流程名 → 入口函数”的配置（如 YAML/JSON），跑批时按配置展开并写库；后续仓库变更时只对“受影响入口”重跑。

**建议**：先做 1 + 2（规则展开 + LLM 总结），再按需加 3、4。

---

## 五、PR / Issue 的纳入方式

- **数据来源**  
  - **选项 A**：GitHub/GitLab API（需 token，写在 .env），拉取 Issue/PR 列表与正文、评论、diff。  
  - **选项 B**：本地 clone 后，用 `gh` CLI 或爬取 .git 目录下的信息（若平台支持）；或只解析本地有的 PR 元数据。  
  建议优先 **API + token**，信息最全。

- **与代码的关联**  
  - **PR**：用 API 返回的 `changed_files` 存为 PR 的 `changed_paths`（或类似），查询“该 PR 涉及哪些函数”时用 Function.file_path 与 changed_paths 匹配；若做函数级 diff 可建 PR→Function 的边。  
  - **Issue**：通过正文提到的路径/函数名、embedding 与 Function 摘要的相似度、或“关闭该 Issue 的 PR”所涉函数，建立 `Issue -[:REFERENCES]-> Function`。  
  首版可只做 PR 存 changed_paths 和 Issue 正文的存储，关联可逐步细化。

- **更新策略**  
  - 定期全量同步：如每周拉取一次 Issue/PR 列表，与 Neo4j 中已有节点按 external_id 做 upsert。  
  - 或 webhook：仓库配置 webhook，在 Issue/PR 事件时触发同步脚本（需有公网可访问的端点）。

---

## 六、仓库持续修改时的增量更新

1. **记录进度**  
   在运行配置或 Neo4j 的 `Config`/`ScanState` 等节点中记录 `last_processed_commit`（如 HEAD 的 sha）；不建 Repository 节点。

2. **发现变更**  
   在仓库目录执行 `git diff --name-only <last_processed_commit>..HEAD`（或 `...HEAD` 看 merge 后的变更），得到本次需要处理的**变更/新增**的 .cpp/.c 文件列表；若某文件被删除，则从图中删除该 file_path 下所有 `Function`/`Class` 节点及相关边。

3. **只重算变更部分**  
   - 仅对“变更/新增”的文件，用当前 `compile_commands.json`（需在 build 目录重新 cmake 以反映最新代码）重新跑 AST 解析，得到新的 Function 与 CALLS（仅限本 TU 内的边；跨 TU 的边可继续用已有图或按需重算）。  
   - 在 Neo4j 中：删除或更新该 path 下原有 Function/Class 节点与相关 CALLS，再写入新解析结果；更新 `last_processed_commit` 为当前 HEAD。

4. **流程与 PR/Issue**  
   - **Workflow**：可只对“受影响的入口函数”重新做调用图展开 + LLM 总结，更新对应 `Workflow` 节点。  
   - **PR/Issue**：增量时一般只追加新 PR/新 Issue，或按 external_id 更新状态/正文；通常不需要因为代码图增量而重算全部 PR 与代码的关联，除非要做“影响范围”的精确回溯。

5. **全量 vs 增量**  
   - **首轮**或 **compile_commands 结构大变**（如大量增删 CMake 目标）时，可做一次**全量**解析再写库。  
   - **日常**：用上述增量逻辑，只解析变更文件，减少耗时。

---

## 七、实现顺序建议（与现有 Code_Graph 规划衔接）

1. **阶段 1**：代码图解析（已有规划）  
   实现“读 compile_commands.json + AST 解析 → nodes/edges”，并**写入 Neo4j**：写 **Function、Class**（带 file_path）及 Class -CONTAINS→ Function、**CALLS**（方案 A，无 Repository/Directory/File）；同时写入或更新 `last_processed_commit` 到配置或 Config 节点（实现时怎么方便怎么来）。

2. **阶段 2**：流程理解与入库  
   实现“入口函数配置 + 调用图展开 + 可选 LLM 总结”，创建 `Workflow` 节点及 PART_OF_WORKFLOW、WORKFLOW_ENTRY 关系。

3. **阶段 3**：Issue / PR 采集  
   使用 `.env` 中的 **GITHUB_TOKEN** 调用 GitHub API，拉取 Issue/PR，创建节点；PR 存 `changed_paths`（或类似）与 Function.file_path 匹配以表达“改了哪些函数”，按需建立 REFERENCES（Issue→Function）、FIXES（PR→Issue）。

4. **阶段 4**：增量更新  
   实现 git diff → 变更文件列表 → 仅重解析变更文件 → 更新 Neo4j + 可选 workflow 重算 + 更新 last_processed_commit。

5. **阶段 5**（可选）：embedding 与 RAG  
   对 Function/Class/Workflow 做摘要与 embedding，写入 Neo4j 或外部向量库，供“问题 → 检索 → LLM”使用。

---

## 八、已确认选项（实现按此执行）

- **图模型**：**方案 A**（仅 Function、Class，不建 Repository、Directory、File；文件/目录由 Function/Class.file_path 推导）。图更小、实现与增量更简单，足以支撑“回答问题”（含 SWE-QA 类：声明与调用、实体交互、跨文件/多跳推理）。
- **版本策略**：只维护**当前最新一张图**，不保留历史提交的图。
- **流程理解**：先做“入口函数 + 调用图展开 + LLM 总结”，入口函数列表由配置文件（如 YAML）提供。
- **PR/Issue**：使用 **GitHub API**，token 从 `.env` 的 **GITHUB_TOKEN** 读取。
- **增量更新**：实现上怎么方便怎么来；阶段 1 会写入/更新 `last_processed_commit`，阶段 4 基于此做 git diff 与按文件更新。
- **范围**：单仓，不需多仓库/多分支。

后续按阶段 1 → 2 → 3 → 4 的顺序在 Code_Graph 下实现脚本，并在根目录 `项目编写历史.md` 记录实现内容与输出位置。
