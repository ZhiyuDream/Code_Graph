以后每次跑实验，都要有以下指标报告：
llm分数：0-1打分
或者直接正确-错误这样打分也行
token消耗
延时
调用工具步数
evidence召回率

每次做的改进，都要和上次改进之前进行对比，比如有注释无注释。rag的话跑过一版有记录就行了

如果跑的是qa可以互不干扰，请并行20个问题跑，节省我的等待时间
在judge运行的时候，也可以同时开二十个并行去跑，节省时间

每次跑完，需要找good case（改进后能回答对的问题）和bad case（改进后反而回答不对的问题）

每次新的改进记录都要写到design目录下面，记录改进了什么，改进效果如何

---

## 评估脚本使用方法

### 1. LLM Judge 评估（推荐）

使用 `tools/eval_benchmark.py` 进行统一评估：

```bash
# 基本用法
python tools/eval_benchmark.py eval -i results/xxx.json -o results/xxx_evaluated.json -w 20

# 参数说明
-i, --input     # 输入结果文件（JSON格式）
-o, --output    # 输出评估文件（默认: 输入文件.evaluated.json）
-w, --workers   # 并行数（默认8，建议20）
--binary-only   # 仅进行二元判断（更快，不打0-1分数）
```

**输入文件格式要求**：
```json
[
  {
    "index": 0,
    "具体问题": "问题文本",
    "参考答案": "参考答案文本", 
    "生成答案": "模型生成的答案"
  }
]
```

**评估结果字段**：
- `eval_binary_correct`: true/false（是否正确）
- `eval_binary_reason`: 判断原因
- `eval_graded_score`: 0-1分数
- `eval_graded_reason`: 评分理由
- `eval_embedding_sim`: Embedding相似度（参考）

### 2. 对比两个结果文件

```bash
python tools/eval_benchmark.py compare -b baseline.json -n new_result.json
```

### 3. 评估报告查看

评估完成后会自动生成 `.report.md` 文件：

```bash
cat results/xxx_evaluated.report.md
```

报告包含：
- 二元正确率统计
- 0-1分数段分布
- 按路由类型的准确率
- Embedding相似度参考

### 4. 其他评估脚本

- `score_flask.py`: Flask数据集的5维度评分（correctness/completeness/relevance/clarity/reasoning）
- `run_judge.py`: Graph-Agent vs RAG 对比评判
- `eval_graph_coverage.py`: 图谱覆盖率评估

**推荐流程**：先用 `eval_benchmark.py` 进行LLM Judge评估，需要更细致分析时再使用其他脚本。
