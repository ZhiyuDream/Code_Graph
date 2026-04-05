# PR/Issue 节点对图谱 QA 的影响分析

## 当前图谱中的 PR/Issue 数据

| 节点/边类型 | 数量 | 说明 |
|------------|------|------|
| PullRequest 节点 | 748 | 最近 10 页 merged PR |
| Issue 节点 | 256 | 最近 10 页 closed Issue |
| FIXES 边 (PR→Issue) | 9 | PR body 中含 `fixes #N` |
| MODIFIES 边 (PR→File) | 64 | PR 的 changed_files 匹配图中 File 节点 |
| TOUCHES 边 (PR→Function) | 3,767 | changed_files 匹配 Function.file_path |
| MENTIONS 边 (PR/Issue→Function) | 944 | body 中提到的函数名匹配图中 Function |

---

## 能回答的问题（有了 PR/Issue 节点之后）

### 1. "哪个 PR 修复了这个 Issue？"

```cypher
MATCH (pr:PullRequest)-[:FIXES]->(i:Issue)
RETURN pr.number, pr.title, i.number, i.title
```

**实际示例**：

| PR | Issue | 说明 |
|----|-------|------|
| #20672 vulkan: disable mmvq on Intel Windows driver | #17628 Vulkan performance degradation on A770 | Vulkan 性能退化 |
| #20296 vulkan: fix OOB check in flash_attn_mask_opt | #19955 Vulkan server crashes with DeviceLostError | Vulkan 崩溃 |
| #20059 vulkan: Fix ErrorOutOfHostMemory on Intel GPU | #19420 Qwen3-Coder crash in vulkan Intel | Intel GPU OOM |
| #19916 ggml-cuda: add mem check for fusion | #19659 Corrupted output on CUBLAS with moe | CUDA 输出损坏 |
| #20132 kv-cache: fix M-RoPE checkpoints | #20002 Qwen3.5-35b unloads automatically | KV cache 问题 |

**价值**：直接回答"这个 bug 是怎么修的"，不需要 LLM 推理。

### 2. "这个函数最近被哪些 PR 改过？"

```cypher
MATCH (pr:PullRequest)-[:TOUCHES]->(f:Function {name: 'build_graph'})
RETURN pr.number, pr.title ORDER BY pr.merged_at DESC
```

**被改动最多的函数 Top 5**：

| 函数 | 文件 | 被多少个 PR 改过 |
|------|------|-----------------|
| build_graph | tests/test-backend-ops.cpp | 97 |
| vars | tests/test-backend-ops.cpp | 97 |
| initialize_tensors | tests/test-backend-ops.cpp | 45 |
| max_nmse_err | tests/test-backend-ops.cpp | 20 |
| grad_precise | tests/test-backend-ops.cpp | 16 |

**发现**：test-backend-ops.cpp 是改动热点，几乎每个后端相关 PR 都会改它。这对代码审查很有价值——改这个文件要格外小心。

### 3. "这个 Issue 涉及哪些函数？"

```cypher
MATCH (i:Issue)-[:MENTIONS]->(f:Function)
WHERE i.number = 12091
RETURN f.name, f.file_path
```

**价值**：当用户报 bug 时，快速定位可能相关的函数。

---

## 不能回答的问题（当前局限）

### 局限 1：97.7% 的 PR 没有 TOUCHES 边

748 个 PR 中只有 17 个有 TOUCHES 边（因为只有 ~100 个 PR 拉到了 changed_files，且其中很多文件路径与图中 Function.file_path 不匹配）。

**影响**：对于绝大多数 PR，无法回答"这个 PR 改了哪些函数"。

**原因**：
- GitHub API 获取 PR 的 changed_files 需要额外请求（每个 PR 一次）
- rate limit 导致后面的 PR 没拉到文件列表
- 图中 Function.file_path 是相对路径，PR 的 changed_files 也是相对路径，但格式可能不完全一致

**改进方案**：rate limit 恢复后重新拉取所有 PR 的 changed_files。

### 局限 2：MENTIONS 边噪声严重

Issue #12091 匹配到 103 个函数，其中大量是 `main`、`write`、`load` 这种通用名。

**原因**：当前 MENTIONS 匹配逻辑是"Issue body 中出现的函数名 ∩ 图中所有函数名"，没有过滤通用词。

**实际效果**：
```
Issue #12091 -> 103 个函数（大部分是噪声）
Issue #2990  -> 97 个函数（大部分是噪声）
Issue #730   -> 88 个函数（大部分是噪声）
```

**改进方案**：
- 过滤掉长度 ≤ 5 且非 `ggml_`/`llama_` 前缀的函数名
- 只匹配 backtick 包裹的函数名（`func_name`），不匹配裸词
- 或者用 TF-IDF 权重，降低高频函数名的匹配权重

### 局限 3：CI/配置类 PR 无法关联

**具体案例**：PR #20521（ci: try to optimize some jobs）

这个 PR 改的是 `.github/workflows/*.yml`，图中没有 CI 配置文件节点，所以：
- 无 MODIFIES 边
- 无 TOUCHES 边
- MENTIONS 边也没有（CI 配置不提函数名）

**问题**："这个 PR 做了什么优化？" → 图完全无法回答

**改进方案**：
- 将 PR 的 title + body 作为节点属性存储（已做）
- QA 系统对 PR 相关问题直接读取 PR.body，不走图检索
- 或者增加 CIConfig 节点类型

### 局限 4：早期 Issue 缺乏结构化信息

**具体案例**：Issue #71（Longer and infinite output）

这是 llama.cpp 早期的 Issue，body 中只有自然语言描述和一个代码行链接，没有函数名。

**问题**："Issue #71 讨论的核心问题是什么？" → 图中无任何关联边

**改进方案**：
- 对 Issue body 做 NER 提取代码实体（文件路径、行号、错误信息）
- 或者直接将 Issue body 作为 embedding 检索的候选文档

---

## 对比：有/无 PR 节点时的 QA 能力

### 能回答 → 能回答（质量提升）

**问题**：PR #20508 修改的函数有哪些？

| 条件 | 回答质量 |
|------|---------|
| 无 PR 节点 | 只能通过函数名/文件路径模糊匹配，可能找到不相关的函数 |
| 有 PR 节点 + TOUCHES | 直接查 `(pr)-[:TOUCHES]->(f)` 得到精确结果 |

### 不能回答 → 能回答（新能力）

**问题**：哪个 PR 修复了 Vulkan 在 A770 上的性能退化？

| 条件 | 结果 |
|------|------|
| 无 PR 节点 | 完全无法回答，图中没有 bug 修复的概念 |
| 有 PR 节点 + FIXES | `PR #20672 -[:FIXES]-> Issue #17628`，一跳直达 |

### 不能回答 → 仍然不能回答

**问题**：PR #20521 的 CI 优化具体做了什么？

| 条件 | 结果 |
|------|------|
| 无 PR 节点 | 无法回答 |
| 有 PR 节点 | PR.body 中有答案，但当前 QA 系统不读 PR.body，仍然走图检索 |

**关键洞察**：PR 节点的 body 属性已经存了完整描述，但 QA pipeline 没有利用它。

---

## Token/延迟影响预估

PR/Issue 节点本身不直接增加 QA 的 token 成本（它们是图中的节点，不是注入 prompt 的文本）。但如果未来将 PR body 作为上下文注入：

| PR body 长度 | 预估增加 tokens | 占比 |
|-------------|----------------|------|
| 短（< 200 字） | ~100 tokens | +10% |
| 中（200-1000 字） | ~400 tokens | +40% |
| 长（> 1000 字） | ~800+ tokens | +80% |

**建议**：对 PR body 做摘要后再注入，控制在 200 tokens 以内。

---

## 改进路线图

1. **短期**：修复 MENTIONS 噪声（过滤通用函数名），重新拉取 PR changed_files
2. **中期**：QA pipeline 增加 PR/Issue body 直读路径，不走图检索
3. **长期**：Issue body NER 提取代码实体，CI 配置文件建模
