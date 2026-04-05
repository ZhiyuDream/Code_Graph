# Struct Field 解析问题分析与解决方案

## 问题描述

**目标**：将 struct/class 成员变量（struct field / class member）入库为 `Attribute` 节点，建 `Class -> HAS_MEMBER -> Attribute` 边，供 `search_attributes` 工具查询。

**现状**：

| 组件 | 预期行为 | 实际行为 |
|------|---------|---------|
| `clangd_parser.py` `_collect_symbols_from_children()` | 对 kind=8(Field) 的字段节点设置 `kind="member"`，关联到父 struct | struct 字段虽然被 documentSymbol 返回（kind=8），但作为**扁平顶层符号**而非 struct 的 children，导致 `scope_class_index=None` |
| `graph_builder.py` | 对 `kind="member"` 且 `parent_type="Class"` 的变量创建 `Attribute` 节点 | struct 字段的 `parent_type="File"`（因 `scope_class_index=None`），无法创建 Attribute |
| Neo4j | 写入 `Attribute` 节点 + `HAS_MEMBER` 边 | `Attribute: 0`（clangd 方案），`HAS_MEMBER: 0` |
| `search_attributes` 工具 | 能查到 struct 成员 | 查不到任何结果（clangd 方案节点为空） |

---

## 根因分析

### 直接原因

clangd 的 `textDocument/documentSymbol` 返回的是**扁平符号列表**，而非层级树。

实验验证（clangd v14，`ggml.h`）：
```
ggml_init_params: kind=5(Class), children=0, line=646
mem_size:         kind=8(Field),     children=N/A (顶层), line=648
mem_buffer:       kind=8(Field),     children=N/A (顶层), line=649
```

- clangd **确实返回**了 struct 字段（kind=8 = `SymbolKind.Field`）
- 但它们作为**顶层符号**出现，不嵌套在 parent struct 的 children 中
- `_collect_symbols_from_children` 按层级 walk 时，struct 的 children=[] 为空，字段未被捕获
- 即使将字段作为顶层变量处理，`scope_class_index=None` 导致 `parent_type="File"`

### 补充 bug

`clangd_parser.py` 第130行：
```python
kind_str = "param" if (isinstance(kind, int) and kind == 8) ...
```
`kind=8`（Field）被错误归类为 `"param"`，应为 `"member"`（当有 parent struct 时）。

### 深层原因

clangd 的 `textDocument/documentSymbol` 是为 IDE "Go to Symbol" 设计的，**按声明顺序返回扁平符号列表**，不提供结构体字段与其父 struct 的嵌套关联。

对比：
- `textDocument/documentSymbol`（clangd）：返回扁平符号列表，struct 字段与 struct 同级，无法建立父子关系
- libclang `clang_Cursor_getChildren`：返回真实 AST 层级，FIELD_DECL 嵌套在 STRUCT_DECL 之下，天然有父子关系

clangd 在其他 LSP 端点（如 Hover、codeCompletion）通过语义分析可以获取字段偏移等信息，但 documentSymbol 不提供层级关联。

---

## 解决方案

### 方案 A：安装 libclang，用 `ast_parser.py`（推荐，已验证）

**原理**：`ast_parser.py` 用 `pycparser` + `libclang` 的 `clang_Cursor_getChildren`，直接遍历 AST 的所有子节点，包括 `FIELD_DECL`，且有正确的嵌套层级。

libclang v20.1.5（pip install）与系统 libclang-20.so.1 配合工作，需要过滤 build-only 编译 flags（-o、-c、-Werror 等）。

已修复的问题：
1. **Flag 过滤**：过滤 `-o`、`-c`、`-Werror*`、`-Wall` 等影响 libclang 解析的 flags
2. **Null check**：`cursor.get_definition()` 可能返回 None，需 null 检查
3. **系统头过滤**：跳过 `/usr/include/*` 中的 cursor，只保留项目头文件中的 struct 定义
4. **跨文件 struct**：允许来自其他项目文件的 struct 定义被添加到 classes 列表，使其字段能找到 parent

**验证结果**（libclang，ggml.c）：
```
Classes: 47, Variables: 2196, Members: 170
Members by dir: {'ggml/src': 105, 'ggml/include': 65}
```

成员字段示例（正确 parent_class）：
```
ggml_tensor.type (line 655, ggml.h) → parent ggml_tensor at line 655
ggml_tensor.buffer (line 657) → parent ggml_tensor
ggml_init_params.mem_size (line 649) → parent ggml_init_params at line 647
```

### 方案 B：修改 `clangd_parser.py`，对 `.h` 文件用正则解析 struct 定义

见原文档。

### 方案 C：clangd 其他 LSP 端点

clangd 在 Hover、codeCompletion 等端点提供 struct 字段的语义信息（如偏移量、大小），但不适合批量建图（需要为每个 struct 单独请求）。

---

## 推荐路径

**方案 A（libclang + ast_parser.py）** 已验证：
- 解析 316 个文件耗时 ~58s
- 产出：4,764 个 Attribute 节点（结构体字段），正确关联 parent Class
- `search_attributes` 工具可查到 `tensor` 等结构体成员

---

## 解决方案

### 方案 A：安装 libclang，用 `ast_parser.py`（推荐，最直接）

**原理**：`ast_parser.py` 用 `pycparser` + `libclang` 的 `clang_Cursor_getChildren`，直接遍历 AST 的所有子节点，包括 `FIELD_DECL`。

```python
# ast_parser.py 已有逻辑：
elif kind == cindex.CursorKind.FIELD_DECL:
    _add_variable(cursor, "member")  # 正确处理 struct field
```

**步骤**：
```bash
# 安装 libclang
conda install -c conda-forge clangdev  # 或
pip install clang

# 验证
python -c "from clang import cindex; print('OK')"

# 切换建图脚本
python run_stage1.py  # 而非 run_stage1_clangd.py
```

**优点**：不改代码，`ast_parser.py` 已有完整的 struct field 处理逻辑
**缺点**：需要安装 libclang，依赖环境变更

---

### 方案 B：修改 `clangd_parser.py`，对 `.h` 文件用正则解析 struct 定义（workaround）

**原理**：C/C++ 头文件里 struct 定义是文本，直接用正则解析 `struct XXX { ... }` 获取字段列表。

```python
import re

STRUCT_PATTERN = re.compile(
    r'struct\s+(\w+)\s*\{([^}]*)\}',
    re.MULTILINE
)

def parse_struct_fields(file_path: str) -> dict[str, list[str]]:
    """解析 .h 文件中的 struct 定义，返回 {struct名: [字段名列表]}"""
    with open(file_path) as f:
        content = f.read()
    result = {}
    for m in STRUCT_PATTERN.finditer(content):
        struct_name = m.group(1)
        body = m.group(2)
        # 匹配类型 + 字段名（不匹配函数指针等复杂情况）
        field_names = re.findall(r'\b(\w+)\s*;', body)
        result[struct_name] = field_names
    return result
```

然后在 `collect_all_via_clangd()` 处理完每个文件后，对 `.h` 文件补充调用 `parse_struct_fields()`，将解析出的字段加入 `tu["variables"]`。

**优点**：不依赖额外库，纯 Python
**缺点**：
- 正则无法处理嵌套 struct、union、匿名结构体、函数指针类型
- `.cpp` 文件里的 struct 定义无法覆盖
- hacky，不够健壮

---

### 方案 C：利用 clangd 的 `textDocument/codeLens` 或其他 LSP 接口（不推荐）

clangd 没有专门的 "field" 符号接口，`documentSymbol` 是唯一选择，但不支持。

---

## 推荐路径

**优先方案 A（安装 libclang）**。理由：

1. `ast_parser.py` 已有完整正确的 `FIELD_DECL` 处理代码，无需写新逻辑
2. `run_stage1.py` 已实现，切换成本低
3. `ast_parser.py` 用的是 libclang 的 Python binding，比 clangd LSP 更贴近 AST 底层，信息更完整

**如果无法安装 libclang**，才考虑方案 B 作为 workaround，但需要限定只解析 `.h` 文件的简单 struct 定义，并明确标注"不完整，仅作 Attribute 补充"。

---

## 待办步骤

### Phase 1：验证 libclang 可用（1 分钟）
```bash
python -c "from clang import cindex; print('libclang OK')"
```
- 成功 → 直接跑 `run_stage1.py`
- 失败 → 继续 Phase 2

### Phase 2：Workaround 正则解析（如果 Phase 1 失败）
- 在 `clangd_parser.py` 加 `parse_struct_fields()` 函数
- 在 `collect_all_via_clangd()` 返回结果后，对 `.h` 文件调用并补充 variables
- 验证：`search_attributes` 能查到 struct field

---

## 验证方法

```python
# 检查 Neo4j
MATCH (a:Attribute) RETURN count(a) AS cnt
# 期望：有具体数字（非 0）

# 检查 graph_builder 统计
# 重新建图后打印：Attribute 节点数量 > 0

# 功能测试
search_attributes("ctx")  # 应返回 llama_ctx 等 struct 的成员
```

---

## 影响范围

- `Attribute` 节点入库 → `search_attributes` 工具有内容可查
- `Variable` 节点不受影响（member 变量本就不在 Variable 里）
- `Class` 节点不受影响
- `Function`、`CALLS` 等不受影响
