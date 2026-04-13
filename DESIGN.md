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

## 二、Neo4j 图模型（实际采用方案 B）

当前目标就是**能回答问题**（可参考 SWE-QA 的问题类型：声明与调用、实体交互、跨文件/多跳推理等）。图里需要**函数 / 类**与**调用关系**，以及和 PR/Issue/Workflow 的关联。**实际采用方案 B**：显式层级结构，包含 Repository、Directory、File、Function、Class 等节点及 CONTAINS 层级边。这种结构便于目录遍历、树形 UI 展示和按目录聚合查询。

> **历史说明**: 设计初期曾考虑方案 A（无 Directory/File 节点，仅通过 file_path 推导），但实际实现为保持工作连贯性采用了方案 B。本章保留方案 A 的对比分析供后续参考，但所有实现均基于方案 B。

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

**实际采用方案 B**，保留 Directory、File、Function、Class 等完整层级。下面 2.5、2.6 给出两种方案各自的节点与关系简表，便于对照。

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

**方案 A 的建议**：与去掉 Directory 的理由一致，**File 节点也可以不建**。只保留 **Function、Class**（带 file_path、name、signature、行号等）和 CALLS、CONTAINS（Class→Function）等边；PR/Issue 的改了哪些文件用路径列表（如 PR.changed_paths）与 Function.file_path 匹配即可。

> **实际实现**: 采用方案 B，保留了 File 节点和完整的层级结构。

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

---

## 九、QA Pipeline 演进（V1 → V5）

### 历史方案

**V1 (A/B/C 硬分类)**：根据问题关键词硬编码路由到不同查询策略
- A类：直接 Cypher 查询
- B类：CALLS 扩展  
- C类：Embedding 搜索
- **问题**：边界模糊，无法组合策略，失败率 65.8%

**V4 (Embedding-RAG)**：废弃 A/B/C，统一使用语义搜索
- 基于函数 embedding 找相关代码
- 扩展调用链（caller/callee）
- **效果**：失败率降至 0.6%，正确率 62.2%

### V5 (Code + Issue 混合检索) - 当前方案

**核心改进**：同时检索代码和 GitHub Issue/PR，解决设计决策类问题

**实现方式（方案2 - 全量混合）**：
1. **并行检索**：
   - 代码：`embedding` 搜索函数（top 5）
   - Issue：`embedding` 搜索 GitHub Issue（top 3）
2. **合并上下文**：代码结果 + Issue 结果 + 调用链
3. **生成答案**：LLM 基于混合上下文回答，标注信息来源

**效果对比**（30道难题）：

| 指标 | V4 (仅代码) | **V5 (代码+Issue)** | 提升 |
|------|-------------|---------------------|------|
| 二元正确率 | ~30% | **93.3%** | ↑ 211% |
| 0-1 平均分 | 0.36 | **0.7333** | ↑ 104% |
| 使用 Issue | 0% | **30%** | 从无到有 |

**典型改进案例**：
- 问题："为什么选择以当前方式划分和设计 http？"
- V4：基于代码推测，分数 0.3
- V5：引用 Issue #19773, #19408 的设计讨论，分数 0.8+

**适用场景**：
- ✅ 设计决策类问题（"为什么"、"为什么选择"）
- ✅ Bug 修复类问题
- ✅ 性能优化类问题
- ✅ 代码实现类问题（代码+Issue 双保险）

---

## 十、后续优化方向

### P0: 混合检索 + RRF（预计 +5-10%）
- BM25 关键词匹配 + Embedding 语义搜索
- Reciprocal Rank Fusion 多路召回融合
- Query Expansion（查询扩展）

### P1: ReAct Agent（预计 +8-15%）
- 多轮迭代：目录 → 文件 → 函数 → 调用链
- 动态工具选择
- Self-correction（检索不足时自动放宽条件）

### P2: Self-Consistency（预计 +5-8%）
- 多次采样投票
- 证据验证（检查答案中的函数名是否真实存在）
- 引用溯源（标注 [1], [2] 来源）

### P3: 模型升级
- gpt-4o → gpt-4 / claude-3.5-sonnet
- 领域微调（用代码-问题对 fine-tune）


---

## 九、QA Pipeline 演进与最终方案（V1 → V5）

### 历史方案对比

| 版本 | 方法 | 二元正确率 | 0-1 平均分 | 失败率 | 核心问题 |
|------|------|-----------|-----------|--------|---------|
| **V1** | A/B/C 硬分类路由 | 36.4% | 0.47 | 65.8% | 边界模糊，无法组合策略 |
| **V2** | 目录驱动 Agent | ~5% | - | 95% | 目录导航效率低 |
| **V3** | 混合检索 (BM25+Embedding+Graph) | ~10% | - | 90% | 实现复杂，RRF 融合效果不佳 |
| **V4** | Embedding-RAG (仅代码) | 62.2% | 0.6726 | 0.6% | 设计决策类问题缺乏依据 |
| **V5** | **Code + Issue 混合检索** | **65.0%** | **0.6937** | **0%** | ✅ 当前最优 |

### V5 最终方案详情

**核心思路**：同时检索代码和 GitHub Issue/PR，解决设计决策类问题

**实现方式（方案2 - 全量混合）**：
1. **并行检索**：
   - 代码：`embedding` 搜索函数（top 5）
   - Issue：`embedding` 搜索 GitHub Issue（top 3）
2. **合并上下文**：代码结果 + Issue 结果 + 调用链
3. **生成答案**：LLM 基于混合上下文回答，标注信息来源

**效果对比**（全量 360 题）：

| 指标 | V4 (仅代码) | **V5 (代码+Issue)** | 提升 |
|------|-------------|---------------------|------|
| 二元正确率 | 62.2% | **65.0%** | ↑ 2.8% |
| 0-1 平均分 | 0.6726 | **0.6937** | ↑ 0.02 |
| 0-1 中位数 | 0.7000 | **0.7000** | - |
| 失败率 | 0.6% | **0%** | ↓ 100% |
| 使用 Issue | 0% | **36.9%** | 从无到有 |

**典型改进案例**：
- 问题："为什么选择以当前方式划分和设计 http？"
- V4：基于代码推测，分数 0.3
- V5：引用 Issue #19773, #19408 的设计讨论，分数 0.8+

**为什么选方案2（全量混合）而非方案1（智能路由）**：
- 方案1需要 LLM 判断问题类型，增加延迟且可能误判
- 方案2简单无脑，并行检索不增加总延迟（代码和 Issue 同时查）
- 实验表明 36.9% 的题目受益于 Issue，全量覆盖更稳妥

---

## 十、后续优化方向（进行中）

### P0: 混合检索 BM25 + Embedding（当前进行）

**现状**：V5 仅用 Embedding 语义搜索，缺少精确匹配

**方案**：
1. **BM25 关键词检索**：对函数名、文件名进行精确匹配
2. **Embedding 语义检索**：对功能描述进行相似度匹配  
3. **Reciprocal Rank Fusion (RRF)**：融合多路召回结果
   - score = Σ 1/(k + rank)
   - k=60（常数，防止低排名项得分过高）
4. **Query Expansion**：把 "blas" 扩展为 "blas sgemm matrix multiplication"

**预期效果**：召回率提升 → 正确率 +5-10%

---

### P1: ReAct Agent + 工具调用（预计 +8-15%）

**现状**：单轮检索 → 生成，缺少迭代

**方案**：
- **多轮推理**：目录 → 文件 → 函数 → 调用链
- **动态工具选择**：
  - 架构问题 → `get_module_overview`
  - 依赖问题 → `get_callers/get_callees`
  - Bug 问题 → `search_issues`
- **Self-Correction**：如果检索结果不足，自动放宽关键词

---

### P2: Self-Consistency + Verification（预计 +5-8%）

**现状**：单一生成答案，可能有幻觉

**方案**：
- **多次采样**：同一个问题生成 3-5 个答案，投票选出最佳
- **证据验证**：检查答案中的函数名是否真实存在于检索结果
- **引用溯源**：要求 LLM 在答案中标注 "[1]", "[2]" 引用来源

---

### P3: 更大模型 + Fine-tuning（预计 +10-15%）

**现状**：使用通用 LLM (gpt-4o)

**方案**：
- **升级模型**：gpt-4o → gpt-4 / claude-3.5-sonnet
- **领域微调**：用 llama.cpp 代码-问题对 fine-tune 一个小模型
- **蒸馏**：用大模型生成高质量答案，蒸馏到本地小模型

---

## 十一、实验记录

### 2026-04-12: V5 全量验证通过

- **配置**：workers=20, 代码 top 5 + Issue top 3
- **结果**：360/360 题完成，二元正确率 65.0%，0-1 平均分 0.6937
- **结论**：V5 作为生产环境默认方案

### 下一实验：混合检索 BM25 + Embedding

- **测试集**：V5 中得分较低的 30 题
- **目标**：验证 RRF 融合是否能提升难题正确率


---

## 十二、实验记录与结论

### 2026-04-12: V5 成为最终方案

| 版本 | 方法 | 二元正确率 | 0-1 平均分 | 结论 |
|------|------|-----------|-----------|------|
| V1 | A/B/C 硬分类 | 36.4% | 0.47 | ❌ 废弃 |
| V4 | Embedding-RAG (仅代码) | 62.2% | 0.6726 | ✅ 基础方案 |
| **V5** | **Code + Issue 混合** | **65.0%** | **0.6937** | ✅ **最终方案** |
| V6 | BM25 + Embedding + RRF | 58.9% | 0.6371 | ❌ 效果下降 |

### V6 混合检索失败分析

**预期**：BM25 精确匹配 + Embedding 语义匹配 → 提升召回率

**实际**：
- 65.3% 题目使用了 BM25
- 但 RRF 融合后整体正确率下降 6.1%

**原因分析**：
1. BM25 基于关键词匹配，容易匹配到不相关的函数名（如问题中有 "hash"，BM25 会匹配所有含 hash 的函数，但实际问题可能问的是特定 hash 实现）
2. RRF 融合时，BM25 的高分噪声项可能排挤了 Embedding 的真正相关结果
3. 代码领域的 BM25 需要更精细的调参（如 IDF 权重、字段权重）

**教训**：
- 不是所有混合检索都能提升效果
- Embedding 单独在代码领域已经表现良好
- Issue 知识库的补充比 BM25 更关键

### 最终推荐架构 (V5)

```
用户问题
    ↓
并行检索
├── 代码: Embedding 语义搜索 (top 5)
└── Issue: Embedding 语义搜索 (top 3)
    ↓
合并上下文（代码 + Issue + 调用链）
    ↓
LLM 生成答案（带引用来源）
```

**关键成功因素**：
1. **Embedding 语义搜索**：准确找到功能相关的函数
2. **Issue 知识库**：提供设计决策和 Bug 修复的真实来源
3. **调用链扩展**：补充函数的上下游依赖关系

---
