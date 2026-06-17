# 代码图构建 Pipeline 审计文档

> 文档版本：2025-05-31
> 适用代码库：llama.cpp（可泛化到其他 C/C++ 项目）
> 核心目标：将源码通过 clangd LSP 解析为 Neo4j 属性图，支撑跨文件、跨模块的代码问答。

---

## 1. 整体架构

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         Ingestion Pipeline                               │
├─────────────────────────────────────────────────────────────────────────┤
│  输入：compile_commands.json + 源码目录                                   │
│       ↓                                                                  │
│  LSP 层：clangd 20+（子进程，JSON-RPC）                                  │
│       ↓                                                                  │
│  解析层：symbol_extractor → field_resolver → call_resolver               │
│       ↓                                                                  │
│  语义层：control_flow_extractor → param_flow_extractor                   │
│           → resource_lifecycle_extractor → fallback_extractor            │
│       ↓                                                                  │
│  组装层：graph_builder（去重 → 实体 → 目录 → 控制流 → 参数流 → 资源生命周期） │
│       ↓                                                                  │
│  存储层：neo4j_writer（UNWIND 批量写入）                                  │
│       ↓                                                                  │
│  输出：Neo4j 图数据库（~12k 节点 / ~29k 边，common/64 文件基准）          │
└─────────────────────────────────────────────────────────────────────────┘
```

### 1.1 关键文件清单

| 文件 | 职责 |
|------|------|
| `src/ingestion/orchestrator.py` | Pipeline 编排，9 阶段顺序执行 |
| `src/ingestion/symbol_extractor.py` | clangd LSP 交互，提取符号、调用、变量引用 |
| `src/ingestion/call_resolver.py` | 调用关系消歧（重载匹配、模糊匹配、外部调用识别） |
| `src/ingestion/field_resolver.py` | 字段归属修正（成员变量 → Class） |
| `src/ingestion/graph_builder.py` | 图结构组装（5 阶段），含 Louvain 社区发现 |
| `src/ingestion/neo4j_writer.py` | UNWIND 批量写入 Neo4j |
| `src/ingestion/control_flow_extractor.py` | 控制流块提取（if/for/while/try 等） |
| `src/ingestion/param_flow_extractor.py` | 函数参数使用模式提取 |
| `src/ingestion/resource_lifecycle_extractor.py` | 资源生命周期（new/delete/malloc/free/RAII/throw） |
| `src/ingestion/fallback_extractor.py` | 正则回退提取（宏调用、函数调用兜底） |
| `src/ingestion/incremental.py` | 增量更新（按文件删除 + 重新摄取） |
| `scripts/ingestion/ingest_code.py` | 全量摄取入口 |
| `scripts/ingestion/ingest_subset.py` | 子集摄取入口（如只跑 common/） |
| `scripts/ingestion/ingest_code_incremental.py` | 增量摄取入口 |

---

## 2. Pipeline 阶段详解

`orchestrator.run_full_pipeline()` 按以下顺序执行：

### Stage 1: 启动 clangd
- 启动 `clangd` 子进程，通过 JSON-RPC 通信
- 使用 `compile_commands.json` 指导索引
- 等待索引就绪（`index_waiter.wait_for_index()`）：对样例文件轮询 `textDocument/documentSymbol`，最多等待 120s

### Stage 2: 收集源文件
- `source_collector.collect_source_files()` 从 `compile_commands.json` 中提取所有 `.c/.cpp/.h/.hpp` 文件
- 支持 `include_dirs` 过滤（如只处理 `common/` 目录）

### Stage 3: 逐文件符号提取（最耗时）
- `symbol_extractor.process_file()` 对每个文件执行 LSP 请求：
  1. `textDocument/documentSymbol` — 提取 Function / Class / Variable
  2. `textDocument/prepareCallHierarchy` + `callHierarchy/outgoingCalls` — 提取调用关系
  3. `textDocument/references` — 提取变量引用（两阶段：先函数内，再全局）
  4. 正则回退 — 提取宏调用、遗漏的函数调用
- **性能优化**：`delay_between_calls=0`（原为 0.02s），单文件节省 ~1.3s，全库节省 ~12 分钟
- **头文件处理**：已移除 `not is_header` 限制，头文件也提取 outgoingCalls、宏调用、变量引用

### Stage 4: incomingCalls 补充（lambda/回调捕获）
- 对 outgoingCalls 为 0 的函数执行 `callHierarchy/incomingCalls`
- 按文件批量 didOpen，减少 LSP 通信开销
- 与 outgoingCalls 去重合并

### Stage 5: 字段归属解析
- `field_resolver.enrich_file_results()` 修正 member 字段的 parent_class
- 处理 `kind=8 (Field)` 正确归类为 member

### Stage 6: 调用关系解析
- `call_resolver.resolve_all_calls()`：
  - **精确匹配**：`callee_detail`（签名）匹配重载函数
  - **全局匹配**：同名函数跨文件匹配
  - **模糊匹配**：多个候选 → `CALLS_AMBIGUOUS` 边
  - **外部调用**：无法解析的 → `EXTERNAL_CALLS` 边（创建 `ExternalCall` 占位节点）

### Stage 7: 图组装（graph_builder.assemble_graph）
见第 4 节。

### Stage 8: Neo4j 写入
- `ensure_constraints()` — 创建唯一性约束（12 种节点标签）
- `clear_code_graph()` — 若 `clear_existing=True`，删除旧图（保留 Issue/PR 节点）
- `write_graph()` — UNWIND 批量写入，每批 500 条

### Stage 9: 统计与返回
- 返回 `stats` dict，含 files/functions/classes/variables/calls/ambiguous/unresolved/external/control_flow/resource_ops/elapsed

---

## 3. 数据模型

### 3.1 节点类型（12 种）

| 标签 | 说明 | 关键属性 |
|------|------|----------|
| `Repository` | 仓库根节点 | `id`, `root_path`, `last_processed_commit` |
| `Directory` | 目录 | `id`, `path`, `name` |
| `File` | 源文件 | `id`, `path`, `name`, `language` |
| `Function` | 函数/方法 | `id`, `name`, `signature`, `file_path`, `start_line`, `end_line`, `parent_class`, `param_usage_json` |
| `Class` | 类/结构体 | `id`, `name`, `file_path`, `start_line`, `end_line` |
| `Variable` | 变量（参数/成员/局部/全局） | `id`, `name`, `file_path`, `start_line`, `kind` |
| `Attribute` | 类成员变量（Variable 的子集，冗余提升查询效率） | `id`, `name`, `file_path`, `start_line`, `member_of_class` |
| `Module` | Louvain 社区发现的模块 | `id`, `name`, `function_count` |
| `ControlFlowBlock` | 控制流块 | `id`, `type`, `condition`, `file_path`, `line`, `is_error_path`, `semantic_type`, `multi_line`, `full_condition` |
| `ResourceOperation` | 资源操作 | `id`, `type`, `resource_type`, `file_path`, `line`, `variable_name`, `paired_operation_id` |
| `ExternalCall` | 外部调用占位 | `id`, `name`, `kind` |
| `AmbiguousCall` | 歧义调用占位 | `id`, `name`, `kind`, `candidates` |

### 3.2 边类型（11 种）

| 类型 | 方向 | 说明 |
|------|------|------|
| `CONTAINS` | 父 → 子 | Repository→Directory→File→Function/Class/Variable |
| `HAS_METHOD` | Class → Function | 类包含方法（支持跨文件匹配 .cpp ↔ .h） |
| `HAS_MEMBER` | Class → Attribute | 类包含成员变量 |
| `BELONGS_TO` | Function → Module | 函数归属模块（Louvain） |
| `CALLS` | Function → Function | 精确调用关系 |
| `CALLS_AMBIGUOUS` | Function → AmbiguousCall | 歧义调用（多候选） |
| `EXTERNAL_CALLS` | Function → ExternalCall | 外部库/系统调用 |
| `MODULE_CALLS` | Module → Module | 模块间调用 |
| `REFERENCES_VAR` | Function → Variable | 函数内引用变量（含行号列表） |
| `CONTROL_FLOW` | Function → ControlFlowBlock | 函数包含控制流块 |
| `MANAGES` | Function → ResourceOperation | 函数管理资源操作；ResourceOperation → ResourceOperation（配对，如 new→delete） |

---

## 4. 图组装 5 阶段（graph_builder）

```
P0: 去重 ──→ 同名同文件函数合并（保留定义，丢弃声明）
    ↓
P1: 实体收集 ──→ Function / Class / Variable / Attribute 节点 + CONTAINS / HAS_METHOD / HAS_MEMBER / REFERENCES_VAR 边
    ↓
P2: 模块发现 ──→ Louvain 社区发现 → Module 节点 + BELONGS_TO + MODULE_CALLS 边
    ↓
P3: 控制流 ──→ ControlFlowBlock 节点 + CONTROL_FLOW 边
    ↓
P4: 参数流 ──→ Function.param_usage_json（JSON 字符串，非嵌套 Map）
    ↓
P5: 资源生命周期 ──→ ResourceOperation 节点 + MANAGES 边（含配对关系）
```

### 4.1 P0 去重策略
- 同一文件中同名同起止行的函数视为重复
- 保留 `is_definition=True` 的，丢弃前向声明/头文件声明
- 生成 `id_remap`，下游所有引用统一应用

### 4.2 P2 Louvain 社区发现
- 基于 CALLS 边（权重 1.0）+ 文件共现（权重 0.5）构建无向图
- `resolution=0.3` 产生大社区
- 小社区（< 10 函数）合并到最近的大社区
- 为每个社区创建 Module 节点

---

## 5. 语义 Extractor 详解

### 5.1 ControlFlowBlock（控制流提取）

| 属性 | 说明 |
|------|------|
| `type` | `if` / `for` / `while` / `switch` / `try` / `catch` |
| `semantic_type` | `parameter_check` / `resource_check` / `state_validation` / `error_guard` / `""` |
| `multi_line` | 条件是否跨多行 |
| `full_condition` | 完整条件表达式（含跨行） |
| `is_error_path` | 是否为错误处理分支 |

- **跨行条件匹配**：基于括号深度计数器 `_collect_multi_line_condition()`
- **语义推断**：`_infer_semantic_type()` 通过关键词匹配（如 `NULL`, `nullptr`, `err`, `valid` 等）

### 5.2 ParamFlow（参数流提取）

`Function.param_usage_json` 存储 JSON 数组，每个元素：
```json
{"param": "x", "operations": ["field_read", "pass_to"], "lines": [42, 45]}
```

检测的操作类型：
- `field_read` — 读取参数字段（`param->field`）
- `field_assign` — 赋值给参数字段
- `pass_to` — 作为实参传递给其他函数
- `return` — 直接返回参数
- `assign_to` — 赋值给参数
- `deref` — 解引用参数

### 5.3 ResourceOperation（资源生命周期）

| type | 资源类型示例 | 配对 |
|------|-------------|------|
| `allocate` | `new`, `malloc`, `calloc` | → `release` |
| `release` | `delete`, `free` | ← `allocate` |
| `raii_guard` | `std::lock_guard`, `std::unique_ptr` | 无 |
| `throw` | `throw` 语句 | 无 |

- 配对规则：同函数内、同变量名、allocate 在 release 之前
- 配对后生成 `ResourceOperation → ResourceOperation` 的 `MANAGES` 边（`relation: paired`）

---

## 6. 调用解析策略（call_resolver）

```
输入：所有 Function + 所有 RawCall
    ↓
1. 建立全局函数索引（name → list[Function]）
    ↓
2. 对每个 RawCall，按优先级匹配：
   a) callee_detail 签名匹配（精确消歧重载）
   b) 同名 + 同文件优先
   c) 跨文件全局匹配（.cpp → .h 对应）
   d) 多个候选 → AMBIGUOUS
   e) 无候选 → EXTERNAL（如 std::vector::push_back）
    ↓
输出：ResolvedCalls(calls, ambiguous, unresolved, external_calls)
```

---

## 7. Neo4j 写入策略

### 7.1 批量写入
- 节点：按标签分组，每批 500 条，`UNWIND $batch AS node MERGE (n:Label {id: node.id}) SET n += node`
- 边：按关系类型分组，**内部再按 (from_label, to_label) 分组**，以命中索引加速 MATCH

### 7.2 约束
- 所有 12 种节点标签都有 `CREATE CONSTRAINT IF NOT EXISTS FOR (n:Label) REQUIRE n.id IS UNIQUE`

### 7.3 清空策略
- `clear_code_graph()` 按标签逐个 `MATCH (n:Label) DETACH DELETE n`
- **不删除** Issue、PullRequest、Repository（非代码图节点）

---

## 8. 增量更新

`incremental.delete_file_nodes(file_path, driver, database)`：

1. 删除该文件下的所有 Function / Class / Variable / Attribute 节点
2. 删除 `ControlFlowBlock`、`ResourceOperation`、`ExternalCall`、`AmbiguousCall` 节点
3. 清理孤儿边：`BELONGS_TO`、`HAS_METHOD`、`EXTERNAL_CALLS`
4. 调用方重新运行 `process_file()` + `graph_builder` + `neo4j_writer`

---

## 9. 性能基准

### common/ 64 文件子集

| 指标 | 基线 (delay=0.02s) | 优化后 (delay=0) |
|------|-------------------|-----------------|
| 总时间 | 240s | **180s** |
| Symbol extraction | 123.7s | 63.8s |
| Incoming calls | 107.4s | 107.4s |
| 节点数 | 12,464 | 12,464 |
| 边数 | 28,777 | 28,777 |

### 全库估算（~740 文件）
- 基线：~46 分钟
- 优化后：~34 分钟（节省 ~12 分钟）

### 性能瓶颈分析
- **LSP-bound**：symbol extraction + incoming calls 占总时间 95%+
- **IO-bound**：Neo4j 写入仅占 ~5%
- **优化空间**：
  - 并行化：clangd 是单进程，但可多开实例分片处理
  - 缓存：LSP 结果可缓存，避免重复解析未变更文件

---

## 10. 配置参数

| 参数 | 位置 | 默认值 | 说明 |
|------|------|--------|------|
| `delay_between_calls` | `symbol_extractor.process_file()` | `0.0` | LSP 请求间 sleep，已优化为 0 |
| `sleep_after` | `extract_incoming_calls_for_function()` | `0.01` | incomingCalls 后的 sleep |
| `BATCH_SIZE` | `neo4j_writer.py` | `500` | UNWIND 批量大小 |
| `resolution` | `graph_builder._build_module_nodes()` | `0.3` | Louvain 分辨率，越小社区越大 |
| `clear_existing` | `run_full_pipeline()` | `True` | 是否先清空旧图 |
| `collect_calls` | `run_full_pipeline()` | `True` | 是否收集 outgoingCalls |
| `collect_var_refs` | `run_full_pipeline()` | `True` | 是否收集变量引用 |
| `extract_macros` | `run_full_pipeline()` | `True` | 是否正则提取宏调用 |
| `skip_vendor_calls` | `run_full_pipeline()` | `True` | 是否跳过 vendor 目录 outgoingCalls |

---

## 11. 已知问题与限制

### 11.1 已修复
- **Neo4j 嵌套 Map 错误**：`param_usage` 列表无法直接写入节点属性 → 改为 JSON 字符串 `param_usage_json`
- **日志格式错误**：`ingest_subset.py` 中 `%(files)d` 与位置参数冲突 → 统一为单字典参数

### 11.2 现存限制
1. **单线程 LSP**：clangd 是单进程，pipeline 顺序处理文件，无法并行
2. **头文件重复**：同一模板/内联函数在 .h 和 .cpp 中可能被重复提取（P0 去重只处理同文件）
3. **跨文件 lambda**：incomingCalls 只能捕获部分 lambda 调用关系
4. **宏调用精度**：正则回退提取的宏调用无行号精确匹配，仅作补充
5. **Louvain 可选**：若未安装 `python-louvain`，Module 节点不生成
6. **资源配对局限**：只匹配同函数内的 allocate/release，跨函数配对（如 factory→destructor）未实现

### 11.3 潜在风险
- **clangd 稳定性**：`delay=0` 下 64 文件测试通过，但 740 文件全量运行时，长时间高频率 LSP 请求可能导致 clangd 内存增长或响应延迟
- **大文件处理**：>5000 行的文件（如 `ggml.c`）LSP 响应可能超时（当前 timeout=30s）

---

## 12. 运行方式

### 全量摄取（清空旧图）
```bash
python scripts/ingestion/ingest_code.py
```

### 子集摄取（如 common/）
```bash
python scripts/ingestion/ingest_subset.py
# 内部调用 run_full_pipeline(include_dirs=["common"], clear_existing=False)
```

### 增量摄取（按文件更新）
```bash
python scripts/ingestion/ingest_code_incremental.py --file-path src/common/some.cpp
```

### 环境要求
- Neo4j 运行中（`.env` 配置 `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`）
- `compile_commands.json` 已生成（llama.cpp/build/）
- clangd 20+ 在 PATH 中
- Python 依赖：`neo4j`, `networkx`, `python-louvain`（可选）

---

## 13. QA Agent 检索策略（下游使用）

基于当前图结构，QA Agent 的检索路径：

```
Function
  ├─ CALLS → Function（调用链上下游）
  ├─ CONTROL_FLOW → ControlFlowBlock（条件判断、错误处理）
  ├─ MANAGES → ResourceOperation（资源分配/释放）
  ├─ REFERENCES_VAR → Variable（变量使用）
  ├─ param_usage_json（参数使用模式，JSON 字符串）
  └─ BELONGS_TO → Module（模块归属）
```

---

*文档维护：每次修改 ingestion pipeline 后，请同步更新此文档。*
