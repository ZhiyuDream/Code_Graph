# Graph-Agent 优化方案

**基于 180 例失败案例根因分析**

---

## 一、核心发现：RAG index 有 annotation，Neo4j 没有

RAG 的 7695 个 chunk 包含完整的函数描述（`描述:` 字段），但这些 annotation 从未写回 Neo4j。

| 数据源 | 函数 annotation | 来源 |
|--------|----------------|------|
| **RAG index** | ✅ 6691 个函数全有 `描述:` | annotation 步骤已生成，存储在 `data/classic_rag_index.json` |
| **Neo4j** | ❌ 全部 6691 个函数 `annotation_json = NULL` | annotation 步骤从未写回 |

**实际影响**：以 `gemm_bloc` 为例
- RAG chunk: `描述: Compute a block of a general matrix-matrix multiplication and store the results in matrix C.`
- Neo4j: `annotation_json = NULL`

这就是为什么 RAG 答案质量远高于 Agent——RAG 用的是带描述的 chunks，Agent 的 `get_function_detail` 只返回函数名和文件。

---

## 二、优化方案（按优先级）

### P0 — 数据修复（影响 60+ 例）

**P0-1: 将 RAG index 的 annotation 写回 Neo4j**

数据已经存在，只需要写回：

```python
# 从 RAG index 提取 annotation，写入 Neo4j
import json
with open("data/classic_rag_index.json") as f:
    index = json.load(f)

for chunk in index["chunks"]:
    chunk_id = chunk["id"]  # e.g. "func::gemm_bloc::ggml/src/ggml-cpu/llamafile/sgemm.cpp"
    if not chunk_id.startswith("func::"):
        continue

    # 解析：func::name::file_path
    parts = chunk_id.split("::", 2)
    func_name = parts[1]
    file_path = parts[2]

    # 解析 description
    text = chunk["text"]
    desc = ""
    role = ""
    if "描述:" in text:
        desc = text.split("描述:")[1].split("\n")[0].strip()
    if "工作流角色:" in text:
        role = text.split("工作流角色:")[1].split("\n")[0].strip()

    annotation = json.dumps({
        "summary": desc,
        "workflow_role": role,
        "version": 1
    }, ensure_ascii=False)

    # 写入 Neo4j
    s.run("""
        MATCH (f:Function {name: $name, file_path: $fp})
        SET f.annotation_json = $ann
    """, {"name": func_name, "fp": file_path, "ann": annotation})
```

执行方式：
```bash
# 直接用 Python 脚本一次性完成（6691 个函数，批量写入）
python -c "
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path('.') / 'src'))
from neo4j_writer import get_driver
from config import NEO4J_DATABASE
# [执行上面逻辑]
"
```

**效果**：修复后 `get_function_detail` 能返回函数描述，57 例"多步但仍输"中的大部分应能改善。

---

### P0-2: 添加 fallback 工具——按文件路径搜函数

当 `find_module_by_keyword` 搜不到模块时（如 `ggml-blas`），Agent 应能搜索**文件内容中包含关键词**的函数。

**新增工具：`search_functions_by_content`**

```python
def tool_search_functions_by_content(driver, keyword: str, limit: int = 8) -> str:
    """
    通过函数名的子串搜索，但扩展到搜索文件路径中的目录名，
    解决 "ggml-blas" 搜不到但 "blas" 能找到的问题。
    """
    rows = _run(driver, """
        MATCH (f:Function)
        WHERE toLower(f.name) CONTAINS toLower($kw)
           OR toLower(f.file_path) CONTAINS toLower($kw)
        RETURN f.name AS name, f.file_path AS file,
               f.fan_in AS fan_in, f.fan_out AS fan_out
        ORDER BY f.fan_in DESC LIMIT $lim
    """, {"kw": keyword, "lim": limit})
    if not rows:
        return f"未找到名字或路径包含 '{keyword}' 的函数。"
    lines = [f"{r['name']} ({r['file']}) fan_in={r['fan_in']}" for r in rows]
    return "\n".join(lines)
```

**案例 0 修复效果**：`find_module_by_keyword('ggml-blas')` 失败后 → 调用 `search_functions_by_content('blas')` → 找到 `gemm_bloc`, `tinyBLAS` 等 → Agent 能回答。

---

### P1 — 工具层面改进（影响 90+ 例）

**P1-1: 关键词缩短 fallback（影响 ~43 例"部分名称变体"）**

当 `search_functions` 返回空时，自动截取关键词前 3-5 个字符重试：

```python
# 搜索失败后，提取前3-5个字母数字组合重试
def _try_shorter_pattern(driver, original_kw: str, tool_fn, **kwargs):
    # 提取有意义的前缀
    import re
    short_kws = re.findall(r'[\w]{3,}', original_kw)
    if not short_kws:
        return None
    # 尝试前3个有意义的词
    for kw in short_kws[:3]:
        result = tool_fn(driver, **{**kwargs, 'name_pattern': kw})
        if '未找到' not in result:
            return result
    return None
```

**P1-2: Issue 关键词截取（影响 ~10 例 Issue 搜索失败）**

`search_issues` 接收超长关键词时，自动截取前 3 个实义词重试：

```python
# 在 search_issues 工具中
if len(keyword) > 15:
    # 截取前两个词
    short_kw = " ".join(keyword.split()[:2])
    rows = _run(driver, """
        MATCH (i:Issue)
        WHERE toLower(i.title) CONTAINS toLower($kw)
           OR toLower(coalesce(i.body, '')) CONTAINS toLower($kw)
        ...
    """, {"kw": short_kw})
    # 如果短词有结果，和原始关键词结果合并去重
```

**P1-3: 类型名多工具同时搜索**

当用户问的是 `_t` 结尾的类型时（如 `ggml_gallocr_t`），Agent 应同时搜索 Function 和 Variable，不只搜 Attribute：

```python
# 工具选择策略：判断关键词是否可能是类型名（_t结尾）
def _multi_search(driver, name_pattern: str, limit: int = 8) -> str:
    results = []
    # 同时搜 Function 和 Variable
    for label in ['Function', 'Variable']:
        rows = _run(driver, f"""
            MATCH (n:{label})
            WHERE n.name CONTAINS $pat
            RETURN n.name AS name, n.file_path AS file
            ORDER BY size(n.file_path) LIMIT $lim
        """, {"pat": name_pattern, "lim": limit})
        results.extend(rows)
    if not results:
        return f"未找到"
    return "\n".join([f"{r['name']} ({r['file']})" for r in results[:limit]])
```

---

### P2 — Agent 策略层面（影响 20+ 例）

**P2-1: 强制最小探索步数**

当工具返回"未找到"后，Agent 必须再尝试至少 1 个替代策略：

```
工具返回空 → 自动触发 fallback（缩短关键词 / 换工具 / 搜相关词）
→ 仍空 → 至少再尝试 1 个相关搜索策略
→ 都失败 → 才返回"未找到"
```

**P2-2: 当 find_module_by_keyword 失败时，自动转为按文件内容搜索**

```python
# 在 agent 循环中
if tool_result == "未找到":
    # 提取关键词中的核心词
    core_kw = extract_meaningful_word(tool_name)  # e.g. "blas" from "ggml-blas"
    # 自动触发 search_functions_by_content
    result2 = tool_search_functions_by_content(driver, core_kw)
    if '未找到' not in result2:
        messages.append({"role": "tool", "tool_call_id": tc.id, "content": result2})
        # 继续，而不是直接放弃
```

**P2-3: "不知道但有关系"的答案策略**

当前 Agent 的策略是"搜不到就说不知道"。但有时搜到的信息虽然不精确，仍能推理出部分答案。

例如 Case 14 (`llama-quant`)：Agent 搜 `llama-quant` 返回空就说不知道，但 RAG 从 embedding 相似性找到了量化相关函数并推理出"将量化逻辑集中管理"。

建议 Agent 的 SYSTEM PROMPT 增加：

```
当搜索结果为空时：
- 尝试提取关键词的核心概念（如 "quant" from "llama-quant"）
- 搜索相关概念而非原词
- 如果找到相关函数，基于这些函数给出合理的推测性回答（明确说明是推测）
```

---

## 三、预期改进效果

| 修复项 | 影响案例数 | 预期 AG 分数提升 |
|--------|-----------|----------------|
| P0-1: 写回 annotation | ~57 例"多步但仍输" | +0.06 ~ +0.10 |
| P0-2: 文件路径搜索 | ~20 例模块/路径问题 | +0.03 ~ +0.05 |
| P1-1: 关键词缩短 | ~20 例部分名称变体 | +0.02 ~ +0.03 |
| P1-2: Issue 关键词截取 | ~10 例 Issue 搜索 | +0.01 ~ +0.02 |
| P2-1/2: 强制探索步数 | ~20 例过早放弃 | +0.02 ~ +0.03 |

**综合预期**：修复后 Delta 有望从 -0.145 收窄至 -0.05 ~ 0（持平或接近 RAG）。

---

## 四、优先级执行顺序

```
第一步：P0-1（写回 annotation）         → 一次性数据修复，当即可验证
第二步：P0-2（文件路径搜索）            → 新增工具，立即可用
第三步：P1 系列（fallback 机制）       → 改进工具逻辑
第四步：P2 系列（Agent 策略）          → 改进 prompt 和循环逻辑
```

---

## 五、P0-1 的具体操作（代码）

```python
#!/usr/bin/env python3
"""将 RAG index 中的 annotation 写回 Neo4j"""
import json, sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
from neo4j_writer import get_driver
from config import NEO4J_DATABASE

with open(_ROOT / "data" / "classic_rag_index.json") as f:
    index = json.load(f)

driver = get_driver()
updated = 0
errors = 0

for chunk in index["chunks"]:
    cid = chunk["id"]
    if not cid.startswith("func::"):
        continue
    parts = cid.split("::", 2)
    func_name, file_path = parts[1], parts[2]

    text = chunk["text"]
    desc = role = ""
    if "描述:" in text:
        desc = text.split("描述:", 1)[1].split("\n")[0].strip()
    if "工作流角色:" in text:
        role = text.split("工作流角色:", 1)[1].split("\n")[0].strip()

    ann = json.dumps({"summary": desc, "workflow_role": role, "source": "rag_index_sync"}, ensure_ascii=False)

    try:
        with driver.session(database=NEO4J_DATABASE) as s:
            n = s.run("""
                MATCH (f:Function {name: $name, file_path: $fp})
                SET f.annotation_json = $ann
                RETURN count(f) as cnt
            """, {"name": func_name, "fp": file_path, "ann": ann}).single()
            if n and n["cnt"] > 0:
                updated += 1
    except Exception as e:
        errors += 1

driver.close()
print(f"更新完成: {updated} 个函数 annotation 已写入, {errors} 个错误")
```
