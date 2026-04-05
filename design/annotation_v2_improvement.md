# Annotation V2 改进设计

> 基于 30 题手动 QA 实验（代码理解类多跳/定位问题）发现 annotation 存在系统性误导风险，优先改进 annotation 质量和图邻域信息。

---

## 1. 问题根因

### 1.1 实验证据

| 题目 | 有注解 | 无注解 | 结论 |
|------|--------|--------|------|
| llama_decode 调用链追踪 | **0.0** | **1.0** | annotation 将 llama_decode 描述为"叶子函数"，Agent 放弃追溯 |
| checkpoint 保存路径定位 | **0.0** | **1.0** | annotation 让 Agent 过度依赖 Issue 关键词匹配 |
| PR #20783 修复链路 | 1.0 | 0.0 | annotation 正确帮助 Issue 追溯 |
| llama_server deadlock | 1.0 | 0.0 | annotation 诚实说明找不到时，反而合理 |

### 1.2 根因分类

**A. 注释错误固化（最严重）**
- annotation 的邻域信息（callers/callees）不准确或过时
- LLM 根据不准确的邻域生成错误 summary，固化到图中
- Agent 看到 annotation 后"短路"——认为已理解，不再深入 code reading

**B. Wrapper 函数的多跳陷阱**
- fan_out 高的函数（如 llama_decode）本身不做事，委托给下游
- annotation 若只描述"做什么"而不标注"是 wrapper"，Agent 会误判为叶子函数

**C. 无边界条件和错误模式**
- 现有 annotation 三个字段（summary / workflow_role / invocation_context）都描述"正常行为"
- 没有"已知崩溃点"、"边界条件"——Agent 无法判断何时该深入代码

---

## 2. 改进方案

### 2.1 Annotation Schema V2

```python
ANNOTATION_SCHEMA_VERSION = 2

"""
Function.annotation_json V2 字段：

{
  "schema_version": 2,
  "summary": str,                          # 一句话功能描述（可信任）
  "workflow_role": str,                     # 在大流程中的角色
  "invocation_context": list[str],           # 典型调用场景

  # --- V2 新增 ---
  "failure_modes": list[str],               # 已知崩溃点/错误模式（可为空）
  "confidence": "high" | "medium" | "low",  # annotation 质量置信度
  "is_wrapper": bool,                       # 是否为薄封装（fan_out 委托下游）
  "call_depth_hint": int,                   # 估计的调用深度：0=叶子, >3=深度

  # --- 邻域信息（V2 增强）---
  "caller_signatures": list[str],            # 关键 caller 的函数签名片段
  "callee_signatures": list[str],            # 关键 callee 的函数签名片段
  "neighborhood_confidence": "high" | "medium" | "low",  # 邻域信息可信度
}
"""
```

### 2.2 Prompt 改进

**核心改动**：

1. **禁止在不确定时猜测邻域**：
   ```
   IMPORTANT: If you cannot reliably identify callers/callees from the code,
   set caller_signatures/callee_signatures to empty list and
   set neighborhood_confidence to "low". Do NOT guess.
   ```

2. **Wrapper 函数强制标注**：
   ```
   If this function calls more than 5 distinct downstream functions
   and delegates most work to them (i.e. is a thin wrapper),
   set is_wrapper=true and briefly describe what the real work is
   done by the key callees in failure_modes.
   ```

3. **failure_modes 必填**（允许为空列表，但不允许省略字段）：
   ```
   List any known crash points, assertion failures, or edge conditions.
   If none known from this code, return empty list.
   ```

4. **confidence 自评**：
   ```
   Rate your confidence in this annotation as "high", "medium", or "low".
   low = you made significant inferences not directly visible in the code.
   ```

### 2.3 邻域信息增强

**现状**（V1）：
```python
callers_limit: int = 5,   # 只取前5个 caller 名字
callees_limit: int = 5,   # 只取前5个 callee 名字
```

**改进（V2）**：

```python
# 采样策略：按 fan_in 权重采样，而非随机取前5
def _get_neighborhood_v2(driver, func_id, callers_limit=8, callees_limit=8):
    # 1. 取 fan_in 最高的 N 个 caller（更可能影响语义）
    # 2. 每个 caller 附带 signature 片段（前两行或声明行）
    # 3. 2-hop 采样：对 high-fan-out 函数，额外采样 callers' callers
```

```python
# 示例返回
{
    "file_path": "src/llama.cc",
    "callers": [
        {"name": "llama_decode", "signature": "llama_decode(ctx, batch) -> int"},
        {"name": "llama_eval",   "signature": "llama_eval(ctx, batch, n_tokens) -> int"},
    ],
    "callees": [
        {"name": "ggml_graph_compute", "signature": "ggml_graph_compute(ctx->cgraph, n_threads)"},
        {"name": "llama_sampler_sample", "signature": "llama_token llama_sampler_sample(sampler*, ctx*)"},
    ],
    "is_2hop": False,
}
```

### 2.4 验证步骤（可选阶段）

```bash
# 新增 --validate flag
python annotate_functions.py --version 2 --validate
```

**验证流程**：
1. 生成 annotation 后，用另一个 LLM 调用验证 prompt
2. 对比 annotation 内容 vs 实际代码，输出不一致点
3. 不一致时标记 `confidence: "low"` 或 `annotation_quality: "failed"`
4. 不写回 Neo4j，只打印报告

```python
VALIDATION_PROMPT = """
You are auditing a function annotation for correctness.

Function name: {name}
File: {file_path}

Code:
{code}

Annotation:
{annotation_json}

Task:
1. Check if summary accurately describes the function behavior
2. Check if failure_modes are real crash points visible in the code
3. Check if callers/callees lists are correct (if neighborhood_confidence != "low")

Output JSON:
{{
  "summary_accurate": bool,
  "failure_modes_complete": bool,
  "neighborhood_accurate": bool,
  "overall_confidence": "high" | "medium" | "low",
  "issues": ["list of specific problems if any"]
}}
"""
```

---

## 3. 图写入侧改进

### 3.1 graph_builder.py 调用解析增强

**现状问题**（line 224）：
```python
callee_id = cands[0] if len(cands) == 1 else cands[0]  # ambiguous 时也取第一个
```

**改进**：ambiguous 时标记为 `CALLS_AMBIGUOUS` 边，不写正式 CALLS
```python
if len(cands) == 1:
    edges["CALLS"].append((caller_id, cands[0], {}))
elif len(cands) > 1:
    # 多于一个同名函数，不确定是哪个，记录但不作为可靠 CALLS
    edges["CALLS_AMBIGUOUS"].append((caller_id, cands, {}))
```

### 3.2 Function 节点新增字段

```cypher
# annotation 相关
annotation_json: String,        # JSON 字符串，V2 schema
annotation_version: Integer,   # 当前版本号
annotation_schema_version: Integer,  # schema 版本（全局常量）
annotation_quality: String,    # "passed" | "low" | "failed" | null
last_annotation_time: DateTime,
annotation_error: String,       # 验证失败时的错误信息

# 图计算指标
call_depth: Integer,           # 调用深度估计（叶子=0，wrapper>0）
is_wrapper_function: Boolean,
```

---

## 4. 实现优先级

| 优先级 | 改进项 | 工作量 | 预期收益 | 风险 |
|--------|--------|--------|----------|------|
| **P0** | failure_modes + confidence 字段 + prompt 更新 | 低 | 解决"错误固化"最核心问题 | 低 |
| **P0** | is_wrapper 标注 + call_depth_hint | 低 | 防止 wrapper 函数误导 | 低 |
| **P1** | 邻域采样增强（按 fan_in 权重 + signature） | 中 | 多跳问题质量提升 | 中（采样逻辑复杂） |
| **P1** | annotation_schema_version 全局常量 | 低 | schema 演进可追踪 | 极低 |
| **P2** | 验证步骤（--validate flag） | 中 | annotation 质量闭环 | 中（额外 LLM 成本） |
| **P2** | CALLS_AMBIGUOUS 边类型 | 低 | 调用解析更诚实 | 低 |
| **P3** | 2-hop 邻域采样 | 高 | 深度调用链追溯 | 高（性能/复杂度） |

---

## 5. 实施计划

### Phase 1：Schema + Prompt（P0，立即可做）

1. 在 `annotate_functions.py` 顶部加 `ANNOTATION_SCHEMA_VERSION = 2`
2. 更新 `_build_prompt()` 的 prompt 模板，加入 failure_modes / confidence / is_wrapper / *_signatures
3. 解析 LLM 返回时兼容 V1 格式（无新字段时填默认值）
4. 写回时增加 `annotation_schema_version` 字段
5. 运行 `--version 2 --dry-run` 验证候选函数数量

### Phase 2：邻域采样增强（P1）

1. 修改 `_get_neighborhood()` → `_get_neighborhood_v2()`
2. 按 fan_in 排序采样 + 附加 signature
3. 更新 `_build_prompt()` 接收新的邻域格式

### Phase 3：验证步骤（P2）

1. 新增 `annotate_functions.py --validate` 模式
2. 写 `_validate_annotation()` 函数
3. 不写回 Neo4j，只输出报告

### Phase 4：图写入增强（P2）

1. 修改 `graph_builder.py` 的 CALLS 解析，添加 AMBIGUOUS 边
2. Neo4j schema 迁移：增加 `is_wrapper_function`, `call_depth`, `annotation_quality` 等字段

---

## 6. 向后兼容

- V1 annotation（schema_version 缺失或 < 2）继续有效
- `annotate_functions.py --version 2` 只处理 `annotation_schema_version < 2` 或 `annotation_version < 2` 的函数
- 旧函数不会强制重刷，除非显式指定 `--force`

---

## 7. 预期效果

| 问题类型 | 当前（有/无差） | V2 预期 |
|----------|---------------|---------|
| llama_decode 调用链 | -1.0（有害） | 持平或改善（is_wrapper 警告） |
| checkpoint 路径定位 | -1.0（有害） | 改善（failure_modes 提供更多路径线索） |
| PR 修复链路追溯 | +1.0（有益） | 持平 |
| 深度架构类问题 | ~0（差不多） | 改善（2-hop 邻域） |
