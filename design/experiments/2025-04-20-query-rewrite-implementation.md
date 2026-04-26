# Query改写实现总结

## 实现状态 ✅ 已完成

### 核心组件

#### 1. QueryRewriter (`tools/search/query_rewriter.py`)

**功能**: 将自然语言问题转换为适合代码搜索的查询

```python
# 使用示例
from tools.search.query_rewriter import rewrite_query, get_grep_keywords

# 改写问题
result = rewrite_query("矩阵乘法的实现代码在哪里")
print(result.keywords)
# ['mat_mul', 'matrix multiplication', 'gemm', 'MMQ', 'MUL_MAT', ...]

# 获取Grep关键词
keywords = get_grep_keywords("KV缓存怎么实现")
print(keywords)
# ['cache', 'buffer', 'kv_cache', 'key_value_cache', ...]
```

**核心能力**:
- ✅ 中文→英文代码术语翻译（50+术语词典）
- ✅ 标识符提取（ggml_xxx, llama_xxx等）
- ✅ 缩写/别名扩展（KV缓存→kv_cache）
- ✅ 多策略搜索query生成

**术语词典示例**:
```python
TERM_DICT = {
    "矩阵乘法": ["mat_mul", "gemm", "MMQ", "MUL_MAT"],
    "KV缓存": ["kv_cache", "key_value_cache", "attn_cache"],
    "量化": ["quantize", "quant", "Q4", "Q8", "IQ4"],
    "注意力": ["attention", "attn", "flash_attn"],
    ...
}
```

#### 2. 集成V8框架 (`run_qa_v8_with_query_rewrite.py`)

**新增功能**:
```bash
# 启用Query改写的V8
python run_qa_v8_with_query_rewrite.py \
    --csv results/qav2_test.csv \
    --output results/v8_rewrite.json \
    --workers 20 \
    --rewrite  # 启用Query改写
```

**修改点**:
- `initial_search_with_rewrite()`: 新增Query改写步骤
- 先进行Query改写，提取关键词
- 使用改写后的关键词进行Grep搜索
- 合并语义搜索和Grep搜索结果

**搜索流程对比**:

```
传统V8:
问题 → 语义搜索(5个) → Grep Fallback(可选) → 文件扩展 → 答案

Query改写V8:
问题 → Query改写 → 语义搜索(5个) + Grep改写搜索(N个) → 文件扩展 → 答案
         ↓
    ["mat_mul", "gemm", "MMQ", "matrix multiplication"]
```

---

## 测试验证

### 单元测试

```bash
$ python3 tools/search/query_rewriter.py

问题: 矩阵乘法的实现代码在哪里
  关键词: ['mat_mul', 'matrix multiplication', 'gemm', 'MMQ', 'MUL_MAT', 'matrix']

问题: KV缓存怎么实现
  关键词: ['cache', 'buffer', 'kv_cache', 'key_value_cache', 'attn_cache']

问题: 模型加载的入口函数
  关键词: ['model_load', 'llama_model_load', 'load_model', 'load_checkpoint']
```

### 集成测试

```bash
# 3题快速测试
问题 0: ggml-blas 主要包含哪些核心功能
  传统语义搜索: 5个
  Query改写Grep: +4个
  合并结果: 9个

问题 1: apertus 包含哪些子模块
  传统语义搜索: 5个
  Query改写Grep: +1个
  合并结果: 6个

问题 2: llama-grammar 的内部结构
  传统语义搜索: 5个
  Query改写Grep: +2个
  合并结果: 7个
```

---

## 预期效果

### 召回率提升

| 问题类型 | 传统V8召回 | Query改写V8 | 提升 |
|---------|-----------|-------------|------|
| 中文概念类 | 5个 | 8-12个 | **+60%** |
| 具体函数名 | 5个 | 6-8个 | +20% |
| 缩写/别名类 | 3个 | 8-10个 | **+150%** |

### 准确率预期

基于V7(71.9%)→V8(77.2%)的经验，Query改写可能带来：
- 中文描述类问题准确率: +10-15%
- 整体准确率: +2-4%
- 预计目标: **79-81%**

---

## 使用方式

### 方式1: 命令行

```bash
# 测试Query改写版本（推荐先跑50题测试）
python experiments/module_expansion/run_qa_v8_with_query_rewrite.py \
    --csv results/qav2_test.csv \
    --output results/v8_rewrite_50.json \
    --workers 20 \
    --rewrite

# 对比Baseline
python experiments/module_expansion/run_qa_v8_with_query_rewrite.py \
    --csv results/qav2_test.csv \
    --output results/v8_baseline.json \
    --workers 20
```

### 方式2: 代码调用

```python
from tools.search.query_rewriter import rewrite_query

# 改写问题
result = rewrite_query("矩阵乘法的实现")
print(result.keywords)  # ['mat_mul', 'gemm', 'MMQ', ...]

# 用于Grep搜索
from tools.search import grep_codebase
for keyword in result.keywords[:5]:
    results = grep_codebase(keyword)
    # 处理结果...
```

---

## 后续优化方向

### Phase 1: 术语词典扩展（1周）
- 从llama.cpp代码库自动挖掘术语
- 社区共建术语映射
- 添加更多领域特定术语

### Phase 2: LLM增强改写（1周）
- 实现LLMQueryRewriter
- 对复杂问题进行语义理解
- 处理词典未覆盖的新术语

### Phase 3: 在线学习（2周）
- 收集改写效果反馈
- 自动优化术语权重
- 个性化改写策略

---

## 文件清单

| 文件 | 说明 |
|-----|------|
| `tools/search/query_rewriter.py` | QueryRewriter核心实现 |
| `experiments/module_expansion/run_qa_v8_with_query_rewrite.py` | 集成Query改写的V8脚本 |
| `design/experiments/2025-04-20-query-rewrite-analysis.md` | 方案设计文档 |
| `design/experiments/2025-04-20-query-rewrite-implementation.md` | 本文件 |

---

## 下一步行动

1. **小规模测试**: 在50题上测试Query改写版本 vs Baseline
2. **效果评估**: 对比准确率、召回率、时延
3. **全量测试**: 如果效果良好，运行360题全量测试
4. **术语优化**: 根据失败case扩展术语词典

是否现在运行50题对比测试？预计耗时约15-20分钟。
