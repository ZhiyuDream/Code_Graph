# 端到端引用 Gap 实验报告

## 实验目的

验证：**当检索阶段已经把 gold evidence 文件找到 90% 时，答案生成阶段是否能把这些文件完整引用出来？**

结果出人意料：**不能**。检索召回 90%，答案完整引用率只有 30%。

---

## 一、整体 Pipeline

```text
Question
  ↓
Stage 0: Symbol-Graph Worklist（检索阶段）
  - Top20 embedding 检索
  - 2-hop 目录扩展
  - 符号图扩展（callers/callees）
  - 输出：每题访问约 60 个文件
  ↓
Stage 1: Evidence Extraction（记忆管理 / 信息过滤）
  - 对每个访问到的文件，用 deepseek-v4-flash 判断相关性
  - 保留相关的文件，提取 key facts
  - 输出：每题约 57 个 evidence 文件
  ↓
Stage 2: Answer Generation（答案生成）
  - 把 evidence_log + files_summary 喂给 deepseek-v4-pro
  - 生成带引用的答案
  ↓
Stage 3: Citation Evaluation
  - 字符串匹配：答案里是否出现 gold evidence 文件路径
```

---

## 二、Stage 0：怎么检索到文件？

使用 `experiments/run_symbol_graph_worklist.py`，基于 `SymbolGraphWorklist` 逻辑：

### 2.1 初始候选池

```python
query_emb = retriever.encode_queries([question])
pool = build_function_pool(query_emb, retriever, top_k_files=20, top_m_functions=5)
```

1. 用原问题做 embedding；
2. 取全局最相关的 20 个**文件**；
3. 每个文件内取最相关的 5 个**函数**；
4. 得到约 15-20 个唯一文件。

### 2.2 目录扩展

对已访问文件所在目录，用原问题 embedding 做**局部检索**，每目录再取 top 5 未访问文件。

```python
for hop in range(2):
    visited_dirs = {Path(v).parent for v in visited_files}
    for d in visited_dirs:
        candidates = files_in_directory[d] - visited_files
        results = retriever.retrieve(query_emb, top_k=5, file_filter=candidates)
        read results
```

### 2.3 符号图扩展

从已访问文件内容中提取高频函数名，对每个符号做 `find_callers` / `find_callees`，读取新发现的文件。

### 2.4 Stage 0 输出

| 指标 | 数值 |
|------|-----:|
| 完整覆盖率 | 45/50 = **90%** |
| 平均覆盖率 | 95.0% |
| 平均访问文件数 | 62.0 |

---

## 三、Stage 1：读完文件后怎么做记忆管理和信息传递？

这是整个 pipeline 的**核心设计**。不是简单地把所有文件内容堆进答案 prompt，而是先做一次**结构化压缩**。

### 3.1 为什么需要记忆管理？

Stage 0 平均访问 62 个文件。如果全部塞进答案生成 prompt：
- 上下文太长，模型容易 lost；
- 噪声文件会干扰判断；
- token 成本极高。

所以需要：
1. **判断每个文件是否相关**；
2. **只把相关文件传给下一 stage**；
3. **用结构化形式（key facts）传递信息**，而不是原始代码。

### 3.2 复用 BaseInvestigator.extract_evidence

使用 `src/qa.investigation.base.BaseInvestigator.extract_evidence`，模型为 `deepseek-v4-flash`。

对每个文件执行：

```python
inv = BaseInvestigator(max_steps=0, model="deepseek-v4-flash")
content = inv.read_file(file_path)
evidence = inv.extract_evidence(file_path, content)
```

`extract_evidence` 内部 prompt（`prompts/extract_evidence.txt`）要求模型输出：

```json
{
  "key_facts": ["...", "..."],
  "new_hypothesis": "...",
  "suspicious_symbols": ["..."],
  "suspicious_files": ["..."]
}
```

### 3.3 信息传递结构

`BaseInvestigator` 用 `InvestigationState` 维护记忆：

```python
@dataclass
class InvestigationState:
    question: str
    entry_file: str
    visited_files: List[str]          # 访问过的文件
    files_content: Dict[str, str]     # 文件原始内容
    evidence_log: List[Dict]          # 每文件提取的证据
    suspicion: SuspicionState         # 当前怀疑焦点
```

**记忆管理流程：**

```text
读文件 → files_content[file_path] = content
  ↓
extract_evidence → evidence_log.append(evidence)
  ↓
只有 evidence 非空（key_facts/suspicious_symbols/suspicious_files）的文件才进入 Stage 2
```

### 3.4 Stage 1 输出

| 指标 | 数值 |
|------|-----:|
| 平均 evidence 文件数 | 57.2 |
| 检索到的 gold 文件数 | 102 |
| 进入 evidence 的 gold 文件数 | **101** |
| 被 evidence extraction 丢弃的 gold 文件数 | **1** |

**结论：记忆管理阶段几乎没有丢失 gold。**

---

## 四、Stage 2：怎么把检索到的文件塞进上下文？

使用 `BaseInvestigator.generate_answer()`，模型为 `deepseek-v4-pro`。

### 4.1 上下文构建

`generate_answer` 把两个东西塞进 prompt：

1. **evidence_log**：每个相关文件的 key facts、hypothesis、suspicious symbols
2. **files_summary**：每个相关文件前 1500 字符的原始内容

```python
evidence_log = "\n\n".join(
    f"=== {e['file_path']} ===\n"
    f"Key facts: {e.get('key_facts', [])}\n"
    f"Hypothesis: {e.get('new_hypothesis', '')}\n"
    f"Suspicious symbols: {e.get('suspicious_symbols', [])}"
    for e in self.state.evidence_log
)

files_summary = "\n\n".join(
    f"=== {fp} ===\n{content[:1500]}"
    for fp, content in self.state.files_content.items()
)
```

Prompt 见 `prompts/generate_answer.txt`：

```text
【问题】
{question}

【调查过程中访问的文件及关键证据】
{evidence_log}

【已访问文件内容摘要】
{files_summary}

要求：
1. 直接回答原始问题
2. 引用你使用的代码证据（格式：file.cpp:start-end）
3. 答案末尾必须包含固定格式的"引用文件清单"
```

### 4.2 Stage 2 输出

每题一个答案文本，带"引用文件清单"。

---

## 五、Stage 3：怎么评估引用完整性？

### 5.1 不用纯字符串匹配

一开始用字符串匹配：

```python
def extract_cited_files(answer: str) -> set[str]:
    pattern = r"[\w\-/.]+\.(?:cpp|c|h|hpp)"
    return set(normalize_path(p) for p in re.findall(pattern, answer))

coverage = len(gold_files ∩ cited_files) / len(gold_files)
```

但发现有两个问题：

1. **漏检**：答案可能说"在 chat.cpp 中"，但字符串匹配只认完整路径 `common/chat.cpp`。
2. **误检**：答案可能顺带提到文件名但没有分析其内容，字符串匹配也会算成引用。

### 5.2 改用 LLM Citation Judge

复用 `evals/eval_v2.py` 里的 `llm_citation_judge`，用 `gpt-4.1-mini` 评估。

评估规则：
- 只看 `.cpp` / `.c` 文件，**忽略 `.h` / `.hpp` 头文件**
- 只要答案明确提到文件路径并作为分析证据使用，就算覆盖
- 不要求行号精确匹配
- 对每个缺失的 gold 文件，判断原因：
  - **检索失败**：答案中完全没有提到该文件
  - **搜到未引**：答案中提到了该文件但没有作为核心证据分析
  - **不需要**：该文件对回答问题不是必需的

调用方式：

```python
from evals.eval_v2 import llm_citation_judge

judgment = llm_citation_judge(
    question=question,
    reference=reference_answer,
    generated=answer,
    gold_files=gold_files,  # 已排除 .h/.hpp
)

# judgment 包含：
# - coverage_ratio: 覆盖率
# - cited_files: 被引用的文件列表
# - missing_files: 缺失的文件列表
# - missing_reasons: 每个缺失文件的原因
# - notes: 简要说明
```

---

## 六、最终结果

### 6.1 核心数字

| 阶段 | 完整覆盖率/引用率 | 平均覆盖率/引用率 |
|------|----------------:|----------------:|
| Stage 0 检索（Symbol-Graph Worklist） | **90%** | **95.0%** |
| Stage 1 记忆管理后（Evidence Extraction） | 约 90%（仅丢 1 个 gold） | - |
| Stage 2 答案生成（Citation，字符串匹配） | 30% | 55.8% |
| **Stage 2 答案生成（Citation，LLM Judge）** | **24%** | **50.8%** |

**LLM judge 比字符串匹配更严格**，因为它要求文件被"作为证据分析"，而不只是文本中出现。

### 6.2 关键观察

**检索找到了 102 个 gold 文件中的 101 个，但答案只完整引用了 15/50 题。**

典型失败案例：

| QID | 检索覆盖率 | 答案引用覆盖率 | Gold 文件都在 evidence 里吗？ |
|---:|---:|---:|:---|
| posthoc_public_003 | 100% | 25% | 是（4/4） |
| posthoc_public_024 | 100% | 0% | 是（2/2） |
| posthoc_public_037 | 100% | 100% | 否（0%） → 是（2/2） |
| posthoc_public_044 | 100% | 0% | 否（0/1） |
| posthoc_public_048 | 100% | 0% | 是（3/3） |
| posthoc_public_050 | 100% | 0% | 是（1/1） |

### 6.3 缺失原因分析（LLM Judge）

在 53 个缺失文件判断中：

| 缺失原因 | 数量 | 比例 |
|---------|-----:|-----:|
| **检索失败**（答案里完全没提） | 43 | 81% |
| **搜到未引**（提了但没作为核心证据） | 7 | 13% |
| **引用但未作为核心证据分析** | 2 | 4% |
| 重复列出 | 1 | 2% |

**关键发现：** 81% 的缺失是因为答案生成阶段**完全没有提到该文件**。

这说明：即使 gold 文件已经进入了 `evidence_log` 和 `files_summary`，deepseek-v4-pro 在生成答案时也没有把它们列出来。不是"搜到了没引用"，而是"生成答案时选择性忽略了大部分证据文件"。

### 6.4 初步结论（需谨慎）

从表面数字看：

```text
检索        : 90%  ✓
记忆管理    : 99%  ✓
答案引用    : 24%  ✗
```

但这个结论**证据不足**，可能混入了多个混淆变量：
- evidence extraction 是否真的保留了 gold 信息？
- prompt 有多长？LLM 是否看到了所有内容？
- gold 文件在 prompt 中的位置是否影响引用？
- 所有 gold 文件真的都应该被引用吗？
- LLM 是用了但没引用，还是根本没用？
- prompt 是否鼓励总结而非穷举？

详见 [`confounder_analysis.md`](confounder_analysis.md)。在排除这些混淆变量之前，不应断言"答案生成是瓶颈"。

---

## 七、为什么答案不引用？

### 7.1 可能原因 1：上下文仍然太长

Stage 2 平均收到 57 个文件。即使每个文件只取 1500 字符，总上下文也有约 85K 字符，加上 evidence_log，可能超过模型有效处理范围。

### 7.2 可能原因 2：模型倾向于"总结"而非"穷举"

deepseek-v4-pro 看到大量文件后，倾向于总结核心结论，只引用少数"代表性"文件，而不是把证据链上所有文件都列出来。

### 7.3 可能原因 3：Prompt 约束力不够

虽然 prompt 要求"引用文件清单"，但没有明确说"**必须把 evidence_log 中所有相关文件都列出来，不要遗漏**"。

### 7.4 可能原因 4：Evidence 文件之间缺少显式关联

evidence_log 是每个文件独立提取的 key facts，模型需要自己在脑中把它们串成证据链。如果模型没串起来，就会漏引中间文件。

---

## 八、下一步方向

1. **强化 citation prompt**：明确要求穷举所有相关文件，禁止遗漏。
2. **上下文截断**：只把 top 10-20 最相关文件给答案生成，减少噪声和认知负荷。
3. **两阶段答案生成**：
   - 第一阶段：让模型先列出所有相关文件；
   - 第二阶段：基于确定好的文件列表写答案。
4. **后处理补引**：生成答案后检查遗漏的 gold 文件，让模型针对遗漏文件补充引用。

---

## 九、代码与结果文件

- 实验脚本：`experiments/run_worklist_end_to_end_v2.py`
- 检索输入：`results/symgraph_worklist_hard_L100_K20_E5_M15_H2_S5.json`
- 端到端结果：`results/worklist_e2e_v2_symgraph_worklist_hard_L100_K20_E5_M15_H2_S5.json`
- 核心复用组件：`src/qa/investigation/base.py` 中的 `BaseInvestigator.extract_evidence` 和 `generate_answer`

---

## 十、方法论备注

**为什么这个实验重要？**

之前大量工作都在优化检索覆盖率，但仓库级代码 QA 的最终目标是生成**可审计、可追溯**的答案。如果检索到了 evidence 但答案不引用，用户仍然无法验证结论来源。

这个实验说明：即使检索问题基本解决，**答案生成阶段的引用合规性**仍然是主要瓶颈。
