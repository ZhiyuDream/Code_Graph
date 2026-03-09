# llama_cpp_QA 题目分类与检索策略

本文档基于导出的 `llama_cpp_QA.csv` 分析题目结构，并给出「用 Neo4j 代码图回答这些题」的检索与生成策略，供后续实现 QA 流水线时使用。

---

## 一、CSV 结构

| 列名     | 含义           | 示例 |
|----------|----------------|------|
| 一级分类 | 题目层级       | 项目级、模块级 |
| 二级分类 | 细分类别       | Project、Namespace |
| 问题类型 | 问法类型       | what、why、where |
| 意图     | 考察意图       | Architecture exploration、Dependency tracing、Feature Location、… |
| 实体名称 | 问题针对的实体 | llama.cpp、ggml/…/virtgpu-forward-device.cpp、olmo、preset |
| 具体问题 | 自然语言问题   | 什么是 llama.cpp 的整体架构… |
| 答案     | 参考答案（长文本） | … |
| Evidence | 证据文件列表   | src/llama-*.cpp, … |

说明：答案多为长段落，且不少写有「提供的源码中未包含具体函数调用或类关系信息」——即参考答案是在**没有代码图**的前提下生成的。有了 Neo4j 中的 Function、CALLS、file_path 等，可以用图**补充真实调用关系与定位**，再结合 LLM 生成更准确的答案。

---

## 二、题目分类（按「如何用图回答」划分）

按「检索方式 + 是否需 LLM」将题目归纳为以下几类，便于在流水线里做路由。

### 类型 A：图直接可答（Cypher 为主，答案可模板化或短文本）

- **谁调用了谁 / 某函数被谁调用**  
  - 对应意图：Dependency tracing、调用链。  
  - 检索：按实体名或关键词在图中匹配 Function（name/signature/file_path），用 CALLS 入边/出边查调用关系。  
  - Cypher 示例：`MATCH (caller)-[:CALLS]->(callee) WHERE callee.name CONTAINS $name RETURN caller.name, callee.name`。

- **某功能/某模块在哪些文件、哪些函数里**  
  - 对应意图：Feature Location、Concept / Definition。  
  - 检索：按关键词或实体名匹配 Function.file_path、Function.name，或按路径前缀（如 `src/llama-`）聚合。  
  - Cypher 示例：`MATCH (f:Function) WHERE f.file_path STARTS WITH $path_prefix RETURN f.name, f.file_path`。

- **变量在哪里被引用**（若图中有 Variable + REFERENCES_VAR）  
  - 对应意图：Data / Control-flow。  
  - 检索：见 `docs/变量图验证示例.md`，按 Variable.id/name + file_path 查 REFERENCES_VAR 与 Function。

### 类型 B：图提供子图/路径，需 LLM 汇总（Cypher + Workflow + LLM）

- **模块/子系统之间的依赖关系、调用路径**  
  - 对应意图：Architecture exploration、Dependency tracing。  
  - 检索：从入口或实体对应函数出发，沿 CALLS 做有限步 BFS/DFS，得到子图或路径；或利用阶段 2 的 Workflow（PART_OF_WORKFLOW、WORKFLOW_ENTRY）取已展开的流程。  
  - 生成：将子图序列化为「函数名 + 文件 + 调用关系」文本，送 LLM 总结为「依赖关系 / 依赖图 / 传递方式」等自然语言。

- **某流程/某功能的执行顺序、主要调用链**  
  - 对应意图：流程类、Feature Location 中「主要调用链」。  
  - 检索：同类型 B 上一段；或从流程起点（CALLS 入度 0 且出度≥1）出发沿 CALLS 展开。  
  - 生成：路径/子图 → 文本描述 → LLM 生成「执行顺序 / 关键步骤」。

### 类型 C：开放意图，需语义检索 + 图扩展 + LLM

- **为什么这样设计、有何考量、对可维护性的影响**（why / Design rationale、Purpose Exploration）  
  - 图难以直接答「为什么」，但可先定位相关代码：用意图或问题做 **embedding 检索**（Function/Workflow 摘要或 name+file_path 文本），再在图上看这些函数的 CALLS 邻域或所在 Workflow。  
  - 生成：检索到的函数列表 + 调用关系片段 + 问题 → LLM 生成设计理由、影响分析。

- **整体架构、主要模块及关系**（what / Architecture exploration）  
  - 检索：可按 file_path 前缀聚合（如 `src/llama-*.cpp` 对应模块），再查这些文件内函数的 CALLS 关系；或先用 embedding 检索「架构/模块」相关 Function/File，再在图上看关系。  
  - 生成：模块列表 + 跨模块 CALLS 子图 → LLM 总结为架构描述。

- **性能、技术栈、构建方式等**（why / Performance、Purpose Exploration）  
  - 图主要提供「谁调谁、在哪」；若要做「为什么这样选、有何影响」，需结合文档或 Issue（阶段 3 的 PR/Issue）与 LLM。

---

## 三、检索策略与数据来源对照

| 题目特征（意图/问题类型）     | 主要检索方式                     | 图数据来源              | 答案生成           |
|------------------------------|----------------------------------|-------------------------|--------------------|
| 调用关系、谁调谁、调用链     | 实体名/关键词 → Function → CALLS | Function, CALLS         | 模板或短句         |
| 功能/代码位置、在哪个文件   | file_path / name 匹配或前缀      | Function.file_path, name| 列表或模板          |
| 变量引用位置                 | Variable + REFERENCES_VAR       | Variable, REFERENCES_VAR| 见变量图验证示例    |
| 模块依赖、架构、流程顺序     | CALLS 展开 / Workflow 子图       | CALLS, Workflow         | LLM 总结           |
| 设计理由、为什么、影响       | embedding 检索 + 图邻域          | Function, CALLS, 可选 Issue/PR | LLM 总结           |

---

## 四、流水线建议（阶段 5 对接 QA）

1. **读题**  
   从 `llama_cpp_QA.csv` 读入每一行，得到：一级分类、二级分类、问题类型、意图、实体名称、具体问题。

2. **路由**  
   - 若意图或关键词落在「调用/依赖/调用链」→ 类型 A 或 B（Cypher + 可选 Workflow）。  
   - 若意图为「在哪里/哪个文件/哪个函数」且实体明确 → 类型 A（Cypher）。  
   - 若意图为「为什么/设计/架构/流程概括」→ 类型 B 或 C（图展开 + LLM，或 embedding + LLM）。  
   路由可先按「意图」列或关键词规则实现；后续可加分类模型或 embedding 相似度。

3. **检索**  
   - 类型 A：根据实体名称或从问题中抽取的函数名/文件路径，执行对应 Cypher，返回节点/边。  
   - 类型 B：从实体对应 Function 或入口集合出发，CALLS 展开或查 Workflow，得到子图/路径并序列化。  
   - 类型 C：对问题或意图做 embedding，检索相关 Function/Workflow，再在图上看 CALLS 邻域；可选查 PR/Issue。

4. **生成答案**  
   - 类型 A：检索结果格式化为「函数名、文件、调用关系」等，直接作为答案或填模板。  
   - 类型 B/C：检索结果（文本化）+ 问题 → 调用 LLM 生成自然语言答案；可选与参考答案对比做评估。

5. **输出与评估**  
   - 输出：每题一行或一文件，包含「问题 id、问题、检索到的证据、生成答案」。  
   - 若有参考答案与 Evidence，可做简单重叠度或 LLM-based 一致性评估（后续再细化）。

---

## 五、实体名称与图的对应关系

- **实体为项目名**（如 `llama.cpp`）：可视为「全图」或按 `file_path` 前缀（如 `src/`、`ggml/`）拆模块后再查 CALLS。  
- **实体为文件路径**（如 `ggml/src/ggml-virtgpu/virtgpu-forward-device.cpp`）：`MATCH (f:Function) WHERE f.file_path CONTAINS $entity RETURN f`，再对这些函数查 CALLS 入边/出边。  
- **实体为模块/命名空间名**（如 `olmo`、`preset`）：可用 `file_path CONTAINS entity` 或 `name CONTAINS entity` 匹配 Function，再查调用关系或 Workflow。

实现时需统一「实体名称」与 Neo4j 中 `file_path` 的格式（如是否带仓库根、是否用相对路径），必要时做简单归一化。

---

## 六、后续可实现的脚本

- **`run_qa.py`**（或类似）：  
  - 读 `llama_cpp_QA.csv`；  
  - 按「意图」或规则路由到类型 A/B/C；  
  - 调用 Neo4j（Cypher）与可选 Workflow/embedding 检索；  
  - 输出「检索结果 + 生成答案」到 CSV 或 JSON，便于与参考答案对比。  

- **先实现类型 A**（调用关系、功能位置、变量引用）：只做 Cypher，不做 LLM，验证「图能答对多少」；再逐步加类型 B、C 与 LLM。

---

*文档基于当前 `llama_cpp_QA.csv` 样本归纳；若 CSV 后续增删列或题目类型，可更新本表与路由规则。*
