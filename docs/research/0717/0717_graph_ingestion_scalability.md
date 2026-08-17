# 0717 超大规模代码仓库建图优化记录

记录日期：2026-07-17

## 1. 本阶段目标

本阶段围绕大规模 C/C++ 仓库的建图稳定性和可扩展性展开：

1. 评估 clangd 在大文件、缺失编译配置和超大文件数量下的可用性。
2. 验证 Tree-sitter 作为基础结构解析器的可行性。
3. 解决一次性解析 337K 个文件导致的 OOM。
4. 降低图组装和 Neo4j 写入的内存与事务开销。
5. 使用真正的端到端 Hard benchmark 检查回答质量和 gold evidence 引用。

---

## 2. clangd 与 Tree-sitter 的定位

clangd 适合提供精确的类型解析和调用关系，但它依赖编译上下文和有状态索引。在大规模仓库中，存在以下问题：

- 大文件可能导致 LSP 请求长时间超时。
- compile_commands.json 可能缺少文件或配置不完整。
- 单个异常文件可能拖慢整个索引阶段。
- 全量索引数十万文件时，启动、索引和内存成本较高。

### 2.1 clangd 的条件编译覆盖缺口

clangd 的解析范围和解析结果依赖当前 compile_commands.json、编译器参数、include 路径以及宏定义环境。对于条件编译较多的仓库，存在一个比超时更根本的问题：某些源码文件或代码分支根本不会被当前构建配置覆盖。

常见情况包括：

- 某个后端文件只在 SYCL、CUDA、Vulkan、Metal 或其他可选构建配置下参与编译，当前 compile_commands.json 中没有对应条目。
- 同一个文件在不同宏定义下展开出不同的函数、类型和调用路径，clangd 当前只看到其中一种配置。
- 生成文件、平台专用文件或实验性后端没有进入默认构建目标，因此不会进入 clangd 的完整索引。
- 即使手动补充文件，缺少正确的编译参数时也可能得到空符号、错误语法树或大量超时。
- compile_commands.json 本身可能生成不全或覆盖旧版本，导致“文件存在但建图没有覆盖”的问题。

这意味着 clangd 的结果不能直接等价于仓库的完整代码图。它更准确地表示“某一个编译配置下被索引到的语义子图”，而不是源码仓库的全集。

因此，基础建图不能以 clangd 成功解析为前提。应先由 Tree-sitter 按源码目录扫描所有目标语言文件，覆盖条件编译分支和未进入默认构建的文件；之后再使用 clangd 对有有效编译上下文的文件进行语义增强。

Tree-sitter 更适合提供必须完成的基础结构层：

- 文件、目录和 namespace
- class / struct
- 函数定义和声明
- include
- 宏和基础语法结构
- 词法调用候选

Tree-sitter 不提供完整的 C++ 类型和名称解析，因此其调用关系应分级存储，不能把所有词法匹配都当成精确 CALLS。

推荐架构：

    Tree-sitter：基础结构图，必须完成
        ↓
    词法调用：CALLS_CANDIDATE，带来源和置信度
        ↓
    clangd：按需增强热点文件和查询相关调用路径

---

## 3. 流式 Tree-sitter 解析

测试仓库：/data/users/zzy/RUC/llama.cpp

全量 Tree-sitter 结果：

| 指标 | 数量 |
|---|---:|
| 文件 | 750 |
| 函数 | 23,689 |
| 类/结构体 | 8,065 |
| 调用候选 | 157,779 |

流式版本将文件按批次解析，并立即写入 JSONL shard：

    有限批次文件
        ↓
    Tree-sitter 解析
        ↓
    写入 shard
        ↓
    释放当前批次
        ↓
    继续下一批

运行命令：

    PYTHONPATH=. python scripts/ingestion/extract_tree_sitter_shards.py
        /data/users/zzy/RUC/llama.cpp
        --output results/tree_sitter_shards_streaming_20260716
        --batch-size 64

测试结果：

- 750 个文件被分为 12 个 shard。
- 流式解析与旧版全量解析的函数 ID 完全一致。
- 函数数量和调用候选数量完全一致。
- 解析耗时约 12～20 秒。
- 子进程峰值内存约 175 MB。

相关文件：

- src/ingestion/tree_sitter_cpp_extractor.py
- src/ingestion/tree_sitter_shards.py
- scripts/ingestion/extract_tree_sitter_shards.py

---

## 4. 337K 文件 OOM 的原因与解决方案

旧流程会同时把以下对象留在内存中：

- 文件内容
- Tree-sitter AST
- FileResult
- 函数、类和调用对象
- 完整 graph nodes / edges
- 去重集合和 ID 映射

因此内存占用实际远大于源文件大小。337K 个文件不能采用“全量解析、全量聚集、一次性写图”的方式。

现在的目标是：

    内存占用 ≈ 单批解析数据 + 当前 Neo4j 写入批次

而不是：

    内存占用 ≈ 整个仓库

后续应继续把 shard 作为中间事实层，让 Neo4j 和 embedding 索引都从 shard 分批读取。

---

## 5. 删除 O(N²) 同文件函数全连接

原来的 Module 构建会把同一个文件中的所有函数两两连接。一个包含 1,000 个函数的文件会产生约 500,000 条人工边。

这些边不是实际调用关系，会造成：

- 图组装内存暴涨。
- Louvain 计算变慢。
- Neo4j 写入量膨胀。
- 模块划分被人工边影响。

现已删除该逻辑。文件内结构由层级边表达：

    File -CONTAINS-> Function
    File -CONTAINS-> Class
    Class -HAS_METHOD-> Function

修改文件：

- src/ingestion/graph_builder.py

---

## 6. Neo4j 写入优化

Neo4j writer 当前做了两项调整：

1. 默认批次从 500 调整为 5,000，减少小事务和网络往返。
2. 增加 merge=False 的全量重建模式。

全量重建且上游已经保证节点和边去重时，可以使用：

    write_graph(driver, graph, database, batch_size=5000, merge=False)

增量更新仍然使用默认的 MERGE 模式。

推荐写入架构：

    解析阶段：有限并行或顺序分片
    Neo4j 阶段：单写入者、串行、大批次

目前已完成代码和语法回归，但还没有在真实 Neo4j 实例上完成千万行规模写入压测。

修改文件：

- src/ingestion/neo4j_writer.py

---

## 7. Hard benchmark 指标纠正

之前误用了区域检索中的 full_coverage 指标。它只表示：

    gold functions 是否落入候选区域。

例如 dir_d0 会把相关目录中的大量函数全部纳入候选，所以该指标可以很高，但它不代表系统已经正确回答问题，也不代表引用了 gold evidence。

真正需要关注的是：

1. 生成答案是否正确。
2. 是否引用了预期的 gold evidence 文件。
3. gold evidence 的引用覆盖率。

Tree-sitter 端到端结果：

| 建图版本 | Binary 正确率 | Gold 文件全引用 | 平均引用覆盖 |
|---|---:|---:|---:|
| Tree-sitter CALLS 派生 Module/Concept | 45/50 = 90% | 33/50 = 66% | 77.7% |
| Tree-sitter 按文件聚合 | 46/50 = 92% | 35/50 = 70% | 80.0% |
| clangd 基线 | 46/50 = 92% | 33/50 = 66% | 75.5% |

结论：

- Tree-sitter 没有导致回答正确率明显下降。
- 按文件聚合版本与 clangd 基线持平。
- 引用覆盖率没有下降，文件聚合版本略高。
- 当前 citation judge 是文件级判断，还没有精确到行号。

相关结果：

- results/eval_concept_symbol_tree_sitter_deepseek-v4-flash_by_gpt41mini_20260716.json
- results/eval_concept_symbol_tree_sitter_file_deepseek-v4-flash_by_gpt41mini_20260716.json

---

## 8. 当前推荐架构

    文件清单与过滤
        ↓
    Tree-sitter 分批解析
        ↓
    本地 JSONL / SQLite / Parquet shard
        ↓
    Directory / Module / Concept / File / Function 结构索引
        ↓
    include 与调用候选索引
        ↓
    Neo4j 单写入者批量导入
        ↓
    按需使用 clangd 增强热点文件和查询相关路径

图结构建议：

    Repository
      └── Directory / Subsystem
            └── Module
                  └── Concept
                        └── File
                              └── Class / Namespace / Function

核心原则：

> 结构图必须尽量完整，语义调用图允许不完整；Neo4j 存导航所需的图，不存所有原始解析证据。

---

## 9. 后续工作

1. 增加 shard 到 Neo4j 的真正流式导入器。
2. 为文件、符号、include 和调用候选建立独立索引。
3. 给 Tree-sitter 调用候选增加置信度和来源属性。
4. 根据文件 hash、parser version 和 grammar version 实现增量更新。
5. 在真实 Neo4j 上测试 5,000、10,000、20,000 不同批次的写入性能。
6. 将新 shard 导入后的完整 Neo4j 图重新执行端到端 Hard benchmark。
7. 将 citation judge 从文件级覆盖扩展到文件+行号级证据覆盖。
