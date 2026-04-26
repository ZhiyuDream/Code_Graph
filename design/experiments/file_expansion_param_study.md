# 文件扩展参数对比实验设计

## 实验目的
科学确定 file_expansion 在 build_context 中的最优函数数量

## 对比参数

| 参数组 | 函数数量 | 代码长度 | 假设 |
|-------|---------|---------|------|
| A | 5个 | 500字符 | 保守策略，减少噪声 |
| B | 8个 | 500字符 | 中等覆盖 |
| C | 10个 | 500字符 | 当前设置 |
| D | 15个 | 500字符 | 激进策略，更多信息 |

## 评估指标
1. 二元正确率 (Primary)
2. 0-1平均分
3. 平均答案长度 (检查是否冗余)
4. 时延

## 实验步骤
```bash
# 修改 answer_generator.py 中的 file_exp_funcs[:N]
# 分别运行360题对比

for n in 5 8 10 15; do
    # 修改代码: file_exp_funcs[:$n]
    python experiments/module_expansion/run_qa_v8_with_file_expansion.py \
        --csv results/qav2_test.csv \
        --output results/param_study/file_exp_n${n}.json \
        --workers 20 --file-expansion
done
```

## 预期结果分析
- 如果5个和10个正确率接近 → 5个更优（更少噪声）
- 如果15个明显更好 → 需要更多上下文
- 如果10个最优 → 当前设置合理

## 决策标准
选择 **正确率最高且答案不冗余** 的参数
