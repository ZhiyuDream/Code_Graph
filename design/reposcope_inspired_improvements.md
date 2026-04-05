# RepoScope 启发：Code_Graph 下一阶段改进

> 基于对 RepoScope（RSSG: Repository Structural Semantic Graph）的源码分析，提取对 Code_Graph 有价值的思路。

---

## 0. 实施状态

| 改进项 | 状态 | 实现内容 |
|--------|------|----------|
| **Call Chain DFS** | ✅ 已实现 | 新增 `get_call_chain(depth=2)` tool，支持 2-hop+ 调用链追溯 |
| **Wrapper 2-hop 展开** | ✅ 已实现 | `get_callers` 对 wrapper 函数自动展开其上游（2-hop） |
| **Context Planner（简化版）** | ✅ 已实现 | System prompt 更新，引导 Agent 在调用链问题时主动使用 `get_call_chain` |
| Similar Functions | ⏳ 待做 | 基于 V2 summary 的文本相似度 |
| Token Budget | ⏳ 待做 | code snippet 优先 + annotation 字段按优先级截断 |

---

## 1. RepoScope 核心架构回顾

### 1.1 四视图上下文 prompt

RepoScope 在生成答案前组织四个视图的上下文：

| 视图 | 内容 | Code_Graph 现状 |
|------|------|-----------------|
| **Callers** | 调用该函数的函数列表 | ✅ Neo4j `MATCH (caller)-[:CALLS]->(f)` 已支持 |
| **Call Chains** | 2-hop 及以上的调用链（带权重评分） | ❌ 只有直接 CALLS 边，无链概念 |
| **Similar Functions** | 基于 embedding 的相似函数（top-10） | ❌ 无 |
| **Similar Code Fragments** | 滑动窗口代码片段向量相似度 | ❌ 无 |

### 1.2 调用链 DFS + 评分机制

```python
# RepoScope call_chain.py 核心逻辑
def _dfs_find_call_chains(...):
    # 1. 按 import_weight（是否跨文件）、infile_weight、similarity 评分
    # 2. mask 过滤避免环路
    # 3. 保留 class 继承层级（structure-preserving）
    # 4. 序列化时保留 class 缩进层级
```

**评分公式**：`chain_score = import_weight * infile_weight * similarity`
- `import_weight`: 跨模块调用权重更高（0.3 vs 0.1 同文件）
- `infile_weight`: 同文件上下文更近
- `similarity`: 基于函数名/签名的文本相似度

### 1.3 动态 Token Budget Prompt 组装

```python
# RepoScope build_prompt.py
def _build_call_chains_or_nodes():
    # 第一步：计算各视图 token 消耗
    # 第二步：按权重分配剩余 token budget
    fragments_length = remaining_length * fragments_alloc_weight / total_weight
    # 第三步：两轮精修确保不超 limit
```

**关键配置**：
```python
CALLERS_ALLOC_WEIGHT = 1.0
CALL_CHAINS_ALLOC_WEIGHT = 1.0
SIMILAR_FUNCTIONS_ALLOC_WEIGHT = 0.5
SIMILAR_FRAGMENTS_ALLOC_WEIGHT = 0.5
```

### 1.4 Context Planner（API Chooser）

```python
# RepoScope prompt_template.py
def plan_prompt(query, repo_summary):
    # 让 LLM 决定需要哪些视图
    # api_choose_prompt: 二分类（需要/不需要每个视图）
```

---

## 2. 对 Code_Graph 的改进建议

### 2.1 立即可借鉴（P0-P1）

#### A. Similar Functions 视图（基于 V2 summary embedding）

**现状**：V2 annotation 已有 `summary` 字段，但没有用来做相似函数检索。

**改进**：
1. 用 V2 `summary` 字段做文本相似度（无需额外 embedding）
2. 在 `tool_get_function_detail` 中新增类似函数列表
3. 利用 Neo4j 的 `apoc.text.levenshteinDistance` 或完全自己计算

```python
# 在 tool_get_function_detail 中增加
similar_fns = session.run("""
    MATCH (f:Function)
    WHERE f.id = $id
    WITH f.annotation_json AS target_json
    MATCH (s:Function)
    WHERE s.id <> $id
      AND s.annotation_json IS NOT NULL
      AND s.annotation_schema_version >= 2
    WITH s, target_json,
         apoc.text.similarity(target_json.summary, s.annotation_json.summary) AS sim
    ORDER BY sim DESC
    LIMIT 5
    RETURN s.id, s.name, s.signature, sim
""", id=func_id)
```

#### B. Token Budget 管理（避免 prompt 过长）

**现状**：`agent_qa_ablation.py` 中 `tool_get_function_detail` 没有 token 预算控制，所有 annotation 字段全部拼接。

**改进**：参考 RepoScope 的两轮精修：
1. 第一轮：计算各部分（caller/callee/annotation/v2_fields）的 token 消耗
2. 第二轮：按权重分配，优先保证 code snippet
3. 超限则截断低优先级字段

```python
def assemble_function_context(func_detail, max_tokens=3000):
    # 1. 计算各部分 token
    # 2. code snippet 优先（最高权重）
    # 3. V2 annotation 次之（failure_modes > summary > callers）
    # 4. 剩余给 callers/callees details
```

#### C. Wrapper 函数 2-hop 传播

**现状**：V2 annotation 有 `is_wrapper` 字段，但 caller 不知道 callee 是 wrapper。

**改进**：在调用 `tool_get_function_detail` 返回 annotation 时，递归标注 wrapper callee：
```python
# 如果 callee 是 wrapper，标注其关键下游
if callee.annotation.get("is_wrapper"):
    callee["downstream_hints"] = callee.annotation.get("callee_signatures", [])[:3]
```

### 2.2 中期可借鉴（P1-P2）

#### D. Call Chain DFS（超越直接 CALLS 边）

**现状**：Neo4j 只有直接 CALLS 边，无 2-hop 调用链。

**改进**：用 Neo4j 的图遍历 API：
```cypher
MATCH (f:Function {name: "target_func"})
MATCH path = (caller)-[:CALLS*1..2]->(f)
WHERE not(any(n in nodes(path) where n.name = caller.name))  // 避免环路
WITH path, length(path) AS depth,
     [n IN nodes(path) | n.name][0] AS direct_caller
RETURN direct_caller, depth, relationships(path)
ORDER BY depth DESC
LIMIT 20
```

**问题**：Neo4j 原生遍历性能差，需考虑：
- 预计算热门函数的 2-hop 表
- 只对 wrapper 函数做 2-hop 扩展

#### E. Context Planner（决定需要哪些视图）

**现状**：每次调用 `tool_get_function_detail` 都返回所有视图（annotation + callers + callees）。

**改进**：在检索前加一层 planning：
```python
PLANNING_PROMPT = """
Given the user question about function {func_name}:
{question}

Which of the following context views are relevant?
- Callers (functions that call this): yes/no
- Callees (functions this calls): yes/no
- Similar functions (by summary): yes/no
- Code snippet: yes/no

Return JSON with reasoning.
"""
```

### 2.3 长期可借鉴（P3）

#### F. Similar Code Fragments（滑动窗口 + 向量）

**现状**：Code_Graph 依赖 Neo4j 图结构，无代码片段向量检索。

**改进**：
1. 对每个函数所在文件做滑动窗口（如每 50 行一个 fragment）
2. 用现有 embedding 模型生成向量
3. 存储到向量数据库（或 Neo4j 向量属性）
4. 检索时 top-k 相似片段

**成本**：需要额外的 embedding 计算 + 存储，不适合小团队快速迭代。

---

## 3. 实施优先级矩阵

| 优先级 | 改进项 | 工作量 | Code_Graph 契合度 | 风险 |
|--------|--------|--------|-------------------|------|
| **P0** | Similar Functions（基于 summary） | 低 | 高（复用 V2 annotation） | 低 |
| **P1** | Token Budget 管理 | 中 | 高（直接改善 quality） | 中 |
| **P1** | Wrapper 2-hop 传播 | 低 | 高（复用 V2 is_wrapper） | 低 |
| **P2** | Call Chain DFS | 高 | 中（需预计算 or 限制范围） | 高 |
| **P2** | Context Planner | 中 | 中（需 prompt 调优） | 中 |
| **P3** | Similar Code Fragments | 高 | 低（存储/计算成本大） | 高 |

---

## 4. 快速落地计划（1-2 周可完成）

### Week 1: P0 改进

1. **Similar Functions**
   - 在 `tool_get_function_detail` 增加基于 summary 文本相似度的 top-5 类似函数
   - 使用 `apoc.text.similarity` 或 Python `difflib.SequenceMatcher`

2. **Token Budget**
   - 在 `agent_qa_ablation.py` 的 `tool_get_function_detail` 加 token 计数
   - 实现简单截断逻辑：code snippet > annotation > callers/callees

### Week 2: P1 改进

3. **Wrapper 2-hop 传播**
   - 当发现 callee 是 wrapper 时，自动展开其关键下游
   - 在 annotation 中加入 `downstream_hints` 字段

4. **Context Planning（可选）**
   - 如果 token budget 改善效果不够，加一层 planning prompt

---

## 5. 关键差异：Code_Graph vs RepoScope

| 维度 | Code_Graph | RepoScope |
|------|-------------|-----------|
| 语言 | C++ (llama.cpp) | Python (PyTorch etc.) |
| 解析器 | clangd (C++ AST) | tree-sitter (Python AST) |
| 图数据库 | Neo4j | NetworkX + pickle |
| Embedding | 无 | NLTK lemmatization + WordNet |
| 调用链 | 直接边 | DFS with scoring |
| 相关函数 | 无 | 基于 embedding |
| 上下文组织 | 固定拼接 | Token budget 动态分配 |

**核心差距**：RepoScope 是为 Python 大型项目设计的，代码结构更规整、AST 更易解析。Code_Graph 面对的是 C++ 老项目（llama.cpp），语法复杂、调用图稀疏。

---

## 6. 预期效果

| 改进项 | 当前痛点 | 预期改善 |
|--------|----------|----------|
| Similar Functions | 多跳问题找不到相关函数 | 直接给出 top-5 相似函数，减少盲目搜索 |
| Token Budget | annotation 过长导致 context overflow | 稳定控制在 max_tokens 内 |
| Wrapper 2-hop | 不知道 wrapper 展开后是什么 | annotation 中直接标注下游关键函数 |
| Context Planner | 总是返回完整上下文（浪费） | 按需索取，提升速度 |
| Call Chain DFS | 只能追溯直接调用者 | 2-hop 链追溯，对深度调用链问题有效 |

---

## 7. 参考 RepoScope 关键源码

| 文件 | 核心函数 | 借鉴点 |
|------|----------|--------|
| `call_chain.py` | `_dfs_find_call_chains()` | 链式追溯 + 评分机制 |
| `build_prompt.py` | `_build_call_chains_or_nodes()` | Token budget 分配算法 |
| `prompt_template.py` | `plan_prompt()` / `api_choose_prompt()` | Context planning |
| `node/base_node.py` | `simplify()` | NLTK lemmatization for text |
| `search_code.py` | `_find_top_k_context()` | 代码片段向量相似度 |
