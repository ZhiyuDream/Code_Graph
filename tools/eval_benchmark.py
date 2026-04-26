#!/usr/bin/env python3
"""
统一的 Benchmark 评估脚本：支持二元判断和 0-1 分数段评估

评估维度：
1. 二元判断 (Binary): 正确 / 错误
2. 0-1 分数段 (Graded): 0.0 - 1.0 连续分数

评估方法：
- LLM-as-Judge: 使用 LLM 判断答案正确性
- Embedding Similarity: 使用向量相似度作为参考
- Exact Match: 关键词匹配（辅助）

用法：
  # 评估单个结果文件
  python eval_benchmark.py --input results/qa_benchmark_xxx.json --output eval_report.json

  # 只进行二元判断（快速）
  python eval_benchmark.py --input results.json --binary-only

  # 对比两个结果文件
  python eval_benchmark.py compare --baseline baseline.json --new new.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# 添加项目路径
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

from config import OPENAI_API_KEY, OPENAI_BASE_URL, LLM_MODEL, NEO4J_DATABASE


# ============================================================================
# 评估 Prompts
# ============================================================================

BINARY_JUDGE_PROMPT = """请判断「生成答案」是否正确回答了问题。

判断标准：
- 正确 (CORRECT): 生成答案准确回答了问题，核心信息正确，无重大错误
- 错误 (INCORRECT): 生成答案与问题无关、信息错误、或未回答问题

必须首行输出：结果: CORRECT 或 结果: INCORRECT
第二行起：简要说明理由（1-2句话）

【问题】
{question}

【参考答案】
{reference}

【生成答案】
{generated}
"""

GRADED_JUDGE_PROMPT = """请评估「生成答案」的质量，给出 0-1 的分数。

评分标准：
- 1.0: 完美回答，全面准确，与参考答案一致
- 0.8-0.9: 回答良好，主要信息正确，可能有 minor 遗漏
- 0.6-0.7: 部分正确，核心信息有但不够完整或有 small 错误
- 0.4-0.5: 勉强相关，有明显错误或大量遗漏
- 0.2-0.3: 回答质量差，与问题相关性低
- 0.0-0.1: 完全错误或未回答问题

必须首行输出：分数: [0.0-1.0 之间的数字]
第二行起：说明理由

【问题】
{question}

【参考答案】
{reference}

【生成答案】
{generated}
"""


# ============================================================================
# LLM Judge 函数
# ============================================================================

def binary_judge(client, question: str, reference: str, generated: str) -> dict[str, Any]:
    """
    二元判断：返回是否正确
    """
    import re
    
    # 确保是字符串
    question = str(question) if question else ""
    reference = str(reference) if reference else ""
    generated = str(generated) if generated else ""
    
    prompt = BINARY_JUDGE_PROMPT.format(
        question=question[:500],
        reference=reference[:2000],
        generated=generated[:2000],
    )
    
    try:
        kwargs = {
            'model': LLM_MODEL,
            'messages': [{"role": "user", "content": prompt}],
            'timeout': 60,
        }
        if LLM_MODEL.startswith('gpt-5') or LLM_MODEL.startswith('o1') or LLM_MODEL.startswith('o3'):
            kwargs['max_completion_tokens'] = 300
        else:
            kwargs['max_tokens'] = 300
        resp = client.chat.completions.create(**kwargs)
        text = (resp.choices[0].message.content or "").strip()
        
        # 解析结果
        is_correct = None
        if re.search(r'结果\s*[:：]\s*CORRECT', text, re.IGNORECASE):
            is_correct = True
        elif re.search(r'结果\s*[:：]\s*INCORRECT', text, re.IGNORECASE):
            is_correct = False
        
        # 提取理由（第二行起）
        lines = text.split('\n')
        reason = ""
        for i, line in enumerate(lines):
            if re.search(r'结果\s*[:：]', line):
                reason = '\n'.join(lines[i+1:]).strip()
                break
        
        return {
            "is_correct": is_correct,
            "reason": reason or text,
            "raw_response": text,
        }
    except Exception as e:
        return {
            "is_correct": None,
            "reason": f"评判失败: {e}",
            "raw_response": "",
        }


def graded_judge(client, question: str, reference: str, generated: str) -> dict[str, Any]:
    """
    0-1 分数段评估
    """
    import re
    
    # 确保是字符串
    question = str(question) if question else ""
    reference = str(reference) if reference else ""
    generated = str(generated) if generated else ""
    
    prompt = GRADED_JUDGE_PROMPT.format(
        question=question[:500],
        reference=reference[:2000],
        generated=generated[:2000],
    )
    
    try:
        kwargs = {
            'model': LLM_MODEL,
            'messages': [{"role": "user", "content": prompt}],
            'timeout': 60,
        }
        if LLM_MODEL.startswith('gpt-5') or LLM_MODEL.startswith('o1') or LLM_MODEL.startswith('o3'):
            kwargs['max_completion_tokens'] = 300
        else:
            kwargs['max_tokens'] = 300
        resp = client.chat.completions.create(**kwargs)
        text = (resp.choices[0].message.content or "").strip()
        
        # 解析分数
        score = None
        m = re.search(r'分数\s*[:：]\s*(0?\.\d+|1\.0|0|1)\b', text)
        if m:
            try:
                score = float(m.group(1))
                score = max(0.0, min(1.0, score))  # 限制在 0-1 范围
            except:
                pass
        
        # 提取理由
        lines = text.split('\n')
        reason = ""
        for i, line in enumerate(lines):
            if re.search(r'分数\s*[:：]', line):
                reason = '\n'.join(lines[i+1:]).strip()
                break
        
        return {
            "score": score,
            "reason": reason or text,
            "raw_response": text,
        }
    except Exception as e:
        return {
            "score": None,
            "reason": f"评判失败: {e}",
            "raw_response": "",
        }


def embedding_similarity(client, reference: str, generated: str) -> float | None:
    """
    使用 embedding 相似度作为参考指标
    """
    from config import EMBEDDING_MODEL
    
    if not reference.strip() or not generated.strip():
        return None
    
    try:
        resp = client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=[reference[:1000], generated[:1000]],
        )
        a = resp.data[0].embedding
        b = resp.data[1].embedding
        
        # 计算余弦相似度
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(y * y for y in b) ** 0.5
        
        if na and nb:
            return round(dot / (na * nb), 4)
    except Exception:
        pass
    
    return None


# ============================================================================
# 主评估函数
# ============================================================================

def evaluate_item(client, item: dict, binary_only: bool = False) -> dict:
    """
    评估单个题目
    """
    # 获取字段并确保是字符串
    question = str(item.get("具体问题", "") or "")
    reference = str(item.get("参考答案", "") or "")
    generated = str(item.get("生成答案", "") or "")
    
    # 处理可能的非字符串类型（如 NaN, float 等）
    if question.lower() in ('nan', 'none', 'null'):
        question = ""
    if reference.lower() in ('nan', 'none', 'null'):
        reference = ""
    if generated.lower() in ('nan', 'none', 'null'):
        generated = ""
    
    if not generated:
        return {
            "binary_correct": False,
            "binary_reason": "无生成答案",
            "graded_score": 0.0,
            "graded_reason": "无生成答案",
            "embedding_sim": None,
        }
    
    # 二元判断
    binary_result = binary_judge(client, question, reference, generated)
    
    result = {
        "binary_correct": binary_result["is_correct"],
        "binary_reason": binary_result["reason"],
    }
    
    # 0-1 分数段评估（如果不是仅二元模式）
    if not binary_only:
        graded_result = graded_judge(client, question, reference, generated)
        result["graded_score"] = graded_result["score"]
        result["graded_reason"] = graded_result["reason"]
        
        # Embedding 相似度
        result["embedding_sim"] = embedding_similarity(client, reference, generated)
    
    return result


def evaluate_single(args):
    """
    评估单个题目的包装函数（用于多线程）
    """
    client, item, binary_only = args
    try:
        result = evaluate_item(client, item, binary_only)
        return item, result, None
    except Exception as e:
        return item, None, str(e)


def evaluate_all(input_path: Path, output_path: Path, binary_only: bool = False, workers: int = 8):
    """
    评估所有题目（并行版本）
    
    Args:
        input_path: 输入结果 JSON 文件
        output_path: 输出评估结果文件
        binary_only: 仅进行二元判断
        workers: 并行工作线程数（默认 8，建议根据 API 并发限制调整）
    """
    from openai import OpenAI
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading
    
    if not OPENAI_API_KEY:
        print("ERROR: OPENAI_API_KEY 未设置", file=sys.stderr)
        sys.exit(1)
    
    client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)
    
    # 读取结果
    print(f"读取结果文件: {input_path}")
    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    total = len(data)
    print(f"共 {total} 题需要评估")
    print(f"评估模式: {'仅二元判断' if binary_only else '二元 + 0-1分数段'}")
    print(f"并行线程数: {workers}")
    print(f"预计时间: {total // workers * 2}-{total // workers * 3} 秒（取决于 API 响应速度）\n")
    
    # 准备需要评估的题目
    pending_items = []
    for item in data:
        if "eval_binary_correct" not in item:
            pending_items.append(item)
    
    if not pending_items:
        print("所有条目已有评估结果，跳过")
        return data
    
    print(f"需要评估: {len(pending_items)}/{total} 题\n")
    
    # 线程锁用于保存文件
    save_lock = threading.Lock()
    completed = 0
    errors = 0
    
    def save_progress():
        """保存进度（带锁）"""
        with save_lock:
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
    
    # 使用线程池并行评估
    with ThreadPoolExecutor(max_workers=workers) as executor:
        # 提交所有任务
        future_to_item = {
            executor.submit(evaluate_single, (client, item, binary_only)): item 
            for item in pending_items
        }
        
        # 处理完成的任务
        for future in as_completed(future_to_item):
            item, result, error = future.result()
            
            if error:
                print(f"  评估失败 (index={item.get('index', '?')}): {error}")
                errors += 1
                # 记录错误但继续
                item["eval_binary_correct"] = None
                item["eval_binary_reason"] = f"评估错误: {error}"
            else:
                # 合并评估结果
                item["eval_binary_correct"] = result["binary_correct"]
                item["eval_binary_reason"] = result["binary_reason"]
                
                if not binary_only:
                    item["eval_graded_score"] = result["graded_score"]
                    item["eval_graded_reason"] = result["graded_reason"]
                    item["eval_embedding_sim"] = result["embedding_sim"]
            
            completed += 1
            
            # 每 10 题打印进度
            if completed % 10 == 0:
                print(f"  已评估 {completed}/{len(pending_items)} 题...")
            
            # 每 20 题保存一次
            if completed % 20 == 0:
                save_progress()
    
    # 最终保存
    save_progress()
    
    print(f"\n评估完成: {completed} 题成功, {errors} 题失败")
    print(f"结果保存至: {output_path}")
    return data


def generate_report(data: list, binary_only: bool = False) -> str:
    """
    生成评估报告
    """
    lines = [
        "# Benchmark 评估报告",
        "",
        f"评估题目数: {len(data)}",
        "",
        "## 1. 二元判断统计",
    ]
    
    # 二元统计
    binary_results = [r for r in data if r.get("eval_binary_correct") is not None]
    if binary_results:
        correct_count = sum(1 for r in binary_results if r["eval_binary_correct"])
        accuracy = correct_count / len(binary_results) if binary_results else 0
        
        lines.extend([
            f"",
            f"| 指标 | 数值 |",
            f"|------|------|",
            f"| 已评估 | {len(binary_results)} |",
            f"| 正确 | {correct_count} |",
            f"| 错误 | {len(binary_results) - correct_count} |",
            f"| **正确率** | **{accuracy*100:.1f}%** |",
        ])
    
    # 0-1 分数段统计
    if not binary_only:
        lines.extend([
            "",
            "## 2. 0-1 分数段统计",
        ])
        
        graded_results = [r for r in data if r.get("eval_graded_score") is not None]
        if graded_results:
            scores = [r["eval_graded_score"] for r in graded_results]
            avg_score = sum(scores) / len(scores) if scores else 0
            
            # 分数段分布
            bins = [
                (0.0, 0.3, "差"),
                (0.3, 0.5, "较差"),
                (0.5, 0.7, "一般"),
                (0.7, 0.9, "良好"),
                (0.9, 1.0, "优秀"),
            ]
            
            lines.extend([
                f"",
                f"| 分数段 | 题数 | 占比 | 评价 |",
                f"|--------|------|------|------|",
            ])
            
            for lo, hi, label in bins:
                count = sum(1 for s in scores if lo <= s < hi or (hi == 1.0 and s == 1.0))
                pct = count / len(scores) * 100 if scores else 0
                lines.append(f"| [{lo}, {hi}) | {count} | {pct:.1f}% | {label} |")
            
            lines.extend([
                f"",
                f"**平均分**: {avg_score:.4f}",
                f"**中位数**: {sorted(scores)[len(scores)//2]:.4f}" if scores else "",
            ])
            
            # Embedding 相似度
            sim_results = [r for r in data if r.get("eval_embedding_sim") is not None]
            if sim_results:
                sims = [r["eval_embedding_sim"] for r in sim_results]
                avg_sim = sum(sims) / len(sims) if sims else 0
                lines.extend([
                    "",
                    "## 3. Embedding 相似度（参考）",
                    f"",
                    f"平均相似度: {avg_sim:.4f}",
                    f"（余弦相似度，越高表示与参考答案越接近）",
                ])
    
    # 按路由类型统计
    lines.extend([
        "",
        "## 4. 按路由类型统计",
        "",
    ])
    
    by_route = {}
    for r in data:
        rt = r.get("路由类型", "unknown")
        if rt not in by_route:
            by_route[rt] = []
        by_route[rt].append(r)
    
    lines.append("| 路由类型 | 题数 | 二元正确率 | 平均分数 |")
    lines.append("|----------|------|------------|----------|")
    
    for rt in sorted(by_route.keys()):
        group = by_route[rt]
        binary_group = [r for r in group if r.get("eval_binary_correct") is not None]
        graded_group = [r for r in group if r.get("eval_graded_score") is not None]
        
        binary_acc = "—"
        if binary_group:
            correct = sum(1 for r in binary_group if r["eval_binary_correct"])
            binary_acc = f"{correct/len(binary_group)*100:.1f}%"
        
        avg_score = "—"
        if graded_group:
            scores = [r["eval_graded_score"] for r in graded_group]
            avg_score = f"{sum(scores)/len(scores):.4f}"
        
        lines.append(f"| {rt} | {len(group)} | {binary_acc} | {avg_score} |")
    
    return "\n".join(lines)


def cmd_eval(args):
    """执行评估命令"""
    input_path = Path(args.input)
    output_path = Path(args.output) if args.output else input_path.with_suffix(".evaluated.json")
    
    # 评估
    data = evaluate_all(input_path, output_path, args.binary_only, args.workers)
    
    # 生成报告
    report = generate_report(data, args.binary_only)
    
    # 保存报告
    report_path = output_path.with_suffix(".report.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    
    print(f"\n报告保存至: {report_path}")
    print("\n" + "=" * 50)
    print(report)


def cmd_compare(args):
    """对比两个结果文件"""
    baseline_path = Path(args.baseline)
    new_path = Path(args.new)
    
    with open(baseline_path, "r", encoding="utf-8") as f:
        baseline = json.load(f)
    with open(new_path, "r", encoding="utf-8") as f:
        new_data = json.load(f)
    
    print("# Benchmark 对比报告")
    print(f"\n基准: {baseline_path.name}")
    print(f"新结果: {new_path.name}")
    
    # 计算指标
    def calc_metrics(data):
        binary = [r for r in data if r.get("eval_binary_correct") is not None]
        graded = [r for r in data if r.get("eval_graded_score") is not None]
        
        binary_acc = sum(1 for r in binary if r["eval_binary_correct"]) / len(binary) if binary else 0
        avg_score = sum(r["eval_graded_score"] for r in graded) / len(graded) if graded else 0
        
        return {
            "binary_acc": binary_acc,
            "avg_score": avg_score,
            "count": len(data),
        }
    
    base_metrics = calc_metrics(baseline)
    new_metrics = calc_metrics(new_data)
    
    print(f"\n| 指标 | 基准 | 新结果 | 变化 |")
    print(f"|------|------|--------|------|")
    print(f"| 二元正确率 | {base_metrics['binary_acc']*100:.1f}% | {new_metrics['binary_acc']*100:.1f}% | {(new_metrics['binary_acc']-base_metrics['binary_acc'])*100:+.1f}% |")
    print(f"| 平均分数 | {base_metrics['avg_score']:.4f} | {new_metrics['avg_score']:.4f} | {new_metrics['avg_score']-base_metrics['avg_score']:+.4f} |")


def main():
    parser = argparse.ArgumentParser(
        description="统一的 Benchmark 评估脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    
    subparsers = parser.add_subparsers(dest="command", help="命令")
    
    # eval 命令
    eval_parser = subparsers.add_parser("eval", help="评估结果文件")
    eval_parser.add_argument("--input", "-i", required=True, help="输入结果 JSON 文件")
    eval_parser.add_argument("--output", "-o", help="输出文件（默认: 输入文件.evaluated.json）")
    eval_parser.add_argument("--binary-only", action="store_true", help="仅进行二元判断（更快）")
    eval_parser.add_argument("--workers", "-w", type=int, default=4, help="并行数")
    
    # compare 命令
    compare_parser = subparsers.add_parser("compare", help="对比两个结果文件")
    compare_parser.add_argument("--baseline", "-b", required=True, help="基准结果文件")
    compare_parser.add_argument("--new", "-n", required=True, help="新结果文件")
    
    args = parser.parse_args()
    
    if args.command == "eval":
        cmd_eval(args)
    elif args.command == "compare":
        cmd_compare(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
