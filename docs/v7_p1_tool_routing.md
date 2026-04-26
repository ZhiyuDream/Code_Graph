# V7 P1 改进：智能工具路由

## 问题
当前 V7 P0 使用固定阈值触发 Grep Fallback：
```python
if max_embedding_score < 0.5:  # 固定阈值
    trigger_grep_fallback()
```

问题：
- 有些问题即使 Embedding 分数高，也需要 Grep（如找具体函数实现）
- 有些问题即使 Embedding 分数低，也不需要 Grep（如开放性问题）

## 方案：LLM 决策工具路由

让 LLM 根据问题特征决定检索策略：

```python
def llm_route_decision(client, question: str) -> Dict:
    """
    LLM 决定使用哪些工具进行检索
    """
    prompt = f"""分析问题特征，决定最佳的代码检索策略。

问题: {question}

可选策略:
1. "semantic" - Embedding语义搜索（适合概念类、设计类问题）
2. "grep" - 精确匹配搜索（适合找具体函数、变量定义）
3. "graph" - 图遍历查询（适合调用链、依赖关系问题）
4. "hybrid" - 组合策略（多个工具并行）

分析要点:
- 问题是否包含具体的函数名/类名/变量名？
- 问题是否在问调用流程、执行顺序？
- 问题是否在问设计原因、实现原理？
- 问题的模糊程度如何？

返回JSON:
{{
    "analysis": "问题分析...",
    "primary_strategy": "semantic|grep|graph|hybrid",
    "secondary_strategy": "semantic|grep|graph|none",
    "entities": ["提取的函数名/类名"],
    "confidence": 0.8
}}

只输出JSON:"""
    
    resp = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=200,
        timeout=10
    )
    
    text = resp.choices[0].message.content.strip()
    # 提取JSON...
    return json.loads(text)


def smart_search(client, driver, question: str) -> Dict:
    """
    根据LLM决策执行智能检索
    """
    # Step 1: LLM 决策路由
    route = llm_route_decision(client, question)
    primary = route.get("primary_strategy")
    secondary = route.get("secondary_strategy")
    entities = route.get("entities", [])
    
    results = {"functions": [], "issues": [], "sources": []}
    
    # Step 2: 执行主要策略
    if primary == "grep" and entities:
        # Grep 精确搜索
        for entity in entities[:2]:
            grep_results = grep_search(entity)
            results["functions"].extend(grep_results)
        results["sources"].append("grep")
        
    elif primary == "graph":
        # 图遍历查询
        graph_results = graph_traversal_search(driver, question)
        results["functions"].extend(graph_results)
        results["sources"].append("graph")
        
    elif primary == "hybrid":
        # 并行执行多个策略
        with ThreadPoolExecutor() as executor:
            future_semantic = executor.submit(semantic_search, client, question)
            future_grep = executor.submit(grep_search, entities[0] if entities else "")
            
        semantic_results = future_semantic.result()
        grep_results = future_grep.result()
        
        # LLM 融合决策
        results["functions"] = llm_fusion_rank(
            client, question, 
            semantic=semantic_results,
            grep=grep_results
        )
        results["sources"].extend(["semantic", "grep"])
        
    else:  # semantic (default)
        # Embedding 语义搜索
        semantic_results = search_code_embedding(client, question)
        results["functions"] = semantic_results
        results["sources"].append("semantic")
    
    # Step 3: 可选的辅助策略
    if secondary == "grep" and primary != "grep":
        # Embedding 搜索结果不理想，补充 Grep
        if max([f.get("score", 0) for f in results["functions"]], default=0) < 0.5:
            for entity in entities[:1]:
                grep_results = grep_search(entity)
                results["functions"].extend(grep_results)
            results["sources"].append("grep_fallback")
    
    return results
```

## 示例场景

| 问题 | LLM 决策 | 原因 |
|------|----------|------|
| "函数 llm_build_exaone 的核心逻辑" | primary: grep | 有具体函数名，精确查找 |
| "llama.cpp 的初始化流程是什么" | primary: graph | 问流程，需要调用链 |
| "为什么选择这种设计" | primary: semantic | 概念类问题，语义搜索 |
| "解析失败的原因" | primary: hybrid | 模糊问题，多策略并行 |

## 预期效果

1. **精准匹配**：有函数名的问题直接走 Grep，不走弯路
2. **避免浪费**：概念类问题不走 Grep，节省 API 调用
3. **灵活组合**：复杂问题可以并行多策略，LLM 综合判断

## 实现优先级

- P1: 基础路由决策（semantic/grep/graph 三选一）
- P2: 混合策略支持（hybrid + LLM 融合）
- P3: 动态 Fallback（根据中间结果调整策略）
