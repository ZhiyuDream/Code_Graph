# 0714 实验与工程改造记录

记录日期：2026-07-14 前后

本阶段核心目标：
1. 验证 glm-5.2 在 Repository QA 端到端流程中的实际表现。
2. 统一 Embedding / LLM 调用接口，消除脚本中的硬编码 provider/model。
3. 改进日志与文件管理，减少零散中间文件。

---

## 1. 模型切换接口改造

### 1.1 问题

原脚本硬编码 provider/model：
- `evals/eval_v2.py` 需要 `--provider openai/deepseek`，内部直接构造 `OpenAI(...)`。
- `scripts/qa/run_module_symbol_qa.py` 需要 `--provider openai/deepseek`，内部直接构造 `OpenAI(...)`。
- `scripts/qa/run_concept_symbol_qa.py` 写死 DeepSeek client，循环跑 `deepseek-v4-pro` 和 `deepseek-v4-flash`。

这导致切换模型（如 glm-5.2）需要改多处代码，且无法灵活指定任意模型。

### 1.2 改造

统一为只通过 `--model` 指定模型，调用层全部走 `src/core/llm_client.py` 的 `call_llm()` / `call_llm_json()`，由 `ModelRegistry` 自动解析 provider / api_key / base_url。

| 文件 | 改动 |
|---|---|
| `evals/eval_v2.py` | 移除 `--provider`，`--model` 指定 judge 模型 |
| `scripts/qa/run_module_symbol_qa.py` | 移除 `--provider`，`--model` 指定 answer 模型 |
| `scripts/qa/run_concept_symbol_qa.py` | 移除硬编码 DeepSeek，`--model` 可多次指定 |
| `evals/eval_location.py` | 同样改走 `call_llm()` |

### 1.3 效果

切换模型只需改 `.env` 里的 `LLM_MODEL` 或在命令行传 `--model`：

```bash
# QA
python scripts/qa/run_module_symbol_qa.py --model glm-5.2 --workers 15
python scripts/qa/run_concept_symbol_qa.py --model glm-5.2 --workers 10

# Eval
python evals/eval_v2.py \
    --result results/qa_concept_symbol_glm-5.2.json \
    --benchmark datasets/benchmark_hard.json \
    --range all \
    --model gpt-4.1-mini \
    -o results/eval_concept_symbol_glm52_by_gpt41mini.json \
    -w 20
```

---

## 2. Embedding 接口统一封装

### 2.1 问题

Embedding 调用分散在三处，都直接构造 `OpenAI()`：
- `src/qa/retrievers/fast_embedding.py`
- `src/qa/retrievers/embedding.py`
- `src/search/semantic_search.py`

换成本地 BGE 等模型时要改多处。

### 2.2 改造

新增 `src/core/embedding_client.py`：
- `EmbeddingEncoder` 抽象基类
- `OpenAIEmbeddingEncoder`：当前默认后端
- `LocalEmbeddingEncoder`：本地后端占位（可接 BGE / sentence-transformers / onnx / vLLM）
- `get_encoder()` 工厂函数

三处调用全部替换为 `get_encoder().encode()` / `encode_single()`。

### 2.3 未来切换本地模型

方式一（环境变量）：
```bash
export EMBEDDING_BACKEND=local
export LOCAL_EMBEDDING_ENDPOINT=http://localhost:8000
```

方式二（自定义 encoder）：
```python
from src.core.embedding_client import EmbeddingEncoder
import numpy as np

class BGEEncoder(EmbeddingEncoder):
    def encode(self, texts: list[str]) -> np.ndarray:
        # 调用本地模型
        return np.asarray(..., dtype=np.float32)
```

---

## 3. Ingestion 并行化说明

### 3.1 问题

`scripts/ingestion/ingest_code.py` 是串行建图，LLM 问得对：确实慢。

### 3.2 已有并行版本

项目已有 `scripts/ingestion/ingest_parallel.py`，多进程 + 每个 worker 独立 clangd 分片解析文件。

### 3.3 改造

- 把 `ingest_parallel.py` 的临时缓存目录从默认 `/tmp` 改到 `/data/users/zzy/.tmp/`（遵守 AGENTS.md）。
- 把 `ingest_code.py` 和 `ingest_parallel.py` 的日志统一写到 `logs/ingestion_YYYYMMDD_HHMMSS.log`。
- 更新 `MANUAL_RUN.md`，推荐并行建图。

### 3.4 推荐命令

```bash
python scripts/ingestion/ingest_parallel.py --workers 8
```

---

## 4. glm-5.2 端到端 QA 实验结果

### 4.1 实验设置

- Benchmark：`datasets/benchmark_hard.json`（50 题）
- Judge 模型：统一用 `gpt-4.1-mini`（glm-5.2 / deepseek 自评不稳定，见 4.3）
- 对比方法：Module-Symbol vs Concept-Symbol
- 对比模型：glm-5.2、deepseek-v4-pro、deepseek-v4-flash

### 4.2 结果汇总

| 方法 | Answer Model | Binary Judge | Full Coverage | Avg Coverage |
|---|---|---|---|---|
| **Concept-Symbol** | **glm-5.2** | **49/50 = 98.0%** | **43/50 = 86.0%** | **88.2%** |
| Concept-Symbol | deepseek-v4-flash | 44/50 = 88.0% | 31/50 = 62.0% | 73.8% |
| Concept-Symbol | deepseek-v4-pro | 42/50 = 84.0% | 33/50 = 66.0% | 75.7% |
| Module-Symbol | glm-5.2 | 45/50 = 90.0% | 33/50 = 66.0% | 72.3% |
| Module-Symbol | deepseek-v4-pro | 46/50 = 92.0% | 32/50 = 64.0% | 72.2% |

### 4.3 关键发现

1. **glm-5.2 生成答案能力很强**
   - Concept-Symbol 下达到 98% binary / 86% full coverage，是本轮实验最优。

2. **Concept-Symbol 显著优于 Module-Symbol**
   - 同模型（glm-5.2）下，binary 提升 8 个点，full coverage 提升 20 个点。
   - 说明 concept 层比 module 层更适合作为 question → evidence 的桥梁。

3. **glm-5.2 不适合当 judge**
   - 自评 binary 仅 8.0%，且 citation judge 多次 JSON parse failed。
   - 评估固定用 gpt-4.1-mini 更稳定。

4. **deepseek 当 judge 严重低估实际表现**
   - deepseek-v3 judge 给 deepseek-v4-pro answer 仅 20-28% binary。
   - gpt-4.1-mini 重新评分为 84-88%。

### 4.4 输出文件

| 文件 | 说明 |
|---|---|
| `results/qa_module_top5_symbol_glm52.json` | Module-Symbol glm-5.2 QA 结果 |
| `results/eval_module_top5_symbol_glm52_by_gpt41mini.json` | Module-Symbol glm-5.2 评估结果 |
| `results/qa_concept_symbol_glm-5.2.json` | Concept-Symbol glm-5.2 QA 结果 |
| `results/eval_concept_symbol_glm52_by_gpt41mini.json` | Concept-Symbol glm-5.2 评估结果 |
| `results/eval_concept_symbol_deepseek_by_gpt41mini.json` | Concept-Symbol deepseek-v4-pro 重新评估 |
| `results/eval_concept_symbol_deepseek_flash_by_gpt41mini.json` | Concept-Symbol deepseek-v4-flash 重新评估 |

---

## 5. 文件与日志管理

### 5.1 清理

- 删除 `results/old/`（约 21MB，6 月 5 日及以前的临时/中间结果）。
- 当前 `results/` 剩余约 69MB，248 个文件。

### 5.2 日志策略

- `ingest_code.py` / `ingest_parallel.py` 日志统一写到 `logs/ingestion_YYYYMMDD_HHMMSS.log`。
- 避免再写 `/tmp`。

### 5.3 后续建议

- QA/eval 默认文件名保留模型名，避免覆盖，但也不生成过多带时间戳的中间文件。
- 中间 debug 文件默认不生成，需要时通过 `--debug` 开启。

---

## 6. 当前推荐命令速查

```bash
# 建图（推荐并行）
python scripts/ingestion/ingest_parallel.py --workers 8

# 抽象层构建
python scripts/ingestion/build_module_abstraction.py
python scripts/ingestion/build_concept_abstraction.py

# QA
python scripts/qa/run_module_symbol_qa.py --model glm-5.2 --workers 15
python scripts/qa/run_concept_symbol_qa.py --model glm-5.2 --workers 10

# Eval（固定 gpt-4.1-mini 当 judge）
python evals/eval_v2.py \
    --result results/qa_concept_symbol_glm-5.2.json \
    --benchmark datasets/benchmark_hard.json \
    --range all \
    --model gpt-4.1-mini \
    -o results/eval_concept_symbol_glm52_by_gpt41mini.json \
    -w 20
```

---

## 7. 下一步可能方向

1. **Concept-Symbol 已经验证有效**：可以深入分析为什么 concept 层比 module 层好，进一步细化 concept 构建策略。
2. **glm-5.2 值得继续用**：在 answer generation 上表现最优，成本也低。
3. **judge 模型固定 gpt-4.1-mini**：避免自评/跨模型 judge 的不稳定。
4. **Hierarchical Investigation Agent**：在 concept 层做 ReAct，再下钻到函数级，可能是下一步方向。
