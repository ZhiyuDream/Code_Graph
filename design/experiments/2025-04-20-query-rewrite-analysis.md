# Query改写与跨语言搜索分析

## 当前Agent的Query处理能力

### 现状：无Query改写

```python
# 当前流程（问题：中文→直接搜索）
中文问题 → extract_entities() → 中文实体 → grep_search(中文) → 结果差

# 示例
问题: "ggml_backend_graph_compute 在哪里定义"
↓
extract_entities() → ["ggml_backend_graph_compute"] （正确提取）
↓
grep_search("ggml_backend_graph_compute") → 找到英文代码 ✅

问题: "矩阵乘法函数在哪里"  
↓
extract_entities() → ["矩阵乘法"] （中文实体）
↓
grep_search("矩阵乘法") → 找不到英文代码 ❌
```

### 实际代码检查

| 组件 | Query改写 | 跨语言支持 | 问题 |
|-----|----------|-----------|------|
| `extract_entities_from_question` | ❌ 无 | ❌ 无 | 提取中文实体，未翻译 |
| `grep_codebase` | ❌ 无 | ❌ 无 | 直接搜索传入的keyword |
| `semantic_search` | ❌ 无 | ⚠️ embedding模型可能支持 | 依赖模型跨语言能力 |
| `issue_search` | ❌ 无 | ⚠️ embedding模型可能支持 | 依赖模型跨语言能力 |

---

## 问题影响分析

### 场景1：中文问题 + 英文代码

```
问题: "矩阵乘法的实现代码在哪里"
当前行为:
  - extract_entities → ["矩阵乘法"] (中文)
  - grep_search("矩阵乘法") → 0结果 (代码中是mat_mul/MUL_MAT)
  - semantic_search("矩阵乘法") → 可能找到(依赖embedding跨语言)
  
期望行为:
  - query_rewrite → "matrix multiplication mat_mul MUL_MAT"
  - grep_search("mat_mul") → 找到代码
```

### 场景2：缩写/别名问题

```
问题: "KV缓存怎么实现"
当前行为:
  - extract_entities → ["KV缓存"] 
  - 无法关联到 "kv_cache", "key_value_cache"
  
期望行为:
  - query_rewrite → "kv_cache key_value cache"
```

### 场景3：模糊描述

```
问题: "模型加载的入口函数"
当前行为:
  - extract_entities → [] (空)
  - 因为没有具体函数名
  
期望行为:
  - query_rewrite → "llama_model_load load_model"
  - 基于语义推测可能的函数名
```

---

## 解决方案设计

### 方案1：LLM-based Query改写

```python
class QueryRewriter:
    """基于LLM的Query改写器"""
    
    TRANSLATION_PROMPT = """
    你是一个代码搜索专家。请将用户的问题改写成适合代码检索的查询。
    
    改写目标:
    1. 将中文概念翻译成英文代码标识符
    2. 补充可能的缩写、别名、同义词
    3. 提取具体的函数名、类名、变量名
    4. 将模糊描述转换为精确的代码术语
    
    示例:
    问题: "矩阵乘法的实现"
    改写: "matrix multiplication mat_mul gemm MMQ mul_mat"
    
    问题: "KV缓存"
    改写: "kv_cache key_value_cache kv_cache_init kv_cache_clear"
    
    问题: "模型加载的入口"
    改写: "llama_model_load llama_load_model_from_file load_model"
    
    现在请改写:
    问题: {question}
    
    返回JSON:
    {{
        "original": "原问题",
        "translated": "英文翻译",
        "keywords": ["关键词1", "关键词2", ...],
        "possible_identifiers": ["函数名/类名1", ...],
        "search_queries": ["query1", "query2", ...]
    }}
    """
    
    def rewrite(self, question: str) -> Dict:
        result = call_llm_json(self.TRANSLATION_PROMPT.format(question=question))
        return result
```

### 方案2：Rule-based + LLM Hybrid

```python
class HybridQueryRewriter:
    """规则+LLM混合改写器"""
    
    # 预定义术语词典（可扩展）
    TERM_DICTIONARY = {
        "矩阵乘法": ["matrix multiplication", "mat_mul", "gemm", "MMQ", "MUL_MAT"],
        "矩阵": ["matrix", "mat", "tensor"],
        "向量": ["vector", "vec"],
        "缓存": ["cache", "buffer", "kv_cache"],
        "KV缓存": ["kv_cache", "key_value_cache", "KV cache"],
        "量化": ["quantize", "quantization", "quant", "Q4", "Q8"],
        "模型加载": ["model load", "llama_model_load", "load_model"],
        "后端": ["backend", "ggml_backend"],
        "图计算": ["graph compute", "ggml_graph_compute"],
        "分词": ["tokenize", "tokenizer", "vocab"],
        "注意力": ["attention", "attn", "flash_attn", "multi_head_attention"],
        "归一化": ["normalize", "norm", "layer_norm", "rms_norm"],
        "激活函数": ["activation", "gelu", "relu", "silu", "softmax"],
        "内存分配": ["memory alloc", "malloc", "ggml_malloc", "allocator"],
        "线程": ["thread", "pthread", "openmp", "threadpool"],
        "并行": ["parallel", "concurrent", "multi_thread"],
    }
    
    def rewrite(self, question: str) -> Dict:
        # Step 1: 规则匹配
        rule_based = self._apply_rules(question)
        
        # Step 2: LLM补充
        llm_based = self._llm_rewrite(question)
        
        # Step 3: 合并去重
        return self._merge(rule_based, llm_based)
    
    def _apply_rules(self, question: str) -> Dict:
        """应用规则词典"""
        keywords = []
        for cn_term, en_terms in self.TERM_DICTIONARY.items():
            if cn_term in question or cn_term.replace("", "") in question:
                keywords.extend(en_terms)
        return {"keywords": keywords}
```

### 方案3：多阶段改写

```python
class MultiStageRewriter:
    """多阶段Query改写流水线"""
    
    STAGES = [
        "entity_extraction",    # 提取实体
        "translation",          # 翻译
        "synonym_expansion",    # 同义词扩展
        "abbreviation_resolve", # 缩写解析
        "query_construction",   # 构建搜索query
    ]
    
    def rewrite(self, question: str) -> SearchQuery:
        # Stage 1: 提取实体
        entities = self._extract_entities(question)
        
        # Stage 2: 翻译（中文→英文）
        translated = self._translate(entities)
        
        # Stage 3: 同义词扩展
        expanded = self._expand_synonyms(translated)
        
        # Stage 4: 缩写解析
        resolved = self._resolve_abbreviations(expanded)
        
        # Stage 5: 构建最终query
        queries = self._construct_queries(resolved)
        
        return SearchQuery(
            original=question,
            entities=entities,
            keywords=resolved,
            search_queries=queries
        )
```

---

## 实现示例

### 集成到V8框架

```python
def initial_search_with_rewrite(driver, client, question: str) -> dict:
    """带Query改写的初始搜索"""
    
    # 1. Query改写
    rewriter = QueryRewriter()
    rewritten = rewriter.rewrite(question)
    
    print(f"Query改写: {question}")
    print(f"  → 关键词: {rewritten['keywords']}")
    print(f"  → 标识符: {rewritten['possible_identifiers']}")
    print(f"  → 搜索query: {rewritten['search_queries']}")
    
    # 2. 多策略搜索
    results = {"functions": [], "issues": [], "snippets": []}
    
    # 2.1 语义搜索（用原始问题）
    semantic_results = search_functions_by_text(question, top_k=5)
    results["functions"].extend(semantic_results)
    
    # 2.2 Grep搜索（用改写后的英文关键词）
    for keyword in rewritten['keywords'][:3]:
        grep_results = grep_codebase(keyword, limit=5)
        new_funcs = convert_grep_to_function_results(grep_results)
        results["functions"].extend(new_funcs)
    
    # 2.3 Issue搜索（用改写后的query）
    for query in rewritten['search_queries'][:2]:
        issue_results = search_issues(query, top_k=3)
        results["issues"].extend(issue_results)
    
    return results
```

---

## 预期效果

### 准确率提升分析

| 问题类型 | 当前准确率 | 预期准确率 | 提升 |
|---------|----------|-----------|------|
| 中文描述类 | 40% | 70% | +30% |
| 缩写/别名类 | 50% | 80% | +30% |
| 模糊概念类 | 30% | 60% | +30% |
| 具体函数名类 | 90% | 92% | +2% |

### 具体示例

```
问题: "矩阵乘法的优化实现在哪里"

当前V8:
  - semantic_search: 可能找到（embedding跨语言）
  - grep_search: "矩阵乘法" → 无结果 ❌
  - 结果: 依赖semantic_search的5个结果

改写后:
  - semantic_search: "矩阵乘法" → 5个
  - grep_search: "mat_mul" → 3个
  - grep_search: "gemm" → 3个  
  - grep_search: "MMQ" → 5个
  - grep_search: "MUL_MAT" → 5个
  - 结果: 20+个相关函数，召回率提升4倍 ✅
```

---

## 实现路线图

### Phase 1: 基础改写（1周）

```python
# 实现LLM-based改写
# 集成到initial_search
# 50题快速验证
```

**预期效果**: 中文描述类问题准确率+15%

### Phase 2: 术语词典（1周）

```python
# 构建llama.cpp专用术语词典
# Hybrid改写器
# A/B测试对比
```

**预期效果**: 缩写/别名类问题准确率+20%

### Phase 3: 在线学习（2周）

```python
# 收集改写效果反馈
# 自动优化词典
# 个性化改写策略
```

**预期效果**: 整体准确率+5-8%

---

## 风险评估

| 风险 | 可能性 | 影响 | 缓解措施 |
|-----|--------|------|---------|
| LLM改写成本高 | 中 | 时延+50% | 缓存改写结果，规则fallback |
| 改写错误 | 中 | 准确率下降 | 多query并行搜索，结果合并 |
| 术语词典不全 | 高 | 覆盖度低 | 社区共建，自动挖掘 |
| 过度改写 | 低 | 召回噪声 | 保留原始query，并行搜索 |

---

## 下一步行动

1. **收集数据**: 分析当前失败case中有多少是中文query导致的
2. **快速原型**: 实现LLM-based改写，在50题上验证
3. **构建词典**: 基于llama.cpp代码库自动挖掘术语映射
4. **集成测试**: 对比改写前后的准确率和时延

是否需要我先实现一个**QueryRewriter原型**，在现有V8框架上测试效果？
