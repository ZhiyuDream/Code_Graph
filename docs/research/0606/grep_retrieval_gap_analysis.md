# Grep 检索层遗漏分析 — 类型2：文件根本不在检索列表中

> 分析时间：2026-06-06  
> 目标：Symbol Fast Path 移除 embedding 硬编码后，grep 检索层无法召回的 5 处源文件遗漏

---

## 一、概述

新系统（移除 embedding 硬编码、分层 top_k）相比旧系统在 **9 道题**上漏得更多。其中 **5 处属于类型2**：文件根本不在检索列表中。这些遗漏集中暴露了当前基于 ripgrep + 正则扫描的函数提取器 `_extract_functions_from_file` 的结构性缺陷。

| 题目 | 目标符号 | 遗漏文件 | 旧系统命中 | 影响 |
|---|---|---|---|---|
| Q02 | `llama_model_default_params` | `src/llama.cpp` | 否 | 大函数超500行被丢弃 |
| Q10 | `common_sampler_init` | `common/common.cpp` | 是 | 初始化列表 `{}` 干扰 brace counting |
| Q19 | `common_sampler_types_from_names` | `common/arg.cpp` | 是 | `} catch` 被误识别为函数定义 |
| Q36 | `common_chat_verify_template` | `common/arg.cpp` | 否 | `} catch` 被误识别为函数定义 |
| Q46 | `postprocess_cpu_params` | `common/arg.cpp` | 否 | `} catch` 被误识别为函数定义 |

**关键发现**：
- 旧系统对 Q10、Q19 的命中全靠 **embedding 语义召回**补上漏洞，新系统移除 embedding 后暴露了底层缺陷
- 5 处遗漏**不是检索范围问题**（ripgrep 都匹配到了目标函数名的文本行），而是**函数边界提取失败**，导致匹配行无法被归属到任何函数

---

## 二、根因分类与逐案分析

### 根因 A：大函数超过 500 行硬限制，被完全丢弃

**代码位置**：`src/qa/retrievers/grep.py:118`
```python
for i in range(brace_start, min(len(lines), start + 500)):
```

**影响案例**：Q02 `src/llama.cpp`

`llama_params_fit_impl` 函数实际长度 **612 行**（第175-786行），超出 `start + 500` 的搜索窗口。`_extract_functions_from_file` 找不到闭合大括号，直接丢弃整个函数。ripgrep 在第184行匹配到 `llama_model_default_params()` 的调用，但 `_extract_function_at_line` 返回 `None`（该行不在任何提取的函数内），`src/llama.cpp` 因此完全未进入检索列表。

**验证**：
```
llama_params_fit_impl: start=175, brace_start=178, end=786, total_lines=612
Line 184: NOT in any function
```

---

### 根因 B：初始化列表 `{}` 干扰大括号匹配

**代码位置**：`src/qa/retrievers/grep.py:117-127`

**影响案例**：Q10 `common/common.cpp`

`common_init_result` 构造函数的初始化列表包含 `pimpl(new impl{})`：
```cpp
common_init_result::common_init_result(common_params & params) :
    pimpl(new impl{}) {
```

`_extract_functions_from_file` 从 `{` 开始逐字符计数，`new impl{}` 中的 `}` 导致 `brace_count` 提前归零。构造函数被错误识别为仅1行长（第1144-1144行），后续第1225行的 `common_sampler_init` 调用不在此函数内。同时，`_extract_function_at_line` 找不到包含第1225行的有效函数，返回 `None`。

---

### 根因 C：`} catch` 被错误识别为函数定义，截断父函数

**代码位置**：`src/qa/retrievers/grep.py:98-100`

当前过滤逻辑：
```python
ctrl_keywords = ('if ', 'if(', 'for ', 'for(', 'while ', 'while(', 
                 'switch ', 'switch(', 'catch ', 'catch(')
if any(stripped.startswith(kw) for kw in ctrl_keywords):
    continue
```

`} catch (std::exception & e) {` 不以 `catch ` 开头（以 `}` 开头），因此**未被过滤**，被当作函数定义处理。其"函数体"从第526行延伸到第701行。

**影响案例**：Q19、Q36、Q46 `common/arg.cpp`

`common_params_parse_ex`（第427-644行）是包含 `common_sampler_types_from_names`、`common_chat_verify_template`、`postprocess_cpu_params` 调用的真正父函数。但 `_extract_function_at_line` 采用"最内层优先"策略（`max(containing, key=lambda x: x["start"])`），当匹配行（第568、569、571、572、635、1576行）同时被 `common_params_parse_ex` 和错误的 `catch` 块包含时，**选择了 start 更大的 `catch` 块**。

`catch` 块的函数名提取为空（签名 `} catch (std::exception & e) {` 无法提取有效函数名），被命名为 `line_526`。随后 `_grep_file` 将其过滤：
```python
if func_info and func_info["name"] and not func_info["name"].startswith("line_"):
```

这 3 处遗漏因此**从检索结果中彻底消失**。

---

## 三、修复方案

### 方案 1：修复 `_extract_functions_from_file` 的边界检测（高优先级）

#### 1a. 移除/放宽 500 行限制

```python
# 旧
for i in range(brace_start, min(len(lines), start + 500)):
# 新
for i in range(brace_start, len(lines)):
    if i > start + 2000:  # 软限制，仅用于防止极端情况
        logger.warning("Function too long, truncating: %s", func_name)
        end_idx = i - 1
        break
```

#### 1b. 修复初始化列表 `{}` 干扰

改进 brace counting 逻辑，在函数签名区域（`start` 到 `brace_start`）忽略 `{}`：
```python
# 在找到真正的函数体 { 之前，忽略初始化列表中的 {}
for i in range(start, min(len(lines), start + 10)):
    if '{' in lines[i]:
        # 检查这一行是否在初始化列表上下文中
        # 简单方案：从第一个不在签名中的 { 开始计数
        brace_start = i
        break
```

**更简单的替代方案**：使用 **缩进栈** 而非大括号计数来推断函数边界，或者使用 `ctags`/`tree-sitter` 等外部工具。

#### 1c. 过滤 `} catch`、`else if`、`else {`

```python
# 强化控制结构过滤
ctrl_patterns = (
    'if ', 'if(', 'for ', 'for(', 'while ', 'while(', 
    'switch ', 'switch(', 'catch ', 'catch(', 
    '} catch', '}catch', 'else if', 'else {', 'else{',
)
if any(stripped.startswith(kw) for kw in ctrl_patterns):
    continue
```

#### 1d. 不过滤 `line_xxx`，改用文件路径+行号去重

```python
# 旧
if func_info and func_info["name"] and not func_info["name"].startswith("line_"):
    key = (file_path, func_info["name"])
# 新
if func_info and func_info["name"]:
    key = (file_path, func_info["name"])
```

同时修改 `_extract_functions_from_file` 中对无函数名的情况：
```python
# 旧
func_name = func_name or f"line_{start + 1}"
# 新：给匿名结构一个更有意义的标识
func_name = func_name or f"anonymous_at_line_{start + 1}"
```

---

### 方案 2：增加 file-level fallback（中优先级）

当 `_extract_function_at_line` 返回 `None` 时，直接返回匹配行附近的代码片段，而不是丢弃：

```python
def _extract_function_at_line(self, file_path: str, line_num: int, file_funcs: list[dict] | None = None) -> dict | None:
    # ... 现有逻辑 ...
    
    if not containing:
        # Fallback: 返回匹配行 ±context_lines 的代码片段
        start_idx = max(0, target_idx - self.context_lines)
        end_idx = min(len(lines) - 1, target_idx + self.context_lines)
        content = ''.join(lines[start_idx:end_idx + 1])
        return {
            "name": f"snippet_at_line_{line_num}",
            "file": file_path,
            "line": line_num,
            "content": content,
            "start_line": start_idx + 1,
            "end_line": end_idx + 1,
        }
```

这能确保：即使函数边界提取失败，匹配到的文件至少会以 snippet 形式进入检索列表。

---

### 方案 3：引入 ctags 或 tree-sitter 替代正则扫描（长期）

正则扫描 C++ 函数边界本质上是不可靠的（初始化列表、模板、宏、Lambda 等都会干扰）。推荐引入：

- **ctags**（已安装，轻量）：`ctags -x --c++-kinds=f src/llama.cpp` 可精确输出函数名、起止行
- **tree-sitter**（更精确）：能处理类成员函数、模板特化、命名空间等复杂场景

```python
# ctags 示例输出
# llama_params_fit_impl    function    175 src/llama.cpp llama_params_fit_impl(...)
# common_init_result       function  1143 common/common.cpp common_init_result(...)
```

使用 ctags 后：
1. 函数边界 100% 准确
2. 无 500 行限制问题
3. 无 `} catch` 误识别问题
4. 性能更好（不需要逐字符扫描大括号）

---

## 四、推荐修复优先级

| 优先级 | 方案 | 改动量 | 预期修复 |
|---|---|---|---|
| P0 | 1a（放宽500行）+ 1c（过滤catch）+ 1d（保留line_xxx） | 小 | 5/5 |
| P1 | 2（file-level fallback） | 中 | 兜底所有未来边界提取失败 |
| P2 | 1b（初始化列表） | 中 | Q10 |
| P3 | 3（ctags替代） | 大 | 彻底根治 |

---

## 五、验证计划

修复后应验证：
1. `_extract_function_at_line('../llama.cpp/src/llama.cpp', 184)` 返回 `llama_params_fit_impl`
2. `_extract_function_at_line('../llama.cpp/common/common.cpp', 1225)` 返回 `common_init_result`
3. `_extract_function_at_line('../llama.cpp/common/arg.cpp', 568)` 返回 `common_params_parse_ex`
4. `_grep_file('llama_model_default_params')` 结果包含 `src/llama.cpp`
5. `_grep_file('common_sampler_init')` 结果包含 `common/common.cpp`
6. `_grep_file('common_sampler_types_from_names')` 结果包含 `common/arg.cpp`
