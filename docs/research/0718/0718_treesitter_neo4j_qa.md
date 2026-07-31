# 0718 Tree-sitter-only 完整建图与 QA 评测记录

记录日期：2026-07-18

## 1. 本次实验目标

验证新的 Tree-sitter-only 建图流程是否能够完整跑通：

    Tree-sitter 解析
        ↓
    写入真实 Neo4j
        ↓
    从 Neo4j 重建 Module / Concept
        ↓
    运行 Hard benchmark QA
        ↓
    评估答案正确性和 gold evidence 引用

本次流程没有使用 clangd，也没有读取 compile_commands.json。

---

## 2. Neo4j 实例

原 Neo4j 实例的 transaction log 文件属于 root，但 Neo4j 进程由 zzy 用户运行，导致旧实例无法继续写入 transaction log。

因此本次实验启动了一个独立 Neo4j 实例：

    Bolt：bolt://127.0.0.1:7688
    HTTP：7475
    数据目录：Code_Graph/.neo4j
    运行方式：zzy 用户
    认证：关闭，仅用于本地实验

独立实例没有使用原来的 /var/lib/neo4j 数据目录，也没有修改原 Neo4j 实例。

---

## 3. 实际建图流程

### 3.1 文件扫描和解析

源码仓库：

    /data/users/zzy/RUC/llama.cpp

Tree-sitter 直接扫描 C/C++ 文件，不依赖 compile_commands.json：

- C
- C++
- CC
- CXX
- H
- HPP
- HH
- HXX

每批文件解析后立即写入 JSONL shard，不把整个仓库的 FileResult 保留在内存中。

本次产物：

    results/tree_sitter_neo4j_20260718/

解析统计：

| 指标 | 数量 |
|---|---:|
| 文件 | 750 |
| 函数 | 23,689 |
| 类/结构体 | 8,065 |
| 原始词法调用候选 | 157,779 |
| shard | 1 |

### 3.2 符号索引

解析完成后，流程从 shard 建立 SQLite 符号索引，记录：

- 函数 ID
- 函数名
- 函数名尾部
- 所属文件
- 是否为定义

该索引用于在第二阶段解析词法调用候选。

### 3.3 Neo4j 节点

实际写入 Neo4j 的节点：

| 节点类型 | 数量 |
|---|---:|
| Repository | 1 |
| Directory | 110 |
| File | 750 |
| Class | 7,883 |
| Function | 23,689 |

Tree-sitter 解析出的部分类声明在 Neo4j 中由于 ID 去重后为 7,883 个。

### 3.4 Neo4j 边

实际写入的边：

| 边类型 | 数量 |
|---|---:|
| CONTAINS | 32,432 |
| CALLS_CANDIDATE | 23,819 |

当前没有把 Tree-sitter 词法调用直接命名为精确 CALLS，而是使用 CALLS_CANDIDATE，并记录：

- confidence
- resolution
- source
- 调用行号

当前解析策略：

- 同文件唯一匹配：高置信度
- 全局唯一匹配：中等置信度
- 多候选调用：不写入确定调用边

---

## 4. 当前代码入口

默认建图入口：

    scripts/ingestion/ingest_code.py

兼容入口：

    scripts/ingestion/ingest_parallel.py

Tree-sitter pipeline：

    src/ingestion/tree_sitter_pipeline.py

Tree-sitter shard：

    src/ingestion/tree_sitter_shards.py

当前两个 ingestion 入口都不会：

- 启动 clangd
- 创建 LSPClient
- 读取 compile_commands.json
- 等待 clangd 全局索引

ingest_parallel.py 中的 workers 参数目前只保留兼容性，Neo4j 写入仍然使用单写入者。

---

## 5. Module / Concept 重建

本次从新 Neo4j 实例查询 Function 节点，生成了新的 Tree-sitter-only cache：

    data/module_abstraction_tree_sitter_neo4j_20260718.json
    data/concept_abstraction_tree_sitter_neo4j_20260718.json

为了控制变量，本次采用按文件聚合：

| 指标 | 数量 |
|---|---:|
| Neo4j Function | 23,689 |
| QA 索引中可用函数 | 15,900 |
| Module | 718 |
| Concept | 718 |

每个文件对应一个 Module 和一个 Concept。该方案不是最终的语义抽象方案，而是当前 Tree-sitter-only Neo4j 闭环的稳定基线。

---

## 6. Hard benchmark QA

Benchmark：

    datasets/benchmark_hard.json

共 50 道问题。

答案模型：

    deepseek-v4-flash

评估模型：

    gpt-4.1-mini

QA 答案：

    results/qa_concept_symbol_tree_sitter_neo4j_20260718_deepseek-v4-flash.json

评估结果：

    results/eval_concept_symbol_tree_sitter_neo4j_20260718_by_gpt41mini.json

### 6.1 结果

| 指标 | 结果 |
|---|---:|
| Binary correctness | 45/50 = 90.0% |
| Gold evidence 全引用 | 30/50 = 60.0% |
| Gold evidence 部分引用 | 14/50 |
| Gold evidence 零引用 | 6/50 |
| 平均 citation coverage | 73.8% |

### 6.2 与此前结果对比

| 方案 | Binary correctness | Gold 文件全引用 | 平均引用覆盖 |
|---|---:|---:|---:|
| clangd 基线 | 92% | 66% | 75.5% |
| Tree-sitter 独立 CALLS 抽象 | 90% | 66% | 77.7% |
| Tree-sitter 独立文件聚合 | 92% | 70% | 80.0% |
| 本次真实 Neo4j Tree-sitter 图 | 90% | 60% | 73.8% |

本次结果和此前 Tree-sitter 实验基本处于同一水平，没有出现明显的回答能力崩溃，但引用覆盖率略低。

需要注意，Hard benchmark 使用了 LLM 生成答案，单次结果会受到模型输出和 API 随机性的影响，因此不能只根据 2 个百分点的 Binary 差异判断图质量发生了确定性下降。

---

## 7. 本次实验得到的结论

### 7.1 建图链路已经真实跑通

本次不是离线模拟：

- Tree-sitter 真实解析了仓库。
- 结果真实写入了独立 Neo4j。
- Module / Concept cache 来自新 Neo4j 中的 Function 节点。
- Hard benchmark 答案和评估在该 cache 上完成。

### 7.2 clangd 可以从默认建图链路中移除

当前结果表明，在这个仓库和 Hard benchmark 上：

- Tree-sitter-only 可以完成完整结构建图。
- 不依赖 compile_commands.json。
- 不受默认构建配置限制。
- 可以覆盖未进入当前构建目标的条件编译文件。
- QA Binary correctness 仍达到 90%。

### 7.3 当前主要短板

当前 Tree-sitter-only pipeline 还存在以下限制：

1. Neo4j 中目前主要是结构边和 CALLS_CANDIDATE，尚未形成完整的精确语义 CALLS。
2. Module / Concept 本次按文件聚合，尚未使用更成熟的目录、namespace、include 和候选调用联合聚类。
3. embedding 索引复用了已有 Tree-sitter 函数 ID 对应的向量，没有重新编码全部函数。
4. 当前 citation judge 主要评估 gold 文件级引用，还没有精确评估 gold 行号。
5. 当前 Neo4j writer 仍需要进一步减少小批次 Cypher 请求。

---

## 8. 后续工作

1. 将 CALLS_CANDIDATE 按置信度分层写入图。
2. 使用目录、namespace、include 和候选调用构建更稳定的 Module。
3. 在 Module 内构建 Concept，但避免同文件函数 O(N²) 全连接。
4. 将 Neo4j 导入改为真正的 shard 级批量导入。
5. 重新生成完整 embedding index，避免复用旧向量。
6. 优化 citation judge，使其支持文件和行号级 gold evidence。
7. 对新流程进行多次 Hard benchmark 重复实验，估计 LLM 生成随机性。
