# QA 系统 v2 — 组件化检索与问答 Pipeline

## 架构概览

```
src/qa/
├── models.py              # 统一数据模型
├── trace.py               # 实验追踪记录器
├── expansion.py           # 渐进式代码展开
├── prompts.py             # Prompt 模板管理
├── agent_loop.py          # ReAct 决策循环
├── pipeline.py            # 主 Pipeline 编排
├── runner.py              # 实验运行器
├── retrievers/            # 检索器层（每个组件一个文件）
│   ├── base.py            # 检索器基类
│   ├── grep.py            # Grep 关键词检索
│   ├── embedding.py       # Embedding 语义检索
│   ├── graph.py           # Neo4j 图检索
│   └── issue.py           # Issue/PR 检索
└── tools/                 # 细粒度工具（每个工具一个文件）
    ├── file_reader.py     # 文件/函数读取
    ├── call_chain.py      # 调用链扩展（callers/callees）
    └── class_reader.py    # 类完整实现读取
```

## 设计原则

1. **一组件一文件**：每个检索器/工具独立一个 `.py`，<350行，职责单一
2. **统一接口**：所有检索器继承 `BaseRetriever`，返回 `RetrievalResult`
3. **全程可观测**：`TraceRecorder` 记录每步召回、时延、token
4. **渐进式展开**：签名 → 完整实现 → 完整类/文件，按需加载节省 token

## 快速开始

```python
from src.qa import QAPipeline, QARunner
from src.qa.retrievers import GrepRetriever, EmbeddingRetriever

# 1. 配置检索器
retrievers = [
    GrepRetriever(repo_root="/path/to/llama.cpp"),
    EmbeddingRetriever(),
]

# 2. 创建 Pipeline
pipeline = QAPipeline(
    retrievers=retrievers,
    enable_react=True,
    max_react_steps=5,
)

# 3. 运行单个问题
result = pipeline.run("llama.cpp 中量化的函数有哪些？")
print(result.answer)
print("召回函数:", result.all_function_names)
print("耗时:", result.total_latency_ms, "ms")
print("Token:", result.total_tokens)

# 4. 批量跑 benchmark
runner = QARunner(pipeline, output_dir="./results")
questions = [
    {"question": "...", "id": "q1"},
    {"question": "...", "id": "q2"},
]
results = runner.run_benchmark(questions, workers=5)
```

## Pipeline 流程

```
query
  │
  ▼
┌─────────────────┐
│ Initial Search  │ → 并行调用所有 enabled retrievers
└─────────────────┘
  │
  ▼
┌─────────────────┐     sufficient?
│ ReAct Loop      │ → YES → 跳到生成
│ (决策+执行)      │ → NO  → 继续搜索/扩展
└─────────────────┘
  │
  ▼
┌─────────────────┐
│ Expansion       │ → 高匹配函数展开完整实现
│ (渐进式展开)     │
└─────────────────┘
  │
  ▼
┌─────────────────┐
│ Generate Answer │ → 构建上下文 → LLM → 答案
└─────────────────┘
```

## ReAct Actions

| Action | 说明 |
|--------|------|
| `grep_search` | 用新关键词进行 Grep 搜索 |
| `semantic_search` | 用新查询进行 Embedding 语义搜索 |
| `expand_callers` | 扩展目标函数的调用者（上游） |
| `expand_callees` | 扩展目标函数的被调用者（下游） |
| `expand_same_file` | 扩展同文件中的其他函数 |
| `expand_same_class` | 扩展同类/命名空间中的其他方法 |
| `read_class` | 读取目标函数所在类的完整实现 |
| `read_full_file` | 读取目标函数的完整源文件 |
| `sufficient` | 信息足够，生成答案 |

## 实验记录

`TraceRecorder` 每步记录：
- `phase`: initial_search / react_search / expansion / generate
- `action`: 具体执行的操作
- `query`: 搜索关键字
- `retrieved`: 召回的函数名列表
- `latency_ms`: 本步耗时
- `token_usage`: prompt / completion / total tokens
- `info_gain`: 本步新增的函数数量

最终 `QAResult` 包含完整的 `steps` 列表，便于事后分析：
- 是**召回失败**（没找到函数）还是 **理解错误**（找到了但理解错了）
