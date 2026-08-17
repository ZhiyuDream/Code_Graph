# Code_Graph 手动运行手册

本手册说明如何从零开始手动跑通当前主流程，以及如何把 LLM / Embedding API 替换成本地部署模型。

> 适用分支：`feat/navigation-architecture` 及之后基于 Module/Concept Abstraction 的版本。
> 目标仓库：以 [llama.cpp](https://github.com/ggerganov/llama.cpp) 为例。

---

## 1. 环境准备

### 1.1 Python 环境

```bash
conda create -n code_graph python=3.11
conda activate code_graph
pip install -r requirements.txt
```

若系统没有 libclang，可任选其一：

```bash
# 方案 A：conda 安装
conda install -c conda-forge libclang

# 方案 B：系统 clang（示例为 Ubuntu）
sudo apt install libclang-14-dev
export LIBCLANG_PATH=/usr/lib/llvm-14/lib/libclang.so.1
```

### 1.2 Neo4j

需要本地或远程 Neo4j 实例，并创建好数据库（默认 `neo4j`）。

### 1.3 配置 .env

项目根目录已有 `.env.example`，复制为 `.env` 并填写：

```bash
cp .env.example .env
```

关键字段说明见下表。

| 变量 | 说明 | 示例 |
|---|---|---|
| `NEO4J_URI` | Neo4j Bolt 地址 | `neo4j://localhost:7687` |
| `NEO4J_USERNAME` | 用户名 | `neo4j` |
| `NEO4J_PASSWORD` | 密码 | `your_password` |
| `REPO_ROOT` | 目标仓库绝对路径 | `/data/users/zzy/RUC/llama.cpp` |
| `COMPILE_COMMANDS_DIR` | 含 `compile_commands.json` 的目录 | `/data/users/zzy/RUC/llama.cpp/build` |
| `OPENAI_API_KEY` | OpenAI 或兼容服务的 Key | `sk-...` |
| `OPENAI_BASE_URL` | 自定义 OpenAI 兼容端点；留空则用官方 | `https://api.openai.com/v1` |
| `LLM_MODEL` | 默认生成答案/评估模型 | `gpt-4o-mini` |
| `EMBEDDING_MODEL` | Embedding 模型 | `text-embedding-3-small` |
| `DEEPSEEK_API_KEY` | DeepSeek Key | `sk-...` |
| `DEEPSEEK_BASE_URL` | DeepSeek 端点 | `https://api.deepseek.com/v1` |

> **注意**：根目录下的 `env.example.yml` 是 conda 环境说明，不是 `.env` 模板。请以 `.env.example` 为准。

### 1.4 生成 compile_commands.json

```bash
cd $REPO_ROOT
mkdir -p build && cd build
cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON ..
```

确保 `build/compile_commands.json` 已生成。

---

## 2. 完整数据流命令

整体顺序：

```text
代码摄取 → 建 Module 抽象 → 建 Concept 抽象 → 建 Embedding 索引 → QA 生成答案 → 评估
```

### 2.1 代码摄取（clangd → Neo4j）

推荐并行版本（快很多）：

```bash
python scripts/ingestion/ingest_parallel.py --workers 8
```

- 默认自动选择 worker 数（最多 8，不超过 CPU 核心数，每 worker 至少 10 个文件）。
- 每个 worker 独立启动一个 clangd 实例，分片解析文件。
- 临时缓存目录放在 `/data/users/zzy/.tmp/`（遵守 AGENTS.md）。
- 日志写入 `logs/ingestion_parallel_YYYYMMDD_HHMMSS.log`。

如果不需要并行或调试问题，也可以用串行版本：

```bash
python scripts/ingestion/ingest_code.py
```

- 依赖 `REPO_ROOT` 和 `COMPILE_COMMANDS_DIR`。
- 依赖 **clangd 20+**；clangd 14–19 会产生空 `CALLS` 边。
- 日志写入 `logs/ingestion_YYYYMMDD_HHMMSS.log`。

### 2.2 构建 Module Abstraction

```bash
python scripts/ingestion/build_module_abstraction.py
```

输出：`data/module_abstraction.json`。

构建参数（在脚本内硬编码）：

- `model="gpt-4.1-mini"`：给 module 命名/总结用的 LLM。
- `resolution=0.5`：Louvain 社区发现分辨率。
- `min_module_size=10`：最小 module 大小。
- `same_file_weight=0.5`：同文件加权。

### 2.3 构建 Concept Abstraction

```bash
python scripts/ingestion/build_concept_abstraction.py
```

输出：`data/concept_abstraction.json`。

依赖 `data/module_abstraction.json`，若不存在会自动先构建。

构建参数：

- `model="gpt-4.1-mini"`。
- `sub_resolution=2.0`。
- `min_concept_size=3`。
- `same_file_weight=0.3`。

### 2.4 构建 Embedding 索引

当前仓库**没有独立的 embedding 索引构建脚本**。推荐用下面最小复用脚本，存为 `scripts/ingestion/build_embedding_index.py`：

```python
#!/usr/bin/env python3
from pathlib import Path
from neo4j import GraphDatabase

import sys
_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from config import NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD, NEO4J_DATABASE
from src.qa.retrievers.embedding import EmbeddingRetriever


def main():
    driver = GraphDatabase.driver(
        NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD)
    )
    retriever = EmbeddingRetriever()
    retriever.build_index(
        driver=driver,
        database=NEO4J_DATABASE,
        repo_root="",   # 留空会退化为相对路径，建议填 REPO_ROOT 绝对路径
        force=True,
    )
    driver.close()


if __name__ == "__main__":
    main()
```

运行：

```bash
python scripts/ingestion/build_embedding_index.py
```

输出：`data/qa_embedding_index.json`。

> 如果不想新增脚本，也可临时复用旧脚本：
> ```bash
> python scripts/archive/run_qa_benchmark.py --build_emb_index --retriever embedding ...
> ```
> 但该脚本已归档，不推荐作为常规流程。

### 2.5 运行端到端 QA

#### Module-Symbol 方法

```bash
python scripts/qa/run_module_symbol_qa.py \
    --mode module_top5_symbol \
    --model glm-5.2 \
    --workers 15 \
    --output results/qa_module_top5_symbol_glm52.json
```

可选 `--mode`：`baseline`、`module_top5_symbol`、`module_top10_symbol`。

`--model` 留空则默认使用 `.env` 中的 `LLM_MODEL`；若 `LLM_MODEL` 也未设置，回退到 `deepseek-v4-pro`。

默认输出：`results/qa_{mode}_{model}.json`。

#### Concept-Symbol 方法

```bash
# 默认使用 LLM_MODEL（单个模型）
python scripts/qa/run_concept_symbol_qa.py

# 指定模型，可多次指定跑多个模型
python scripts/qa/run_concept_symbol_qa.py \
    --model glm-5.2 \
    --model deepseek-v4-pro
```

输出：`results/qa_concept_symbol_{model}.json`。

> 若输出文件已存在，脚本会跳过。需要重跑时请手动删除对应文件。

### 2.6 评估

```bash
python evals/eval_v2.py \
    --result results/qa_concept_symbol_glm-5.2.json \
    --benchmark datasets/benchmark_hard.json \
    --range all \
    --model glm-5.2 \
    -o results/eval_concept_symbol_glm52.json \
    -w 20
```

`--model` 留空则默认使用 `LLM_MODEL` 环境变量，否则回退到 `gpt-4.1-mini`。

---

## 3. 单独跑一个实验（最小示例）

如果前面所有数据产物（`data/*.json`）已经存在，只想跑一次 Concept-Symbol QA：

```bash
python scripts/qa/run_concept_symbol_qa.py
```

然后评估：

```bash
python evals/eval_v2.py \
    --result results/qa_concept_symbol_deepseek.json \
    --benchmark datasets/benchmark_hard.json \
    --range all \
    -o results/eval_concept_symbol_deepseek.json \
    -w 20
```

---

## 4. LLM API 替换指南

### 4.1 当前架构

LLM 调用已经过统一层：`src/core/llm_client.py` + `src/core/model_config.py`。

- `ModelRegistry.resolve(model_name)` 返回 `ModelConfig`，包含 provider / api_key / base_url / max_tokens 字段等。
- `call_llm()` / `call_llm_json()` 是业务代码推荐使用的入口。

新增模型只需在 `ModelRegistry._init()` 里注册，或在调用时由名称自动推断（OpenAI / DeepSeek）。

### 4.2 替换成 OpenAI 兼容端点（中转 / vLLM / Ollama）

只要服务兼容 OpenAI Chat Completions API：

```bash
# .env
OPENAI_API_KEY=your_key
OPENAI_BASE_URL=https://your-endpoint/v1
LLM_MODEL=your-model-name
```

代码无需修改（前提是走 `call_llm()` / `call_llm_json()`）。

### 4.3 替换成非 OpenAI 兼容的本地模型

如果需要自定义 HTTP 调用，建议只改一处：`src/core/llm_client.py`。

步骤：

1. 在 `src/core/model_config.py` 的 `ModelRegistry._init()` 里注册新 provider，例如 `local`：

```python
ModelConfig(
    name="qwen2.5-coder-32b",
    provider="local",
    api_key="unused",
    base_url="http://localhost:8000/v1",
    max_tokens_param="max_tokens",
    supports_json_format=True,
)
```

2. 在 `src/core/llm_client.py` 的 `_get_client()` 或 `call_llm()` 里增加分支。例如：

```python
def _get_client(cfg: ModelConfig) -> OpenAI:
    if cfg.provider == "local":
        return OpenAI(api_key="dummy", base_url=cfg.base_url)
    # ... 原有逻辑
```

3. 业务代码保持 `call_llm(..., model="qwen2.5-coder-32b")` 不变。

### 4.4 重要例外（已修复）

~~当前有两个脚本没有走 `call_llm()`，而是自己根据 `--provider` 构造 `OpenAI(...)`：~~

`scripts/qa/run_module_symbol_qa.py` 和 `scripts/qa/run_concept_symbol_qa.py` 已改造为统一走 `call_llm()`，通过 `--model` 指定任意模型即可，`ModelRegistry` 会自动解析 provider / api_key / base_url。

### 4.5 现在只需要 `--model`

生成答案：

```bash
python scripts/qa/run_module_symbol_qa.py --model glm-5.2 --workers 15
```

评估：

```bash
python evals/eval_v2.py --result results/qa_xxx.json --model glm-5.2 -o results/eval_xxx.json
```

不再需要 `--provider openai` / `--provider deepseek` 这种硬编码分支。

---

## 5. Embedding API 替换指南

### 5.1 当前调用点

Embedding 调用分散在几处，没有统一封装：

| 文件 | 用途 |
|---|---|
| `src/qa/retrievers/fast_embedding.py:58` | `FastEmbeddingRetriever.encode_queries()` 对 question 编码 |
| `src/qa/retrievers/embedding.py:48, 153` | `EmbeddingRetriever` 构建/查询索引 |
| `src/search/semantic_search.py:30` | `get_embedding()` 在线语义搜索 |

它们都直接创建 `OpenAI(...)` 并调用 `client.embeddings.create(model=EMBEDDING_MODEL, ...)`。

### 5.2 统一封装已存在

项目已新增 `src/core/embedding_client.py`，提供：

- `EmbeddingEncoder` 抽象基类
- `OpenAIEmbeddingEncoder`：当前默认后端，调用 OpenAI 兼容 embedding API
- `LocalEmbeddingEncoder`：本地后端占位，可接 BGE / sentence-transformers / onnx / vLLM 等
- `get_encoder(model=None, backend=None)`：工厂函数

所有 embedding 调用已迁移到统一接口：

| 文件 | 改动 |
|---|---|
| `src/qa/retrievers/fast_embedding.py` | `encode_queries()` 走 `get_encoder()` |
| `src/qa/retrievers/embedding.py` | `build_index()` / `retrieve()` 走 `_encoder` |
| `src/search/semantic_search.py` | `get_embedding()` 走 `get_encoder()` |

### 5.3 替换成 BGE / 本地模型

方式一：通过环境变量切换（不改代码）

```bash
export EMBEDDING_BACKEND=local
export LOCAL_EMBEDDING_ENDPOINT=http://localhost:8000
export LOCAL_EMBEDDING_MODEL_PATH=/path/to/bge-model
```

方式二：直接调用 `LocalEmbeddingEncoder`

```python
from src.core.embedding_client import LocalEmbeddingEncoder

encoder = LocalEmbeddingEncoder(
    endpoint="http://localhost:8000",
    # model_path="/path/to/bge-model",
)
embs = encoder.encode(["query 1", "query 2"])
```

方式三：自定义后端

继承 `EmbeddingEncoder`：

```python
from src.core.embedding_client import EmbeddingEncoder
import numpy as np

class BGEEncoder(EmbeddingEncoder):
    def __init__(self, model_path: str):
        self.model_path = model_path
        # 加载模型...

    def encode(self, texts: list[str]) -> np.ndarray:
        # 调用本地模型编码
        return np.asarray(..., dtype=np.float32)
```

然后在 `get_encoder()` 中注册，或业务代码直接实例化。

### 5.4 最小修改路径（不改架构）

如果只想临时把 embedding 指向本地 OpenAI 兼容服务，改 `.env` 即可：

```bash
OPENAI_API_KEY=dummy
OPENAI_BASE_URL=http://localhost:8000/v1
EMBEDDING_MODEL=bge-m3
```

### 5.4 注意维度一致性

`data/qa_embedding_index.json` 里的向量维度和查询编码器的输出维度必须一致。若更换 embedding 模型，需要**重新构建索引**。

---

## 6. 已知问题与注意事项

1. **`env.example.yml` 不是 `.env` 模板**。它是 conda 环境说明，已造成过误解。请使用 `.env.example`。
2. **无独立 embedding 索引构建脚本**。目前需要临时脚本或复用归档脚本，建议后续补充 `scripts/ingestion/build_embedding_index.py`。
3. **`ingest_code.py` 日志写 `/tmp/`**。违反项目 `AGENTS.md` 的磁盘规则；大仓库摄取时若 `/tmp` 满会失败。
4. ~~QA 脚本绕过 `ModelRegistry`~~。已改造完成，`run_module_symbol_qa.py` 和 `run_concept_symbol_qa.py` 现在统一走 `call_llm()`。
5. **Concept QA 脚本会跳过已存在输出**。重跑前检查 `results/qa_concept_symbol_*.json` 是否存在。

---

## 7. 常用命令速查

```bash
# 全量重建（推荐并行建图）
python scripts/ingestion/ingest_parallel.py --workers 8
python scripts/ingestion/build_module_abstraction.py
python scripts/ingestion/build_concept_abstraction.py
python scripts/ingestion/build_embedding_index.py   # 需自行创建

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

## 8. 文件对应关系

| 产物 | 路径 | 生成脚本 |
|---|---|---|
| Neo4j 代码图 | Neo4j DB | `scripts/ingestion/ingest_code.py` |
| Module 抽象 | `data/module_abstraction.json` | `scripts/ingestion/build_module_abstraction.py` |
| Concept 抽象 | `data/concept_abstraction.json` | `scripts/ingestion/build_concept_abstraction.py` |
| Embedding 索引 | `data/qa_embedding_index.json` | `scripts/ingestion/build_embedding_index.py`（需自建） |
| QA 答案 | `results/qa_*.json` | `scripts/qa/run_*_qa.py` |
| 评估结果 | `results/eval_*.json` | `evals/eval_v2.py` |
