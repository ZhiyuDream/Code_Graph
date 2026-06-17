# 代码架构重构说明

## 目录结构

```
Code_Graph/
├── scripts/                    # 可执行脚本
│   ├── run_qa_final.py        # ⭐ 最终版QA脚本（使用新架构）
│   ├── run_qa_v7_p0_improved.py  # 旧版（保留备份）
│   └── ...                    # 其他工具脚本
│
├── tools/                      # 工具模块
│   ├── core/                  # 核心基础设施
│   │   ├── __init__.py
│   │   ├── neo4j_client.py    # Neo4j数据库客户端
│   │   ├── llm_client.py      # LLM调用客户端
│   │   └── answer_generator.py # 答案生成器
│   │
│   ├── search/                # 搜索工具
│   │   ├── __init__.py
│   │   ├── semantic_search.py # 语义搜索（Embedding）
│   │   ├── call_chain.py      # 调用链分析
│   │   ├── issue_search.py    # Issue/PR搜索
│   │   └── grep_search.py     # Grep关键词搜索
│   │
│   └── ...                    # 其他工具
│
└── src/                       # 源码目录
```

## 模块说明

### tools.core - 核心基础设施

| 文件 | 功能 | 对外接口 |
|------|------|----------|
| `neo4j_client.py` | Neo4j数据库连接和查询 | `get_neo4j_driver()`, `run_cypher()` |
| `llm_client.py` | LLM调用（带重试） | `call_llm()`, `call_llm_json()` |
| `answer_generator.py` | 答案生成 | `generate_answer()` |

### tools.search - 搜索工具

| 文件 | 功能 | 对外接口 |
|------|------|----------|
| `semantic_search.py` | 基于Embedding的语义搜索 | `search_functions_by_text()` |
| `call_chain.py` | 调用链扩展（callers/callees） | `get_callers()`, `get_callees()` |
| `issue_search.py` | GitHub Issue/PR搜索 | `search_issues()` |
| `grep_search.py` | 基于关键词的代码搜索 | `grep_codebase()` |

## 使用方式

### 导入示例

```python
# 导入核心模块
from tools.core import get_neo4j_driver, call_llm, generate_answer

# 导入搜索模块
from tools.search import search_functions_by_text, get_callers
```

### 运行脚本

```bash
# 使用重构后的脚本
python scripts/run_qa_final.py \
    --csv results/qav2_test.csv \
    --output results/final.json \
    --workers 20
```

## 删除的文件

清理了以下冗余脚本：
- `run_qa_v2.py` ~ `run_qa_v6_hybrid.py` - 旧版本迭代
- `run_qa_v7_*.py` (多个变体) - 合并为最终版本
- `run_qa_deprecated.py` - 废弃代码

## 优势

1. **模块化**：每个工具独立成文件，职责清晰
2. **可复用**：统一接口，避免重复实现
3. **可维护**：修改单个工具不影响其他部分
4. **易测试**：每个模块可独立测试
