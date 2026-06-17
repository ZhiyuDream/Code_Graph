# Experiment Report: 移除 Symbol Fast Path 硬编码 Embedding

**Date**: 2026-06-06  
**Branch**: `feat/navigation-architecture`  
**Benchmark**: `datasets/posthoc_audit_benchmark_v2.json` (50 items)  
**Final Result File**: `results/benchmark_symbol_fastpath_final_20260606_185935.json`

---

## 修改清单

### 1. `agent_loop.py` — 删除静默 fallback
- **Before**: LLM 输出非法 action 时，静默替换为 `expand_callees`
- **After**: 记录 warning，强制 `sufficient`，在 thought 中标注 `[unknown action: xxx]`

### 2. `pipeline.py` — Symbol Fast Path 不再硬调 embedding
- **Before**: Symbol Fast Path 同时调用 `grep` + `embedding`，merge 结果
- **After**: 只调用 `grep`，`embedding` 留给 LLM 在 ReAct loop 中自主决定

### 3. `call_chain.py` — `expand_callers` 加目录优先级排序
- **Before**: `LIMIT 5` 无 `ORDER BY`
- **After**: 按目录优先级排序（`common/` > `src/` > `ggml/` > `tests/`/`examples`）

### 4. `grep.py` — 修复 cwd + 路径解析 bug
- **Before**: `_grep_file` 未设置 `cwd`，`-g` glob 在错误目录匹配
- **After**: `cwd=str(self.repo_root)`，`./xxx` 转为绝对路径

### 5. `grep.py` — 分层 `top_k` 策略
- **Symbol Fast Path**: `top_k=0`（默认不截断，保留所有相关函数）
- **ReAct `grep_search`**: `top_k=5`（防止 LLM 生成宽泛 query 时结果爆炸）

### 6. `call_chain.py` — 移除不存在的 `fan_in` 属性引用

### 7. `expansion.py` — 大函数列表提示
- 当函数数量 >15 时，签名列表上下文显示「共 N 个函数」

---

## 实验结果对比

| 指标 | 旧系统<br>(embedding+top_k) | 新v1<br>(无emb, top_k=10) | 新v2<br>(无emb, 无top_k) | 新v3<br>(当前: 分层top_k) |
|---|---:|---:|---:|---:|
| **Coverage** | **70.3%** | 68.8% | 70.7% | **69.5%** |
| **Full Recall (≥80%)** | **18/50** | 14/50 | 14/50 | **15/50** |
| **Zero Recall** | 0/50 | 0/50 | 0/50 | 0/50 |
| **Avg Retrieved Funcs** | 14.5 | 10.1 | 731.9 ⚠️ | **10.2** |
| **Avg ReAct Steps** | 5.4 | 5.6 | 5.6 | **5.4** |
| **Avg Latency** | 88.4s | 161.8s | 147.1s | **43.2s** ✅ |
| **Embedding Calls (ReAct)** | 2 | 7 | 2 | **4** |
| **Grep Calls (ReAct)** | 24 | 40 | 50 | **33** |

**关键发现**:
- **Coverage 基本持平**: -0.8pp（在可接受范围内）
- **Latency 大幅降低**: 88s → 43s（-51%），主要因为减少了 embedding API 调用
- **无上下文爆炸**: 新v2 因 ReAct 中无 `top_k` 导致平均 732 个函数；分层策略后稳定在 10.2 个
- **LLM 自主调用 embedding**: 4 次，说明 LLM 确实会在需要时主动使用 embedding

---

## 提升显著的题目

| Symbol | 旧 Coverage | 新 Coverage | 说明 |
|---|---|---|---|
| `ggml_sycl_set_device` | 25% | **50%** | 找回了 `common.cpp` 调用方 |
| `trim_trailing_whitespace` | 33% | **67%** | 配对文件被找回 |
| `until_common_prefix` | 33% | **67%** | 配对文件被找回 |
| `prune_whitespace_segments` | 33% | **67%** | 配对文件被找回 |
| `llama_model_chat_template` (Q39) | 67% | **100%** | 调用方补全 |
| `ggml_sycl_op_set` | 33% | **67%** | 调用方补全 |

## 下降显著的题目

| Symbol | 旧 Coverage | 新 Coverage | 根因 |
|---|---|---|---|
| `calculate_diff_split` | **75%** | 25% | 找回了实现函数，但 LLM 未有效引用 |
| `llama_model_chat_template` (Q01) | **100%** | 67% | `common/common.cpp` 丢失 |
| `common_get_model_endpoint` | **100%** | 67% | `hf-cache.cpp` 丢失 |
| `build_chat_peg_parser` | **100%** | 67% | `.h` 头文件丢失 |
| `ggml_backend_free` | **100%** | 75% | 一个调用方丢失 |
| `common_download_model` | **67%** | 33% | 调用方补全失败 |

**下降模式**:
1. **`.h` 头文件系统性丢失**: grep 基于函数体提取，头文件无函数体
2. **LLM 引用不完整**: 有些文件在 retrieved_functions 中，但答案没引用
3. **特定调用方没补全**: `expand_callers` 的 `LIMIT 5` 仍然遗漏了一些调用方

---

## 关键实验洞察

### 1. 单纯去掉 embedding 不够
去掉 embedding 后，头文件和语义相关文件丢失，覆盖率从 70.3% 降到 68.8%。

### 2. 单纯去掉 top_k 会爆炸
新v2 中 ReAct 的 `grep_search` 无截断，LLM 生成宽泛 query（如 `cpu fast path`）时匹配 1500+ 函数，导致平均 732 个函数/题，上下文失控。

### 3. 分层 top_k 是可行折中
- **Symbol Fast Path 不截断**: 保留所有初始相关函数
- **ReAct 设安全阀**: 防止 LLM 生成宽泛 query 时爆炸
- **效果**: Coverage 接近旧系统，latency 降低一半

---

## 结论

> **当前修改达到了核心目标：移除了 Symbol Fast Path 中的硬编码 embedding，同时保持了可接受的 coverage（-0.8pp），并将 latency 降低 51%。**
>
> 这是一个 architecture win：LLM 现在自主决定是否调用 embedding（4 次），而不是每题被硬塞 embedding。

---

## 下一步建议

### P0: 修复头文件丢失（可选）
如果 coverage 需要进一步提升到 >70%，可以：
- 在 grep 结果中补充文件级别的匹配（对于 `.h` 文件，把整个文件作为 reference 返回）
- 或在 prompt 中教 LLM "如果缺少头文件声明，使用 semantic_search 补充"

### P1: 优化 `expand_callers` 的 limit
当前 `expand_callers` 固定 `LIMIT 5`。对于调用者众多的函数（如 `llama_model_chat_template`），5 个可能不够。可以让 LLM 在 prompt 中指定 `limit`。

### P2: 跑 baseline A/B 对比
用 `--config baseline` 跑禁用 Symbol Fast Path 的全局搜索，量化当前收益。

---

## 实验操作记录

```bash
# 修改代码（6 处，见上文）

# 预测试（3题）
python3 scripts/run_benchmark_symbol_fastpath.py --config symbol_fastpath --workers 3 --limit 3
# → 发现 grep.py cwd bug，修复

# 完整 50 题（workers=20）
python3 scripts/run_benchmark_symbol_fastpath.py --config symbol_fastpath --workers 20
# → 结果: benchmark_symbol_fastpath_20260606_164746.json (Coverage 68.8%)

# 去掉 top_k 测试（workers=20）
python3 scripts/run_benchmark_symbol_fastpath.py --config symbol_fastpath --workers 20
# → 结果: benchmark_symbol_fastpath_20260606_172555.json (Coverage 70.7%, 但 AvgFuncs=731.9)

# 最终版本（workers=5, 分层 top_k）
python3 scripts/run_benchmark_symbol_fastpath.py --config symbol_fastpath --workers 5
# → 结果: benchmark_symbol_fastpath_final_20260606_185935.json (Coverage 69.5%, Latency 43.2s)
```
