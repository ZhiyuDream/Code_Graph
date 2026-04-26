# 扩展工具集实现总结

## 目标
为 `run_qa_v7_p0_improved.py` 添加完整的工具集，允许 LLM 自主选择工具（而非仅使用硬编码的 callers/callees）。

## 已完成的工作

### 1. 新增工具集成
从 `agent_qa.py` 导入并集成以下工具：

| 工具名 | 功能 | 适用场景 |
|--------|------|----------|
| `tool_read_file_lines` | 读取文件特定行 | 查看结构体定义、宏定义、具体实现 |
| `tool_search_variables` | 搜索变量/宏定义 | 宏定义（如 GGML_XXX）、全局变量 |
| `tool_search_attributes` | 搜索结构体/类成员 | 结构体字段、类属性 |
| `tool_find_module_by_keyword` | 根据关键词查找模块 | 定位模块位置 |
| `tool_get_file_functions` | 获取文件内所有函数 | "文件中有哪些函数"类问题 |

### 2. 核心代码修改

#### `react_decide()` 函数
- **扩展可用工具列表**：将 action 选项从 `[expand_callers, expand_callees, sufficient]` 扩展为包含 7 个选项
- **优化 prompt**：详细描述每个工具的适用场景，帮助 LLM 正确选择
- **修复 action 限制**：原代码强制限制 action 只能是 callers/callees，现已支持所有工具
- **优化 target 处理**：新工具（如 search_variables）使用 LLM 提供的变量名，而非强制匹配函数列表

#### `react_search()` 函数
- **新增工具调用处理**：为每个新工具添加完整的处理逻辑
  - `read_file_lines`：解析文件路径和行号范围，调用工具，存储代码片段
  - `search_variables`：搜索变量/宏，解析结果，存储到 collected["variables"]
  - `search_attributes`：搜索结构体属性，解析结果，存储到 collected["attributes"]
  - `find_module`：查找模块，存储结果
  - `get_file_functions`：获取文件函数列表，解析存储

#### `generate_answer()` 函数
- **扩展上下文**：将 code_snippets、variables、attributes 纳入答案生成上下文

### 3. 测试结果

#### 测试案例 1：宏定义问题
```
问题: 系统中所有的GGML_HEXAGON_MAX_SESSIONS包含哪些...
决策: search_variables -> GGML_HEXAGON_MAX_SESSIONS
结果: 成功找到 1 个变量/宏
```

#### 测试案例 2：模块功能问题
```
问题: 这个 ggml-blas 主要包含哪些核心功能...
决策: read_file_lines -> ggml_backend_blas.c
结果: 工具被调用（文件未找到，但流程正确）
```

## 关键发现

### 问题诊断
在原始代码的第 443 行发现关键限制：
```python
"action": action if action in ["expand_callers", "expand_callees"] else "expand_callees"
```
这段代码将 LLM 的选择强制限制为 callers/callees，即使 prompt 中提供了其他工具选项。

### 解决方案
1. 扩展 valid_actions 列表包含所有新工具
2. 根据 action 类型采用不同的 target 验证策略
3. 保持向后兼容性

## 待优化项

1. **文件名解析**：`read_file_lines` 需要更智能的文件名解析（支持部分匹配）
2. **去重逻辑**：防止重复搜索相同的变量/文件
3. **并发优化**：新工具目前串行执行，可考虑并行化
4. **大规模测试**：需要完整 360 题测试评估准确率变化

## 代码变更文件
- `scripts/run_qa_v7_p0_improved.py`：主要修改

## 下一步建议
1. 运行完整 360 题测试评估准确率
2. 分析新工具被调用的频率和效果
3. 优化工具参数解析（特别是文件路径）
