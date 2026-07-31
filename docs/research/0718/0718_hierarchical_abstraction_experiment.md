# Tree-sitter 层次化 Module / Concept 聚类实验

## 问题

此前 Tree-sitter QA 对照缓存把每个文件同时作为一个 Module 和一个 Concept，得到：

```text
718 Modules / 718 Concepts
```

这种设计可以验证 Tree-sitter 建图是否能支持 QA，但没有形成真正的高层抽象。Module、Concept 和 File 实际上是同一层。

## 新流程

实验脚本：

```text
scripts/analysis/experiment_hierarchical_abstraction.py
```

该脚本完全离线运行，不依赖 clangd，也不读写 Neo4j。

### Module：文件图社区

Module 的节点是文件，不是函数。文件边由以下信号组成：

1. Tree-sitter 提取的 include 关系；
2. `CALLS_CANDIDATE` 对应的保守词法调用解析，并聚合到文件级；
3. 稀疏的同目录先验。

跨文件调用边使用置信度和文件规模归一化，避免大文件仅仅因为函数多而支配聚类。同目录关系使用星形边，不构造目录内全连接。

### Concept：Module 内函数 kNN 社区

在每个 Module 内，使用以下信号构造函数图：

1. 已有函数 embedding 的 top-10 kNN；
2. 置信度加权的词法调用边；
3. class / namespace 归属；
4. 稀疏的同文件先验。

然后在每个 Module 内独立进行社区发现。该方式避免了全仓库函数图和同文件 `O(n²)` 边。

## 聚类结果

输入：

| 项目 | 数量 |
|---|---:|
| 文件 | 743 |
| QA 函数 | 15,900 |
| include 边 | 1,952 |
| 解析后的词法调用边 | 24,922 |
| 文件图边 | 4,702 |

输出：

```text
743 Files -> 36 Modules -> 325 Concepts -> 15,900 Functions
```

函数覆盖率为 100%。

| 分布 | Module 函数数 | Concept 函数数 | 每 Module Concept 数 |
|---|---:|---:|---:|
| 中位数 | 85.5 | 31 | 5.5 |
| P90 | 1,278.5 | 109.6 | 23 |
| 最大值 | 3,912 | 348 | 37 |

这已经消除了 Module 与 Concept 等价的问题。不过最大 Module 仍然过大，下一步需要加入超大社区递归拆分或 Leiden 约束。

生成文件：

```text
data/module_abstraction_hierarchical_20260718.json
data/concept_abstraction_hierarchical_20260718.json
results/hierarchical_abstraction_experiment_20260718.json
```

## Hard benchmark 端到端结果

答案模型：`deepseek-v4-flash`

Judge：`gpt-4.1-mini`

| 方案 | Binary correctness | Gold 全引用 | 部分引用 | 零引用 | 平均覆盖 |
|---|---:|---:|---:|---:|---:|
| 718/718 文件对照 | 90% | 60% | 14 | 6 | 73.8% |
| 36/325 层次聚类 | 94% | 72% | 7 | 7 | 78.0% |

新旧召回结果并不相同：

- 只有 5/50 题的 `retrieved_functions` 顺序完全一致；
- 两组 top-50 召回集合平均 Jaccard 为 0.738；
- 最低 Jaccard 为 0.19。

因此新聚类确实改变了检索范围。但上述结果只包含一次生成和一次 Judge，2--4 个百分点仍可能受到模型随机性影响，不能据此宣称稳定提升。

有效评测文件：

```text
results/qa_concept_symbol_hierarchical_20260718_deepseek-v4-flash.json
results/eval_concept_symbol_hierarchical_20260718_by_gpt41mini.json
```

首次 citation judge 因隔离环境缺少 `json_repair` 而把所有题错误记录为 0%；补齐依赖后已经覆盖重跑，上述 78.0% 是修复后的结果。

## 复现

```bash
python scripts/analysis/experiment_hierarchical_abstraction.py

python scripts/analysis/run_tree_sitter_concept_symbol_qa.py \
  --concept-cache data/concept_abstraction_hierarchical_20260718.json \
  --output results/qa_concept_symbol_hierarchical_20260718_deepseek-v4-flash.json \
  --model deepseek-v4-flash \
  --workers 10

python evals/eval_v2.py \
  --result results/qa_concept_symbol_hierarchical_20260718_deepseek-v4-flash.json \
  --benchmark datasets/benchmark_hard.json \
  --range all \
  --model gpt-4.1-mini \
  -o results/eval_concept_symbol_hierarchical_20260718_by_gpt41mini.json \
  -w 20
```

## 当前结论

文件级 Module、Module 内语义 Concept 的方向可行。在保持全部函数覆盖的情况下，它形成了真正不同的抽象层，并且本次 Hard benchmark 没有出现端到端能力下降。

下一轮应优先处理：

1. 对超大 Module 做递归拆分，限制最大文件数和函数数；
2. 用 Leiden 替代 Louvain，检查社区连通性和稳定性；
3. 对不同随机种子和不同 Module/Concept resolution 做离线 gold recall 对照；
4. Module/Concept 用作软排序先验，不能作为单选硬边界；
5. 重复运行答案生成，确认提升不是单次 LLM 波动。
