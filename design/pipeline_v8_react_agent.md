# Pipeline V8: ReAct Agent（当前最新版）

> 一个问题进来之后，每一步走哪条路。

## 总览

```
用户问题
  │
  ├──→ 阶段 1: 初始检索（纯工具调用，无模型）
  │      │
  │      ├──→ Embedding 检索 top_k=20
  │      │      │
  │      │      ├──→ 召回 >= 5 个函数 → 跳过 file expansion
  │      │      └──→ 召回 < 5 个函数 → 选择性 file expansion（top-3 文件，每文件最多 3 个）
  │      │
  │      ├──→ 检查最高相似度
  │      │      ├──→ >= 0.5 → 不触发 fallback
  │      │      └──→ < 0.5 → fallback（entity extraction + module search + grep）
  │      │
  │      └──→ Issue 检索 top_k=3
  │
  ├──→ 阶段 2: ReAct 循环（DeepSeek 决策，最多 5 步）
  │      │
  │      ├──→ 构建 prompt（当前函数/Issue/调用链）
  │      ├──→ DeepSeek 返回 JSON 决策
  │      │      ├──→ JSON 解析失败 → fallback 到 sufficient
  │      │      ├──→ step >= 4 → 强制 sufficient
  │      │      ├──→ sufficient=True → 停止循环
  │      │      └──→ action = expand_callers/callees/same_file/same_class → 执行扩展
  │      │
  │      ├──→ 验证 target（不在前8个函数中 → fallback 选第一个未扩展的）
  │      ├──→ 验证 action（无效 → fallback 到 expand_callees）
  │      ├──→ 执行扩展 → 新函数加入结果（去重）
  │      └──→ 检查信息增益（连续2步新增 <=1 → early_stop）
  │
  ├──→ 阶段 3: 答案生成（DeepSeek，max_tokens=8192）
  │      │
  │      ├──→ build_context()：按来源分类构建 prompt
  │      └──→ generate_answer()：调用 DeepSeek 生成答案
  │
  └──→ 阶段 4: 评判（gpt-4.1-mini，独立运行）
         │
         └──→ BINARY_JUDGE_PROMPT → CORRECT / INCORRECT + 理由
```

---

## 阶段 1: 初始检索（`initial_search()`）

### 1.1 Embedding 检索
```python
funcs = search_functions_by_text(question, top_k=20)
```
- 用 question 的 embedding 在 Neo4j 中检索最相似的函数节点
- 返回最多 20 个函数，每个函数有 name, file, score

### 1.2 Fallback（低相似度时触发）

**分支条件**：`max_score < 0.5`

```
max_score >= 0.5
    └──→ 不触发 fallback，跳过

max_score < 0.5
    └──→ 触发 fallback
           ├──→ extract_entities_from_question(question)
           │      └──→ 提取命名实体（如 "ggml-blas", "apertus", "falcon"）
           │
           └──→ 对每个实体（最多2个）：
                  ├──→ 如果是 module 名（含'-'或全小写）
                  │      └──→ search_module_functions(entity, limit=5)
                  │             └──→ 在 Neo4j 中按模块名搜索函数
                  │
                  └──→ grep_codebase(entity, limit=3)
                         └──→ 在代码库中全文搜索实体名
                                └──→ convert_grep_to_function_results()
                                       └──→ 将 grep 结果转换为函数节点
```

### 1.3 选择性 File Expansion

**分支条件**：`len(funcs) < 5`

```
len(funcs) >= 5
    └──→ 不做 file expansion，保持上下文干净

len(funcs) < 5
    └──→ 选择性 file expansion
           ├──→ 取 top-3 文件（按函数相似度分数排序）
           └──→ 每文件最多 3 个函数（从 RAG 索引中按行号邻近选取）
```

> **当前状态**：top_k=20 后，初始函数数平均 24.6 个，file expansion **从未触发**。

### 1.4 Issue 检索
```python
issues = search_issues(question, top_k=3)
```
- 检索与问题相关的 GitHub Issue/PR
- 每个 Issue 包含 number, title, body

### 阶段 1 输出
```python
collected = {
    "functions": funcs,       # 初始检索到的函数列表
    "issues": issues,         # 相关 Issue 列表
    "steps": [{"step": 1, "action": "initial_search", "found": len(funcs)}],
    "call_chains": [],        # 调用链扩展记录
}
```

---

## 阶段 2: ReAct 循环（`react_search()`）

### 循环结构
```python
for step in range(2, MAX_STEPS + 1):  # step = 2, 3, 4, 5
    decision = react_decide(question, collected, step)
    if decision.sufficient:
        break
    execute_action(decision.action, decision.target)
    check_info_gain()
```

### 2.1 ReAct 决策（`react_decide()`）

#### 构建 Prompt
从 `prompts/react_decide.txt` 加载模板，替换变量：
- `{question}` — 用户问题
- `{function_count}` — 已收集函数数
- `{function_list}` — 前8个函数的 name/file/score/source
- `{issue_count}` — Issue 数量
- `{issue_list}` — Issue 的 number/title
- `{chain_count}` — 已扩展调用链数
- `{chain_list}` — 调用链的 from/direction/found/new
- `{actions}` — 所有可用 action 的说明
- `{action_choices}` — action 名称列表（如 "expand_callers|expand_callees|..."）

#### 调用 DeepSeek
```python
result = call_llm_json(
    messages=[{"role": "user", "content": prompt}],
    max_tokens=300,   # 实际自动调整为 1200（DeepSeek）
    model='deepseek-v4-pro',
    provider='deepseek'
)
```

**关键技术**：
- `response_format={"type": "json_object"}` — 强制 API 返回 JSON
- `json_repair` — 4 层兜底修复（代码块提取→标准解析→repair_json→正则提取）

#### 决策解析的分支

```
result is None（JSON 解析失败）
    └──→ fallback: {"sufficient": True, "action": "sufficient", "target": ""}

step >= 4（达到最大步数）
    └──→ 强制 sufficient: {"sufficient": True, "action": "sufficient", "target": ""}

result.sufficient == True or result.action == "sufficient"
    └──→ 停止 ReAct 循环，进入答案生成

result.action in ["expand_callers", "expand_callees", "expand_same_file", "expand_same_class"]
    └──→ 继续扩展
           ├──→ 验证 target
           │      ├──→ target 在前8个函数中 → 使用 target
           │      └──→ target 不在列表中 → fallback 选第一个未扩展的函数
           │
           └──→ 验证 action
                  ├──→ action 在有效列表中 → 使用 action
                  └──→ action 无效 → fallback 到 "expand_callees"
```

### 2.2 执行扩展（`execute_action()`）

```
action == "expand_callers"
    └──→ expand_call_chain(target, "callers")
           └──→ Neo4j: MATCH (caller)-[:CALLS]->(callee {name: target})
           └──→ 返回最多 5 个调用者函数

action == "expand_callees"
    └──→ expand_call_chain(target, "callees")
           └──→ Neo4j: MATCH (caller {name: target})-[:CALLS]->(callee)
           └──→ 返回最多 5 个被调用者函数

action == "expand_same_file"
    └──→ expand_same_file(target)
           └──→ Neo4j: MATCH (f) WHERE f.file_path = target_file AND f.name <> target
           └──→ 按行号距离排序，返回最多 10 个同文件函数

action == "expand_same_class"
    └──→ expand_same_class(target)
           └──→ 从函数名推断 class（如 "Class::method" → class="Class"）
           └──→ Neo4j: MATCH (f) WHERE f.name STARTS WITH "Class::"
           └──→ 返回最多 10 个同类方法
```

### 2.3 新函数加入与去重
```python
new_count = 0
for fn in expansion["functions"]:
    if not any(f['name'] == fn['name'] for f in collected["functions"]):
        fn['score'] = 0.5
        fn['source'] = f'{action}_of_{target}'
        collected["functions"].append(fn)
        new_count += 1
```

### 2.4 信息增益检查

```
step >= 3 且 连续2步的新增函数数 <= 1
    └──→ early_stop: "info gain too low"

否则
    └──→ 继续下一步
```

### 阶段 2 输出
```python
collected = {
    "functions": [...],       # 扩展后的函数列表
    "issues": [...],          # Issue 列表（不变）
    "steps": [...],           # 每步的 action/target/found/new
    "call_chains": [...],     # 调用链记录
}
```

---

## 阶段 3: 答案生成（`generate_answer()`）

### 3.1 构建上下文（`build_context()`）

按来源分类，优先级从高到低：

```python
# 1. 高相关函数（Embedding 检索，score > 0.5）
high_rel = [f for f in funcs if f.get('score', 0) > 0.5]
→ 最多 5 个，代码片段 150 字

# 2. Grep fallback 函数
grep_funcs = [f for f in funcs if f.get('source') == 'grep_fallback']
→ 最多 3 个，代码片段 150 字

# 3. 文件级扩展函数
file_exp_funcs = [f for f in funcs if f.get('source') == 'file_expansion']
→ 最多 10 个，代码片段 500 字

# 4. 调用链扩展函数
chain_funcs = [f for f in funcs if 'caller' in f.get('source', '') or 'callee' in f.get('source', '')]
→ 最多 5 个

# 5. Issue 信息
issues = collected.get("issues", [])
→ 最多 3 个，正文 250 字

# 6. ReAct 探索过程
steps = collected.get("steps", [])
→ 最多 3 步的 summary
```

### 3.2 生成 Prompt

从 `prompts/answer_generation.txt` 加载模板，替换：
- `{context}` — build_context() 的输出
- `{question}` — 用户问题

### 3.3 调用 DeepSeek
```python
call_llm(
    messages=[{"role": "user", "content": prompt}],
    max_tokens=8192,
    model='deepseek-v4-pro',
    provider='deepseek'
)
```

### 阶段 3 输出
- 答案文本（平均 2524 字，中位数 2526 字，最长 6475 字）

---

## 阶段 4: 评判（`eval_with_model.py`，独立运行）

### 4.1 加载 Judge Prompt
从 `prompts/judge_binary.txt` 加载模板，替换：
- `{question}` — 问题前 500 字
- `{reference}` — 参考答案前 800 字
- `{generated}` — 生成答案前 1500 字

### 4.2 调用 gpt-4.1-mini
```python
client.chat.completions.create(
    model='gpt-4.1-mini',
    messages=[{"role": "user", "content": prompt}],
    max_tokens=300,  # 或 max_completion_tokens=300
)
```

### 4.3 解析结果
```
text 首行包含 "CORRECT" 且不含 "INCORRECT"
    └──→ eval_binary_correct = True

否则
    └──→ eval_binary_correct = False

eval_binary_reason = 第二行起的所有文本
```

### 阶段 4 输出
```python
{
    "eval_binary_correct": bool,
    "eval_binary_reason": str,
}
```

---

## 轨迹记录

每题保存完整的检索轨迹：

```json
{
  "index": 0,
  "question": "...",
  "检索轨迹": {
    "initial": {
      "phase": "initial_search",
      "embedding_search": [{"name": "...", "file": "...", "score": 0.35}],
      "fallback_triggered": false,
      "file_expansion": {"triggered": false},
      "issues": [{"number": 123, "title": "..."}]
    },
    "react_steps": [
      {
        "step": 2,
        "decision": {
          "prompt": "...",
          "raw_response": {"thought": "...", "sufficient": false, "action": "expand_callees", "target": "..."}
        },
        "expansion": {
          "action": "expand_callees",
          "target": "llm_build_apertus",
          "found": 5,
          "new": 5
        }
      }
    ],
    "final_stats": {
      "function_count": 29,
      "issue_count": 3,
      "step_count": 3
    }
  }
}
```

---

## 关键参数

| 参数 | 值 | 说明 |
|------|-----|------|
| `top_k` | 20 | Embedding 初始检索数量 |
| `FALLBACK_THRESHOLD` | 0.5 | 触发 fallback 的相似度阈值 |
| `MAX_STEPS` | 5 | ReAct 最大步数 |
| `max_tokens` | 8192 | 答案生成最大 token |
| `file_expansion_max_files` | 3 | 选择性 file expansion 的文件数 |
| `file_expansion_per_file` | 3 | 每文件最大扩展函数数 |
| `max_workers` | 20 | 并行处理题目数 |
| `judge_model` | gpt-4.1-mini | 统一评判模型 |
| `generate_model` | deepseek-v4-pro | 答案生成模型 |
