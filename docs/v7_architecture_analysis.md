# V7 架构与 Claude Code 对比分析

## 当前 V7 P0 架构

```
用户问题
    ↓
Embedding 检索 (Code/Issue)
    ↓ (相似度<0.5时)
Grep Fallback 搜索
    ↓
ReAct 迭代扩展 (Neo4j 调用链)
    ↓
生成答案
```

## 与 Claude Code 的映射

| Claude Code | Code_Graph V7 | 说明 |
|-------------|---------------|------|
| Grep/Glob | Grep Fallback | ✅ 已具备灵活搜索 |
| LSP | Neo4j 图 + clangd | ✅ 已具备结构化导航 |
| LLM 主动决策 | ReAct decide | ✅ 已具备 |
| Read | 代码片段加载 | ✅ 已具备 |

## 关键发现

**我们已经在做 Claude Code 的模式了！**

1. **Neo4j 图 ≈ LSP 的能力**
   - 函数定义查找 ✅
   - 调用链追踪 ✅ (callers/callees)
   - 文件-函数关系 ✅

2. **Grep Fallback ≈ Grep 的能力**
   - 灵活 pattern 匹配 ✅
   - 跨语言搜索 ✅
   - 模糊匹配 ✅

3. **区别**
   - Claude Code：LLM 实时决定用什么工具
   - V7 P0：固定的 Fallback 阈值（0.5）

## 真正的改进方向

不是替换工具，而是**优化决策逻辑**：

### 改进 A：智能工具选择

让 LLM 根据问题类型决定检索策略：

```python
def classify_and_route(question):
    """Claude Code 风格的工具选择"""
    
    # 提取问题特征
    has_func_name = extract_function_name(question)  # "函数xxx"
    has_pattern = has_keywords(question, ["如何", "为什么", "设计"])
    has_flow = has_keywords(question, ["流程", "调用链", "顺序"])
    
    # 智能路由
    if has_func_name:
        # 有具体函数名 → 先用 Grep 精确定位
        return "GREP_FIRST"
    elif has_flow:
        # 流程类 → 用 Neo4j 调用链
        return "GRAPH_TRAVERSE"
    elif has_pattern:
        # 设计/原因类 → Embedding + Issue
        return "SEMANTIC_SEARCH"
    else:
        # 默认 → Embedding + Fallback
        return "HYBRID"
```

### 改进 B：工具结果融合

Claude Code 的关键是**LLM 综合多个工具的结果**：

```python
def multi_tool_search(question):
    """并行使用多个工具，LLM 综合结果"""
    
    # 并行执行
    with ThreadPoolExecutor() as executor:
        future_grep = executor.submit(grep_search, question)
        future_graph = executor.submit(graph_search, question)
        future_semantic = executor.submit(semantic_search, question)
    
    # LLM 综合决策
    all_results = {
        "grep": future_grep.result(),
        "graph": future_graph.result(),
        "semantic": future_semantic.result()
    }
    
    # LLM 判断哪些结果可信
    return llm_fusion_decide(question, all_results)
```

### 改进 C：交互式探索

Claude Code 的精髓是**多轮交互探索**：

当前 V7：
- 一次性检索 → 生成答案

Claude Code 风格：
- 检索 → 发现不确定 → 继续检索 → 验证 → 生成答案

```python
def interactive_exploration(question):
    """交互式代码探索"""
    
    context = {"collected": [], "confidence": 0}
    
    while context["confidence"] < 0.8 and len(context["collected"]) < 5:
        # 根据当前上下文决定下一步
        next_action = llm_decide_next_step(question, context)
        
        if next_action["tool"] == "grep":
            result = grep_search(next_action["target"])
        elif next_action["tool"] == "graph":
            result = graph_query(next_action["query"])
        elif next_action["tool"] == "read":
            result = read_file(next_action["file"])
        
        context["collected"].append(result)
        context["confidence"] = evaluate_confidence(context)
    
    return context["collected"]
```

## 建议的下一步改进

### 短期（P1）

1. **问题类型分类路由**
   - 识别"函数xxx核心逻辑"类问题 → 直接走 Grep
   - 识别"调用流程"类问题 → 直接走 Neo4j 调用链
   - 识别"设计原因"类问题 → 优先走 Issue

2. **工具结果可信度评估**
   - LLM 判断 Grep 结果是否真的相关
   - 过滤 false positive

### 中期（P2）

3. **多工具并行 + 融合**
   - 同时走 Grep + Neo4j + Embedding
   - LLM 综合判断哪个结果更可信

4. **交互式验证**
   - 答案生成前自我验证
   - "我提到的函数 xxx 是否真的存在？去查一下"

### 长期（P3）

5. **clangd 实时补充**
   - 对于 Neo4j 中缺失的最新代码，用 clangd 补充
   - 不是替代，而是增量补充

## 总结

当前架构方向是对的！不需要大改，只需要：

1. ✅ 已有：Grep 灵活搜索
2. ✅ 已有：Neo4j 结构化导航  
3. 🔄 优化：更好的工具选择决策
4. 🔄 优化：多工具结果融合
5. 🔄 优化：交互式验证机制
