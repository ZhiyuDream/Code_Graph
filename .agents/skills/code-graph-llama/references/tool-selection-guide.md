# llama.cpp 工具选择决策矩阵

> 本文件基于 Code_Graph v7 的实验数据编制。
> 核心原则：**简单规则路由优于 LLM 路由**（P0 准确率 74% > P1 71%）。

---

## 1. 问题类型 → 工具映射速查表

| 问题类型 | 识别特征 | 首选工具 | 次选工具 | 禁用工具 |
|----------|----------|----------|----------|----------|
| **精确函数定位** | 问题包含确切函数名 | `search_functions` / `get_function_detail` | `grep_search` | — |
| **文件内结构** | "文件 X 中有哪些函数/结构体" | `get_file_functions` | `read_file_lines` | `semantic_search` |
| **调用链分析** | "谁调用了 X"、"流程"、"依赖" | `get_callers` / `get_callees` | `get_function_detail` | — |
| **变量/常量定位** | 包含变量名、枚举名 | `search_variables` | `read_file_lines` | — |
| **结构体字段细节** | "struct X 中字段 Y 的类型" | `search_attributes` → `read_file_lines` | — | `semantic_search` |
| **模糊概念/设计原理** | "负责量化的函数有哪些"、"为什么这样设计" | `semantic_search` | `search_issues` | `get_callers`/`get_callees` |
| **Bug/性能/Feature** | "遇到了...问题"、"报错"、"crash" | `search_issues` → `get_issue_detail` | `semantic_search` | — |
| **模块/架构** | "ggml 模块的职责"、"模块间关系" | `get_module_overview` | `find_module_by_keyword` | — |

---

## 2. 条件触发规则（替代粗放式扩展）

### callers/callees 扩展触发条件

**实验数据**：callers/callees 占 57.5% 工具调用，但 80% 空手而归（0 新函数）。完全移除导致准确率暴跌 8.5%。

**结论**：不能全删，也不能全用。改用**关键词规则触发**。

```python
CALL_CHAIN_KEYWORDS = {
    '调用', 'caller', 'callee', '调用链', 'call chain',
    '流程', 'flow', '执行顺序', '执行过程',
    '依赖', 'depend', 'dependency',
    '影响分析', '上游', '下游',
    '谁调用', '被谁调用', '哪里调用'
}

def needs_call_chain_expansion(question: str) -> bool:
    q = question.lower()
    return any(kw in q for kw in CALL_CHAIN_KEYWORDS)
```

**规则**：
- `needs_call_chain_expansion(question) == True` → 将 `get_callers` / `get_callees` 加入可用工具池
- `needs_call_chain_expansion(question) == False` → **禁止**调用 callers/callees

### 文件结构问题触发条件

```python
FILE_STRUCTURE_KEYWORDS = {
    '包含哪些', '有哪些函数', '文件中', '代码结构',
    '定义了哪些', 'inside', 'in file', 'contains',
    '有哪些类', '有哪些结构体', '有哪些宏'
}

def is_file_structure_question(question: str) -> bool:
    return any(kw in question.lower() for kw in FILE_STRUCTURE_KEYWORDS)
```

**规则**：
- 命中时，优先使用 `get_file_functions`，不要只依赖 `semantic_search`

### Bug/Issue 问题触发条件

```python
BUG_ISSUE_KEYWORDS = {
    '遇到了', '出现了', '报错', '错误', 'bug', 'crash',
    '性能问题', 'performance', 'illegal memory', 'segfault',
    'feature', '建议', ' enhancement'
}

def needs_issue_search(question: str) -> bool:
    return any(kw in question.lower() for kw in BUG_ISSUE_KEYWORDS)
```

**规则**：
- 命中时，必须搜索 Issue
- 找到 Issue 编号后，**同一轮**立即调用 `get_issue_detail`

---

## 3. Grep Fallback 触发条件

**实验数据**：P0 的 Grep Fallback 显著提升了召回率。

```python
def should_trigger_fallback(semantic_results: list) -> bool:
    if not semantic_results:
        return True
    max_score = max(f.get('score', 0) for f in semantic_results)
    top10_avg = sum(f.get('score', 0) for f in semantic_results[:10]) / len(semantic_results[:10])
    return max_score < 0.5 or (len(semantic_results) >= 5 and top10_avg < 0.4)
```

**规则**：
- `should_trigger_fallback == True` → 立即用 LLM 提取实体，然后执行 `grep_search`
- 将 grep 结果合并到函数列表中

---

## 4. 智能停止条件（熔断机制）

**实验数据**：P0 的"连续2轮增益≤1"过于激进，导致 83.7% 错误题过早停止。

**优化后的停止条件**：

```python
def should_stop(info_gain_history, current_functions, top_score, question_type):
    recent_gains = info_gain_history[-2:]
    
    # 条件 A：连续低增益 + 已有足够信息
    if (all(g <= 1 for g in recent_gains) 
        and len(current_functions) >= 5 
        and top_score >= 0.6):
        return True
    
    # 条件 B：连续 2 轮 0 增益，无论如何停止
    if all(g == 0 for g in recent_gains):
        return True
    
    # 条件 C：函数上限
    if len(current_functions) >= 12:
        return True
    
    # 条件 D：调用链问题的额外检查
    if (question_type == 'call_chain' 
        and not has_expanded_call_chain 
        and step >= 3):
        return False  # 不能停，必须扩展调用链
    
    return False
```

---

## 5. 为什么不用 LLM 做路由？

**P1 实验结果（360题）**：
- LLM 路由增加了 ~11s 延迟
- 准确率从 P0 的 74.1% 降到 71.1%
- LLM 对架构类问题经常误判为 `semantic`，遗漏图结构信息
- `grep` 策略准确率最高（76.7%），但只被使用了 11.9%

**结论**：
> 用快速关键词/规则路由替代 LLM 每题路由。只有在边缘情况下（如同时命中多个关键词），才考虑用轻量级 LLM 辅助决策。

---

## 6. 工具效率参考

| 工具 | 平均新函数/调用 | 效率评级 | 备注 |
|------|----------------|----------|------|
| `graph_search` | 6.12 | ⭐⭐⭐⭐⭐ | 初始检索效果好 |
| `grep_fallback` | 5.97 | ⭐⭐⭐⭐⭐ | 补充 embedding 盲区 |
| `semantic_search` | 5.73 | ⭐⭐⭐⭐ | 适合模糊概念 |
| `issue_search` | 2.31 个 issues | ⭐⭐⭐ | Bug/设计类必需 |
| `get_callers` | 0.55 | ⭐ | 条件触发，禁止滥用 |
| `get_callees` | 0.49 | ⭐ | 条件触发，禁止滥用 |

**资源分配原则**：
- 优先使用高星级工具（semantic + graph + grep）
- callers/callees 只在命中关键词时使用
- 禁止为了"凑步数"而做无意义扩展
