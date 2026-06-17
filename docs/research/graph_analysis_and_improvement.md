# 代码图谱 CALLS 边不足问题分析与改进方案

**日期**: 2026-05-05 | **图谱规模**: 39,532 节点 / 302,808 边

---

## 一、现状诊断

### 1.1 CALLS 边核心指标

| 指标 | 数值 | 说明 |
|------|------|------|
| 总函数节点 | 17,975 | 包含声明、定义、内联函数、模板 |
| CALLS 边总数 | 9,835 | 仅占所有边的 3.2% |
| 有 outgoing 的函数 | 672 (3.7%) | 绝大多数函数无 outgoing calls |
| 有 incoming 的函数 | 3,203 (17.8%) | 被调用的函数比例稍高 |
| 平均 outgoing degree | 0.55 | 正常应有 3-10 |
| P95 outgoing degree | 0.0 | 95% 的函数 outgoing = 0 |
| 孤立节点（无任何边）| 8,905 (22.5%) | 已比之前 82% 大幅改善 |

### 1.2 根本原因分析

#### 原因 1: 第三方库函数占用大量节点但 0 CALLS

```
第三方库函数: 4,285 个
第三方库 CALLS: 0 条
核心代码函数: 13,690 个
核心代码 CALLS: 9,835 条
```

**问题**: `vendor/miniaudio/miniaudio.h` (1,350 函数)、`vendor/cpp-httplib/httplib.h` (996 函数)、`vendor/nlohmann/json.hpp` (919 函数) 等第三方库头文件中有大量内联函数/模板函数/宏，clangd 的 `callHierarchy` 无法提取它们之间的调用关系。

#### 原因 2: 头文件函数 vs 实现文件函数比例失衡

```
头文件函数 (.h/.hpp): 10,876 个 — 有 outgoing 的仅 3.3%
实现文件函数 (.c/.cpp): 7,099 个 — 有 outgoing 的仅 4.4%
```

**问题**: 
- 头文件中主要是**函数声明**（无函数体），自然没有 outgoing calls
- 实现文件中的函数定义虽然有函数体，但 clangd callHierarchy 的提取覆盖率仍然很低
- 调用链从声明处（.h）发起请求，但定义在另一个文件（.cpp），clangd 可能无法正确关联

#### 原因 3: 重复函数节点稀释 CALLS 密度

```
重复函数（同名同文件）: 962 组, 涉及 3,794 个节点
典型: build_graph in tests/test-backend-ops.cpp 出现 100 次
```

**问题**: 测试文件中的匿名命名空间、宏展开导致同一函数名出现多次，这些重复节点没有独立的调用关系。

#### 原因 4: clangd callHierarchy 的固有局限

clangd 的 `textDocument/prepareCallHierarchy` + `callHierarchy/outgoingCalls` 存在以下限制：

| 场景 | clangd 支持 | 影响 |
|------|-----------|------|
| 普通函数调用 | ✅ | 正常提取 |
| 模板函数实例化 | ⚠️ 部分 | 可能遗漏 |
| 宏展开后的调用 | ❌ | 完全遗漏 |
| 函数指针/回调 | ❌ | 完全遗漏 |
| 条件编译 (#ifdef) | ⚠️ 部分 | 可能遗漏 |
| 内联函数 | ⚠️ 部分 | 可能遗漏 |
| 跨文件调用 | ✅ | 可提取但需正确路径匹配 |

### 1.3 call_resolver 解析失败分析

`call_resolver.py` 的解析逻辑：
1. 用 `(callee_file_path, callee_name)` 查找候选函数
2. 用 `callee_line` 精确匹配到函数定义的行范围
3. fallback: 用 `(caller_file_path, callee_name)` 查找

**失败原因**:
- `callee_file_path` 为空或不匹配：clangd 对很多调用不返回目标文件路径
- `callee_line` 不在任何函数行范围内：clangd 返回的 callee_line 可能是调用点而非定义点
- 同名函数重载：即使有 line 信息也可能匹配到错误的重载版本

---

## 二、改进方案

### 2.1 短期改进（1 周内）— 数据清洗

#### 2.1.1 过滤第三方库函数

**建议**: 在建图阶段排除 `vendor/` 目录下的函数节点。

**理由**:
- 第三方库函数不是项目核心代码，问答时不需要追踪它们的内部调用
- 4,285 个第三方库函数占 23.8%，但贡献 0 条 CALLS 边
- 过滤后核心函数 13,690 个，CALLS/函数比从 0.55 提升到 0.72

**实现**:
```python
# 在 symbol_extractor.py 的 process_file 中
if file_path.startswith("vendor/"):
    # 只提取顶级 API，不提取内部函数
    # 或完全跳过 vendor/ 目录
```

#### 2.1.2 去重：同名同文件函数合并

**建议**: 对同名同文件的函数节点进行合并，保留一个代表节点。

**理由**:
- 962 组重复函数涉及 3,794 个节点，占 21.1%
- 这些重复节点大多来自测试文件的匿名命名空间或宏展开

**实现**:
```python
# 在 graph_assembler.py 中
func_groups = defaultdict(list)
for f in functions:
    func_groups[(f.file_path, f.name)].append(f)

# 同名同文件合并为一个节点
merged_functions = []
for (fp, name), group in func_groups.items():
    if len(group) == 1:
        merged_functions.append(group[0])
    else:
        # 合并：取最大行范围，合并签名
        merged = merge_function_group(group)
        merged_functions.append(merged)
```

#### 2.1.3 区分"声明"与"定义"节点

**建议**: 为函数节点增加 `is_declaration` / `is_definition` 属性，优先从定义节点提取 CALLS。

**理由**:
- 头文件中的声明节点不应被用于 outgoing calls 查询
- 可减少约 60% 的无效 callHierarchy 请求

**实现**:
```python
# 基于函数是否有函数体（行数 > 3）判断
def is_definition(func):
    return func.end_line - func.start_line > 3  # 粗略判断
```

**预期效果**:
- 清洗后核心函数约 10,000 个（去重 + 过滤声明）
- CALLS/函数比提升到 ~1.0
- 孤立节点率从 22.5% 降到 ~15%

---

### 2.2 中期改进（2-4 周）— 解析增强

#### 2.2.1 补充基于名称的全局调用匹配

**问题**: `call_resolver` 过于依赖 `callee_file_path`，但很多 RawCall 的 `callee_file_path` 为空。

**改进**: 当 `callee_file_path` 为空时，基于 `callee_name` 进行全局匹配：

```python
def resolve_call_global(lookup, raw, caller_id):
    """全局名称匹配，不限制文件路径。"""
    callee_name = raw.callee_name
    
    # 1. 精确匹配：找唯一的同名函数
    all_candidates = []
    for (fp, name), ids in lookup.by_name.items():
        if name == callee_name:
            all_candidates.extend(ids)
    
    if len(all_candidates) == 1:
        return all_candidates[0][0], "resolved_global", None
    
    # 2. 优先匹配同目录下的函数
    caller_fp = raw.file_path
    caller_dir = os.path.dirname(caller_fp)
    same_dir = [(fp, id) for (fp, name), ids in lookup.by_name.items() 
                for id, _, _ in ids 
                if name == callee_name and os.path.dirname(fp) == caller_dir]
    if len(same_dir) == 1:
        return same_dir[0][1], "resolved_same_dir", None
    
    # 3. 启发式：匹配最短的函数名（排除 getter/setter 等辅助函数）
    # 或匹配被调用次数最多的函数
    
    return None, "unresolved", None
```

**预期效果**: 可额外解析 30-50% 的 unresolved calls。

#### 2.2.2 从 compile_commands.json 获取更精确的定义位置

**问题**: clangd 的 `documentSymbol` 对头文件中的声明和实现文件中的定义分别返回符号，但 callHierarchy 请求需要从定义处发起。

**改进**: 利用 `compile_commands.json` 中的文件编译关系，将头文件中的声明映射到实现文件中的定义：

```python
# 建立 declaration -> definition 映射
decl_to_def = {}
for func in all_functions:
    if is_declaration(func):
        # 查找同名同签名的定义
        key = (func.name, func.signature)
        defs = [f for f in all_functions if f.name == func.name and is_definition(f)]
        if len(defs) == 1:
            decl_to_def[func.id] = defs[0].id
```

#### 2.2.3 提取宏展开调用

**问题**: llama.cpp 大量使用宏（如 `GGML_ASSERT`、`GGML_UNUSED`、`ggml_tensor_set`），宏展开后的调用无法被 clangd 识别。

**改进**: 基于文本分析的宏调用提取：

```python
# 简单的基于正则的宏调用提取
MACRO_PATTERN = re.compile(r'\b([A-Z][A-Z_0-9]{3,})\s*\(')

def extract_macro_calls(source_code, file_path, func_start, func_end):
    """从函数源码中提取宏调用。"""
    lines = source_code.split('\n')[func_start-1:func_end]
    calls = []
    for i, line in enumerate(lines):
        for match in MACRO_PATTERN.finditer(line):
            macro_name = match.group(1)
            calls.append(RawCall(
                caller_index=...,
                callee_name=macro_name,
                file_path=file_path,
                line=func_start + i,
            ))
    return calls
```

**预期效果**: 可额外提取 1,000-2,000 条宏调用边。

---

### 2.3 长期改进（1-2 月）— 模块级聚合与社区发现

#### 2.3.1 模块节点构建（Louvain / Leiden 算法）

**目标**: 将函数聚类为逻辑模块，支持粗粒度检索。

**算法选择**: 
- **Louvain**: 速度快，适合大规模图
- **Leiden**: Louvain 的改进版，社区质量更高，推荐

**实现思路**:

```python
import networkx as nx
import community as community_louvain  # python-louvain

# 1. 构建函数调用图
G = nx.DiGraph()
for func in functions:
    G.add_node(func.id, name=func.name, file=func.file_path)
for caller_id, callee_id in calls:
    G.add_edge(caller_id, callee_id)

# 2. 转为无向图进行社区发现（Louvain 要求无向）
G_undirected = G.to_undirected()

# 3. 运行 Leiden 算法
# 需要 cdlib 库: pip install cdlib
from cdlib import algorithms
communities = algorithms.leiden(G_undirected)

# 4. 为每个社区创建 Module 节点
for i, comm in enumerate(communities.communities):
    module_id = f"module:{i}"
    module_name = infer_module_name(comm)  # 基于文件路径或核心函数命名
    
    # 创建 Module 节点
    nodes["Module"].append({
        "id": module_id,
        "name": module_name,
        "function_count": len(comm),
        "files": list(set(functions_by_id[fid].file_path for fid in comm)),
    })
    
    # 创建 BELONGS_TO 边
    for func_id in comm:
        edges["BELONGS_TO"].append((func_id, module_id, {}))

# 5. 模块间调用边
module_calls = defaultdict(int)
for caller_id, callee_id in calls:
    caller_module = func_to_module.get(caller_id)
    callee_module = func_to_module.get(callee_id)
    if caller_module and callee_module and caller_module != callee_module:
        module_calls[(caller_module, callee_module)] += 1

for (m1, m2), weight in module_calls.items():
    edges["MODULE_CALLS"].append((m1, m2, {"weight": weight}))
```

**模块命名策略**:
- 基于文件路径的公共前缀（如 `ggml/src/ggml.c` → `ggml-core`）
- 基于社区中度数最高的函数名
- 基于目录结构（如 `common/` → `common-utils`）

#### 2.3.2 高频函数降权

**问题**: 一些高频基础函数（如 `ggml_malloc`、`ggml_free`、`ggml_log`）被大量调用，但在问答中往往是噪声。

**改进**: 基于 PageRank / degree centrality 为函数标注权重，检索时降权高频基础函数。

```python
# 计算 PageRank
pagerank = nx.pagerank(G)

# 标注权重
for func_id, pr in pagerank.items():
    # 高频函数（top 5% PageRank）降权
    is_high_freq = pr > threshold
    nodes["Function"][func_id]["weight"] = 0.3 if is_high_freq else 1.0
```

#### 2.3.3 基于 AST 的精确调用图

**最终目标**: 用 clang AST（而非 LSP callHierarchy）构建完整的调用图。

**方案**:
1. 使用 `libclang` Python 绑定遍历 AST
2. 提取 `CallExpr` 节点，获取被调用函数的完全限定名
3. 使用 `compile_commands.json` 确保正确的 include 路径和宏定义
4. 处理模板实例化、函数指针、lambda 等复杂场景

**优势**: 
- 不依赖 clangd LSP，解析更完整
- 可处理宏展开（通过 `-E` 预处理）
- 可获取完全限定名，避免重载歧义

**劣势**:
- 需要编译环境（compile_commands.json）
- 解析速度较慢
- 对模板的支持仍有限

---

## 三、改进优先级与预期效果

### 3.1 优先级排序

| 优先级 | 改进项 | 工作量 | 预期 CALLS 边增量 | 预期正确率提升 |
|--------|--------|--------|------------------|---------------|
| **P0** | 过滤第三方库函数 | 1 天 | +0（但密度提升） | +0% |
| **P0** | 去重同名函数 | 2 天 | +0（密度提升） | +0% |
| **P1** | 全局名称匹配（call_resolver 增强） | 3 天 | +3,000-5,000 | +2-3% |
| **P1** | 宏调用提取 | 2 天 | +1,000-2,000 | +1-2% |
| **P2** | 模块节点（Leiden） | 5 天 | N/A（新节点类型） | +1-2% |
| **P2** | 高频函数降权 | 2 天 | N/A（检索优化） | +0.5-1% |
| **P3** | AST 精确调用图 | 2-4 周 | +5,000-10,000 | +3-5% |

### 3.2 预期效果汇总

**清洗后（短期）**:
- 函数节点: 17,975 → ~10,000（过滤 vendor + 去重 + 过滤声明）
- CALLS 边: 9,835（不变，但密度从 0.55 → ~1.0）
- 孤立节点率: 22.5% → ~15%

**解析增强后（中期）**:
- CALLS 边: 9,835 → 15,000-18,000（全局匹配 + 宏调用）
- CALLS/函数比: ~1.0 → ~1.5-1.8
- 有 outgoing 的函数: 3.7% → 15-20%

**模块聚合后（长期）**:
- 新增 Module 节点: ~50-100 个
- MODULE_CALLS 边: ~200-500 条
- 支持粗粒度 → 细粒度的分层检索

---

## 四、对 QA 系统的直接影响

### 4.1 当前受 CALLS 不足影响的问题类型

| 问题类型 | 受影响程度 | 原因 |
|---------|-----------|------|
| 模块依赖分析 | **高** | 无法追踪跨模块调用链 |
| 数据流分析 | **高** | 无法追踪 caller→callee 的数据传递 |
| 设计意图推断 | **中** | 缺少调用上下文 |
| 代码位置定位 | **低** | Grep 可补充 |
| 架构概览 | **中** | 无法展示模块间关系 |

### 4.2 改进后对 ReAct Agent 的增益

当前 ReAct Agent 的 `graph_search` 经常返回空结果（graph=0），导致：
1. 后续 ReAct 迭代无法发现新的调用线索
2. 上下文碎片化，无法组织成有意义的结论
3. GPT-5.4 被迫"诚实拒绝"

改进后：
1. `graph_search` 召回率从 ~30% 提升到 ~60%
2. ReAct 迭代的信息增益提升，减少无效迭代
3. 跨文件调用链可追踪 2-3 跳
4. GPT-5.4 正确率有望从 87.3% → 90%+

---

*分析时间: 2026-05-05*
