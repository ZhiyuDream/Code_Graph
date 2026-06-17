# 纯Agentic (无Embedding) vs Embedding 版本对比报告

## 实验说明
- **V7 P0 (无Embedding)**: 纯Agentic，使用entity_extraction + grep_search + neo4j扩展
- **V7 P0 (有Embedding)**: 混合方案，semantic_search + grep_fallback + neo4j扩展
- **V8 (File Expansion)**: 当前最佳，semantic_search + file_expansion + neo4j扩展
- **测试集**: 360题

---

## 一、核心指标对比

| 指标 | V7无Embed | V7有Embed | V8 | 结论 |
|-----|-----------|-----------|-----|------|
| **二元正确率** | **66.1%** | **71.1%** | **77.2%** | Embedding版本更优 |
| **0-1平均分** | 0.6446 | 0.6733 | 0.6953 | 逐步提升 |
| **平均每题工具调用** | 4.87次 | 3.37次 | 7.42次 | V8记录更详细 |
| **平均延迟** | 24.4s | 28.4s | 69.4s | 无Embed更快 |
| **答案平均长度** | 1890字符 | 1859字符 | 1907字符 | 基本持平 |

---

## 二、工具调用详细对比

### 2.1 初始检索工具

| 工具 | V7无Embed | V7有Embed | V8 | 效果(个/次) |
|-----|-----------|-----------|-----|------------|
| **entity_extraction** | 360次 | - | - | 1.58 |
| **semantic_search** | 0次 | 181次 | 360次 | 5.73/5.00 |
| **grep_search** | 360次 | - | - | 2.36 |
| **grep_fallback** | - | 88次 | 207次 | 5.97/1.18 |
| **file_expansion** | - | - | **360次** | **42.68** |

**关键发现**:
- 无Embed版本完全依赖 entity_extraction + grep_search
- grep_search 平均2.36个函数/次，远低于 semantic_search (5.0)
- file_expansion 效率最高 (42.68)，是无Embed版本的 **18倍**

### 2.2 扩展工具

| 工具 | V7无Embed | V7有Embed | V8 | 效果(个/次) |
|-----|-----------|-----------|-----|------------|
| **neo4j_callers** | 410次 | 425次 | 215次 | 0.24/0.55/0.95 |
| **neo4j_callees** | 263次 | 273次 | 397次 | -/-/0.78 |

**关键发现**:
- 无Embed版本更依赖Neo4j扩展 (673次 vs 698次)
- 但Neo4j扩展效率都很低 (<1个函数/次)
- 无Embed版本的callers扩展效果最差 (0.24个/次)

### 2.3 工具效率排名

```
效率排行 (函数召回/次):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🥇 file_expansion    42.68  (V8特有)
🥈 semantic_search    5.73  (V7有Embed)
🥉 issue_search       3.00  (所有版本)
4️⃣  grep_fallback     5.97  (V7有Embed, 但触发少)
5️⃣  grep_search       2.36  (V7无Embed)
6️⃣  entity_extraction 1.58  (V7无Embed)
7️⃣  neo4j_callers     0.24-0.95
8️⃣  neo4j_callees     0.49-0.78
```

---

## 三、为什么无Embedding版本效果差？

### 3.1 失败案例分析

**典型失败模式**:
```
问题: "llama-grammar的内部结构..."
无Embed版本:
  - 召回: 1个函数
  - 步骤: 5步
  - 结果: 失败

原因分析:
  1. entity_extraction 提取关键词可能不准确
  2. grep_search 只能匹配文本，无法理解语义
  3. 找不到 "llama-grammar" 相关函数
  4. Neo4j扩展也无法补救（0.24个/次）
```

### 3.2 关键差异

| 方面 | 无Embedding | 有Embedding |
|-----|-------------|-------------|
| **初始检索** | Grep文本匹配 | 向量语义搜索 |
| **召回率** | 2.36个/次 | 5.73个/次 |
| **准确率** | 依赖关键词 | 理解语义 |
| **正确率** | 66.1% | 71.1% (+5%) |

### 3.3 无Embedding版本的问题

1. **实体提取不准确**
   - entity_extraction 平均1.58个实体/题
   - 可能提取不到核心概念

2. **Grep搜索局限**
   - 只能匹配字符串
   - 无法理解同义词、相关概念
   - 例如搜 "grammar" 找不到 "parser"

3. **扩展步骤补救有限**
   - Neo4j callers扩展效果差 (0.24个/次)
   - 初始召回少，扩展也救不回来

---

## 四、纯Agentic的价值

### 4.1 仍有66.1%正确率

尽管没有embedding，纯工具调用仍能达到66.1%正确率，说明：
- Grep搜索在明确函数名时有效
- Neo4j调用链在关系明确时有帮助
- LLM可以从代码文本中推理答案

### 4.2 速度更快

- 无Embed: 24.4s/题
- 有Embed: 28.4s/题 (+16%)
- 省去embedding API调用时间

### 4.3 适用场景

纯Agentic可能更适合：
- 明确知道函数名的查询
- 代码库较小，Grep能快速搜索
- 对延迟敏感的场景

---

## 五、完整对比表

### 工具调用总次数

| 版本 | 总调用 | entity_ext | semantic | grep | issue | neo4j | file_exp |
|-----|--------|------------|----------|------|-------|-------|----------|
| V7无Embed | 1,753 | 360 | 0 | 360 | 360 | 673 | 0 |
| V7有Embed | 1,214 | 0 | 181 | 88 | ? | 698 | 0 |
| V8 | 2,665 | 0 | 360 | 207 | 360 | 612 | 360 |

### 正确率对比

```
正确率提升路径:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
纯Agentic (无Embed):     66.1%
   ↓ +embedding
混合方案 (V7 P0):        71.1%  (+5.0%)
   ↓ +file_expansion
V8 File Expansion:       77.2%  (+6.1%)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
总提升: 11.1个百分点
```

---

## 六、结论

### 6.1 Embedding的价值

**结论**: Embedding显著提升了召回质量
- semantic_search (5.73个/次) vs grep_search (2.36个/次)
- 正确率提升5% (66.1% → 71.1%)

### 6.2 纯Agentic的局限

**结论**: 纯工具调用在语义理解上存在瓶颈
- entity_extraction + grep_search 组合效果有限
- 无法理解概念相关性
- 适合明确函数名的场景，不适合概念性问题

### 6.3 最佳实践

**推荐架构 (V8)**:
```
semantic_search (核心) + file_expansion (增强) + Neo4j扩展 (补充)
```

**为什么不推荐纯Agentic**:
- 正确率低5%，在关键场景可能是致命的
- 虽然速度快，但质量损失较大
- Embedding成本已很低，值得投入

---

**报告生成时间**: 2025-04-20  
**数据文件**: 
- `experiments/qa/v7_p0_no_embedding_360.json`
- `experiments/qa/v7_p0_no_embedding_360_evaluated.json`
