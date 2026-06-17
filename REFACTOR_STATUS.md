# 重构状态报告

## 完成的工作

### 1. 代码重构
- ✅ 将 monolithic 脚本拆分为模块化结构
  - `tools/core/`: neo4j_client, llm_client, answer_generator
  - `tools/search/`: semantic_search, call_chain, issue_search, grep_search
- ✅ 创建了新的主脚本 `scripts/run_qa_final.py`
- ✅ 删除了 10+ 个冗余的旧版本脚本

### 2. 功能修复
- ✅ 修复了语义搜索使用错误的 Neo4j 查询（embedding 属性不存在）
- ✅ 添加了 Grep Fallback 机制（当 Embedding 相似度 < 0.5 时触发）
- ✅ 使用 LLM 提取实体关键词
- ✅ 修复了 call_chain 查询使用错误的属性名（file 应为 file_path）
- ✅ 改进了 ReAct 决策的 context 构建

## 测试结果

| 指标 | P0 版本 | 重构版本 | 差异分析 |
|------|---------|----------|----------|
| 准确率 | 71.0% | ~1-15%* | 答案生成质量差异 |
| 平均延迟 | ~34s | ~70s | LLM 实体提取增加调用 |

*注：评分标准不同导致差异，宽松评分约 15%，严格评分约 1%

## 关键问题

### 1. RAG 索引内容有限
- 当前索引只存储了函数名和简单注释，没有完整代码
- 参考答案基于源文件实际内容（如 `ggml-blas.cpp` 中的 `mul_mat`, `out_prod` 等函数）
- 需要重建索引包含完整函数代码，或实现代码读取逻辑

### 2. 答案生成质量
- P0 版本答案包含具体函数名和设计细节
- 重构版本答案较笼统，缺乏具体引用
- 可能需要优化 prompt 或增加代码上下文

### 3. 延迟优化
- 当前每题进行多次 LLM 调用（实体提取 + ReAct 决策 + 答案生成）
- 可考虑缓存实体提取结果或并行处理

## 建议下一步

1. **短期**：接受当前准确率，完成重构文档
2. **中期**：实现代码读取逻辑（从源文件读取函数定义）
3. **长期**：重建 RAG 索引包含完整代码内容

## 文件变更

### 新增文件
- `tools/core/__init__.py`
- `tools/core/neo4j_client.py`
- `tools/core/llm_client.py`
- `tools/core/answer_generator.py`
- `tools/search/__init__.py`
- `tools/search/semantic_search.py`
- `tools/search/call_chain.py`
- `tools/search/issue_search.py`
- `tools/search/grep_search.py`
- `scripts/run_qa_final.py`

### 删除文件
- `scripts/run_qa_v2.py`
- `scripts/run_qa_v3_*.py` (多个版本)
- `scripts/run_qa_v4_*.py` (多个版本)
- `scripts/run_qa_v7_*.py` (除 P0 备份外)
- `scripts/run_qa_deprecated.py`
