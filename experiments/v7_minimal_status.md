# V7 Minimal 测试状态

## 测试配置
- **版本**: V7 Minimal (移除 callers/callees 扩展)
- **日期**: 2026-04-10
- **测试集**: 360 题
- **并发**: 4 workers
- **输出**: results/v7_minimal_360.jsonl

## 进度追踪
- [2026-04-10 17:03] 测试启动
- [2026-04-10 17:13] 完成 25/360 题 (~7%)

## 关键改动
```python
# V7 Minimal 移除了低效扩展
- expand_callers (neo4j_callers): 80% 失败率，0.55 avg 新函数
- expand_callees (neo4j_callees): 80% 失败率，0.49 avg 新函数

保留的高效工具:
+ semantic_search: 5.73 avg 新函数
+ grep_fallback: 5.97 avg 新函数  
+ graph_search: 6.12 avg 新函数
+ issue_search: 2.31 avg 新 issues
+ explore_file: 文件级探索
```

## 预期指标
| 指标 | P0 (当前最佳) | V7 Minimal 目标 |
|------|---------------|-----------------|
| 准确率 | 74.1% | >= 72% |
| Avg Steps | 2.2 | <= 2.0 |
| Neo4j 调用/题 | ~2.5 | <= 1.5 |
| 平均延迟 | 55.3s | <= 50s |

## 日志位置
- 实时日志: `/tmp/v7_minimal.log`
- 结果文件: `results/v7_minimal_360.jsonl`

## 验证命令
```bash
# 检查进度
grep "已完成" /tmp/v7_minimal.log | tail -1

# 检查是否完成
ls -la results/v7_minimal_360.jsonl
wc -l results/v7_minimal_360.jsonl  # 应为 360 行
```

---
*状态: 运行中*
