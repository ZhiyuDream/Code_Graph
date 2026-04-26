# V7 ReAct 改进方案：解决0分题目问题

## 问题诊断

0分题目的共同特征：
1. **函数查询类**（如"函数llm_build_exaone的核心逻辑"）：Embedding检索不到具体函数
2. **设计决策类**（如"为什么选择这种设计"）：缺乏上下文，只能猜测
3. **答案泛泛而谈**：没有代码级别的具体分析

根本原因：**V7过于依赖Embedding相似度，缺少主动搜索和验证机制**

---

## 改进方案（基于Claude Code架构启发）

### 改进1：LLM主动搜索（Grep Fallback）

当Embedding检索结果不佳时（最高相似度<0.5），让LLM主动搜索：

```python
def react_search_with_fallback(driver, client, question: str) -> Dict:
    # Step 1: 常规Embedding检索
    code_results = search_code_embedding(client, question, top_k=5)
    
    # Step 2: 判断是否需要主动搜索
    max_score = max([r.get('score', 0) for r in code_results], default=0)
    if max_score < 0.5:
        # 从问题中提取关键实体（函数名、类名等）
        entities = extract_entities_with_llm(client, question)
        
        # 主动Grep搜索
        for entity in entities:
            grep_results = grep_search(entity, repo_root)
            # 读取匹配的文件
            for file_path in grep_results[:3]:
                file_content = read_file(file_path)
                # 提取相关函数
                related_funcs = extract_functions_near_match(file_content, entity)
                code_results.extend(related_funcs)
    
    # 继续ReAct流程...
```

**效果**：对于"函数xxx的核心逻辑"类问题，能直接定位到函数实现

---

### 改进2：LSP精确导航（替代Embedding）

用clangd LSP替代Embedding进行函数查找：

```python
def search_with_lsp(driver, func_name: str) -> Dict:
    """用LSP精确查找函数定义和引用"""
    # 调用clangd的textDocument/definition
    location = lsp_definition(func_name)
    
    if location:
        # 读取函数定义文件
        code = read_file(location.file, location.start_line, location.end_line)
        
        # 查找所有引用
        references = lsp_references(func_name)
        
        return {
            "definition": code,
            "references": references,
            "source": "lsp"
        }
```

**效果**：精确找到函数定义，不会出现Embedding相似但无关的情况

---

### 改进3：递减回报检测 + 智能停止

```python
def react_search_smart_stop(driver, client, question: str) -> Dict:
    collected = {"functions": [], "issues": [], "steps": []}
    prev_info_gain = 0
    
    for step in range(1, MAX_STEPS + 1):
        # ...执行检索...
        
        # 计算信息增益（新发现的函数数）
        new_funcs = len([f for f in current_funcs if f not in collected["functions"]])
        info_gain = new_funcs
        
        # 递减回报检测
        if step >= 2 and info_gain <= 1 and prev_info_gain <= 1:
            print(f"      信息增益递减，提前停止")
            break
        
        prev_info_gain = info_gain
        
        # 熔断机制：连续2轮没有找到新信息
        if step >= 3 and info_gain == 0:
            print(f"      连续无新信息，熔断停止")
            break
```

**效果**：避免无效扩展，节省API调用

---

### 改进4：答案自验证（Trusting Recall）

生成答案前验证关键信息：

```python
def verify_answer_before_return(client, answer: str, collected: Dict) -> bool:
    """验证答案中的关键函数是否真实存在"""
    # 从答案中提取提到的函数名
    mentioned_funcs = extract_function_names(answer)
    
    for func in mentioned_funcs:
        # 验证函数是否在检索结果中
        if not any(f['name'] == func for f in collected['functions']):
            # 函数不在检索结果中，可能 hallucination
            return False
    
    return True

def generate_answer_with_verification(client, question: str, collected: Dict) -> str:
    answer = generate_answer(client, question, collected)
    
    # 验证答案
    if not verify_answer_before_return(client, answer, collected):
        # 重新生成，强调只使用检索到的信息
        answer = generate_answer_with_constraint(client, question, collected)
    
    return answer
```

**效果**：减少hallucination，提高答案准确性

---

### 改进5：问题类型路由（智能选择策略）

根据问题类型选择不同策略：

```python
def classify_question_type(question: str) -> str:
    """分类问题类型，选择最佳检索策略"""
    if re.search(r'函数\s+(\w+).*核心逻辑', question):
        return "FUNCTION_DETAIL"  # 需要精确查找函数
    elif re.search(r'为什么.*设计|为什么选择', question):
        return "DESIGN_DECISION"  # 需要Issue/PR上下文
    elif re.search(r'调用关系|依赖链路', question):
        return "CALL_CHAIN"  # 需要图遍历
    else:
        return "GENERAL"  # 通用检索

def search_by_type(driver, client, question: str, qtype: str) -> Dict:
    if qtype == "FUNCTION_DETAIL":
        # 提取函数名，用LSP精确查找
        func_name = extract_target_function(question)
        return search_with_lsp(driver, func_name)
    elif qtype == "DESIGN_DECISION":
        # 优先搜索Issue/PR
        return search_issues_priority(driver, client, question)
    elif qtype == "CALL_CHAIN":
        # 图遍历找调用链
        return search_call_chain(driver, client, question)
    else:
        # 默认Embedding
        return search_code_embedding(client, question)
```

**效果**：不同类型问题用最适合的策略

---

## 实施优先级

1. **P0（立即实施）**：改进1（LLM主动搜索）+ 改进3（智能停止）
   - 解决大部分0分题目
   - 减少无效API调用

2. **P1（短期）**：改进5（问题类型路由）
   - 针对性解决函数查询类问题
   - 提升特定类型问题准确率

3. **P2（中期）**：改进4（答案自验证）
   - 提升答案质量
   - 减少hallucination

4. **P3（长期）**：改进2（LSP集成）
   - 需要clangd环境
   - 最精确的代码导航

---

## 预期效果

- 0分题目减少80%（从9题→2题以内）
- 二元正确率从71.7%提升到75%+
- 0-1平均分从0.6657提升到0.70+
- API调用减少30%（智能停止）
