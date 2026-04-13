# SWE-QA Good Case 与 Bad Case 分析报告（含参考答案对比）

## 1. 实验概述

- **评测项目**: 15个Python项目（conan, requests, astropy, reflex, pylint, sympy, xarray, pytest, flask, django, scikit-learn, streamlink, sphinx, matplotlib, sqlfluff）
- **评测方法**: Graph+RAG Hybrid 架构
- **评分维度**: Correctness, Completeness, Relevance, Clarity, Reasoning
- **满分**: 50分
- **总案例数**: 671个

---

## 2. 低分案例分布统计

| 类型 | 数量 | 占比 |
|------|------|------|
| 系统能力不足 | 84 | **75.7%** |
| API错误(tool_calls) | 22 | 19.8% |
| 网络错误(Connection) | 5 | 4.5% |

**系统能力不足的84个案例细分：**

| 问题类型 | 数量 |
|----------|------|
| Completeness不足（回答太简略） | **59个** |
| 检索+回答都有问题 | 11个 |
| 其他 | 14个 |

---

## 3. Good Case 示例（含参考答案对比）

### 案例1：满分50分 - Matplotlib概念解释

**Question:**
```
What is the exact meaning of Matplotlib's "Axes" concept and its difference from a subplot?
```

**Agent Answer (Score: 50):**
```
Matplotlib's "Axes" is a very core concept, representing a single plotting region within a Figure
with its own data coordinate system. Axes is the actual area for plotting data (line charts,
scatter plots, bar charts, etc.). It manages the coordinate system, axes, curves, markers, labels,
and is the direct carrier of data visualization.

The difference between "Axes" and "subplot":

1. Axes:
   - Represents a specific plotting area, the main region for plotting within a Figure
   - A Figure can contain multiple Axes
   - Axes objects manage their own coordinate systems, ticks, labels, and plotting commands
   - Axes can be regular 2D or 3D/polar coordinate systems

2. Subplot:
   - A convenient way to create Axes, typically dividing a Figure into a grid of small panels
   - The subplot function is a convenient method on Figure for quickly creating grid-layout Axes
   - Essentially, subplot also returns an Axes object

In short, subplot is a way to create and arrange Axes, while Axes is the actual plotting region.
A subplot is essentially an Axes object...
```

**Reference Answer:**
```
Axes is Matplotlib's core plotting object: a single plotting region within a Figure with its own data
coordinate system, XAxis/YAxis, spines, artists, limits, and all plotting methods. A "subplot" is
not a different type; it's an Axes that has been laid out as part of a regular grid (created by
subplot/subplots) and thus has an associated SubplotSpec. In short: every subplot is an Axes,
but not every Axes is a subplot—you can place Axes arbitrarily (e.g., via add_axes) outside a grid.
```

**分析:** Agent答案和参考答案核心内容一致，都解释了Axes是绘图区域、subplot是Axes的一种布局方式。Agent答案更详细、结构更好，5个维度全部满分。

---

### 案例2：49分架构分析 - Conan认证机制

**Question:**
```
What is the integration mechanism between HttpBasicAuthentication and the
AuthorizationHeader base class for processing Basic credentials in the Bottle plugin framework?
```

**Agent Answer (Score: 49):**
```
The integration mechanism between HttpBasicAuthentication and AuthorizationHeader base class
is that HttpBasicAuthentication inherits from AuthorizationHeader and implements the abstract
methods defined by the base class to process Basic credentials:

1. Inheritance relationship: HttpBasicAuthentication is a subclass of AuthorizationHeader

2. Key fields and initialization: Receives a keyword parameter (default "http_basic_credentials")
   and passes it to base class initialization

3. Auth type identifier: Implements get_authorization_type() returning "Basic"

4. Auth header parsing: Implements parse_authorization_value() for base64 decoding
...
```

**Reference Answer:**
```
The integration follows a Template Method Pattern where AuthorizationHeader provides the common
framework for processing authorization headers, and HttpBasicAuthentication implements the specific
behavior for Basic authentication.

Key Components:
1. AuthorizationHeader Base Class - provides common framework
2. HttpBasicAuthentication - implements specific Basic behavior
...
```

**分析:** 两者都正确描述了继承关系和Template Method模式。Agent展开得更细，参考答案更简洁。

---

## 4. Bad Case 示例（含参考答案对比）

### 案例1：Completeness严重不足 - 只给文件路径

**Question:**
```
Where are Pytest's built-in fixture implementations located?
```

**Agent Answer (Score: 27):**
```
Pytest's built-in fixture implementations are located in src/_pytest/fixtures.py in the source tree.
```

**Reference Answer:**
```
In Pytest's source tree under src/_pytest/ — the internal default-plugin modules.
Each built-in fixture lives in its plugin module there (e.g., fixtures.py, tmpdir.py,
monkeypatch.py, logging.py, capture.py, cacheprovider.py, etc.).
```

**评分详情:** C:3 Co:2 R:10 Cl:9 Re:3

**根因分析:**
- Agent说"在src/_pytest/fixtures.py"，技术正确但不完整
- fixture分布在多个文件中：fixtures.py, tmpdir.py, monkeypatch.py, logging.py, capture.py, cacheprovider.py等
- Relevance=10说明检索精准命中了正确文件
- Completeness=2说明Agent只给了"一句话答案"，没有展开

**这不是检索问题，是回答详尽程度问题。**

---

### 案例2：跨项目检索失效 - 答非所问

**Question (Django):**
```
How does Django handle backward compatibility when introducing new features?
```

**Agent Answer (Score: 34):**
```
Based on the analysis of llama.cpp code repository search results, I could not directly find
specific information about how Django handles backward compatibility in Issues or test cases...

However, based on general practices of mature frameworks like Django, Django typically ensures
backward compatibility when introducing new features through:

1. **Semantic Versioning**
2. **Deprecation Policy**
3. **Migration files...**
```

**Reference Answer:**
```
Django preserves backward compatibility through a formal deprecation policy:

- Deprecate-then-remove: When an incompatible change is needed, the old behavior is first
  deprecated and emits versioned deprecation warnings (e.g., RemovedInDjangoXXWarning).
  The old behavior remains for a release cycle...
```

**评分详情:** C:7 Co:5 R:7 Cl:8 Re:7

**根因分析:**
- Agent检索到了**llama.cpp**的内容，和问题完全不相关
- Agent承认"未能直接找到"，然后基于"常识"猜测回答
- 这是**跨项目检索失效**——问Django却检索到llama.cpp

**恐怖程度**: 如果Graph知识图谱构建时项目映射错误，可能导致所有非llama项目的答案都是基于llama.cpp的"常识"而非真实代码。

---

### 案例3：Completeness=3 - 检索OK但展开不足

**Question (Django):**
```
Where in Django's codebase is the "resolve" method defined for URL pattern matching?
```

**Agent Answer (Score: 31):**
```
Based on Django code repository search, the URL pattern matching related resolve method is
primarily defined in django/urls/resolvers.py...

Summary:
- Django URL pattern's resolve method is defined in django.urls.resolvers module
- This method is used to resolve URL paths and match corresponding view functions or sub-routes
- To view specific implementation, see django/urls/resolvers.py
```

**Reference Answer:**
```
In django/urls/resolvers.py, implemented as URLPattern.resolve and URLResolver.resolve.
```

**评分详情:** C:7 Co:3 R:9 Cl:7 Re:5

**根因分析:**
- Relevance=9说明检索到了正确文件和模块
- Completeness=3说明回答停在"在哪个文件"，没有展开resolve的具体实现逻辑
- 参考答案简洁明了，直接给出"URLPattern.resolve and URLResolver.resolve"
- Agent反复说"建议查看"、"未能直接读取"，缺乏深入代码的能力

---

### 案例4：实体名不存在 - 完全无法回答

**Question (pytest):**
```
What is the purpose of the AssertionRewritingHook?
```

**Agent Answer (Score: 30):**
```
The AssertionRewritingHook does not exist in the llama.cpp code repository. This name is more
like a mechanism in Python's pytest framework for assertion rewriting, aiming to rewrite assert
statements to provide more detailed and friendly assertion failure information...

Please provide more context and I can help you further analyze.
```

**Reference Answer:**
```
The AssertionRewritingHook is pytest's meta path import hook that AST-rewrites assert statements
in test-related modules. Its purpose is to turn plain asserts into code that provides rich
failure introspection (intermediate values and explanations). It is managed by pytest's config,
applies only to selected modules...
```

**评分详情:** C:7 Co:3 R:7 Cl:7 Re:6

**根因分析:**
- 检索到llama.cpp内容（不相关），而非pytest代码
- Agent能基于名称猜测功能，但无法给出准确答案
- 参考答案给出了精确的技术描述（AST-rewrite, meta path import hook等）

---

## 5. 问题根因总结

| 问题类型 | 数量 | 占比 | 根因 |
|----------|------|------|------|
| Completeness不足 | 59 | **70%** | Agent缺乏多步展开机制，检索到答案后直接返回 |
| 跨项目检索失效 | ~15 | 18% | Graph项目映射错误，问A项目却检索B项目 |
| 实体不存在 | ~10 | 12% | 问题实体名在代码库中不存在 |

### 5.1 Completeness不足（最主要问题）

```
表现：检索精准命中，但回答只有一句话
Agent: "在src/_pytest/fixtures.py文件中"
参考: "分布在fixtures.py, tmpdir.py, monkeypatch.py, logging.py, capture.py, cacheprovider.py等"

改进方向：
- 增加multi-step retrieval深度
- 引导Agent展开代码逻辑
- 设计Completeness评分反馈机制
```

### 5.2 跨项目检索失效（最恐怖问题）

```
表现：问Django却检索到llama.cpp
根因：Graph知识图谱构建或检索时项目filter失效

影响：
- 如果Graph里只有llama.cpp数据，那所有非llama项目的答案都是"基于常识猜测"
- 这会导致整个评测结果完全失真

验证方法：
- 检查Neo4j中是否只有llama.cpp数据
- 检查sweqa评测时是否正确构建了各项目的Graph
```

### 5.3 实体名不存在

```
表现：类名/方法名在代码库中不存在
Agent: "未能找到XXX，请提供更多上下文"
改进：增加实体识别和名称映射能力
```

---

## 6. 后续改进建议

1. **Completeness提升（优先级最高）**
   - 增加multi-step retrieval深度
   - 引导Agent展开更多实现细节

2. **项目级检索filter验证**
   - 确认评测时各项目的Graph是否正确构建
   - 确认检索时是否严格限定在目标项目内

3. **错误恢复机制**
   - 实体不存在时给出更友好的提示
   - 增加网络错误重试

---

*评测时间: 2026-04-06 ~ 2026-04-07*
