import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT / "src"))   # 核心库
sys.path.insert(0, str(_ROOT))             # 根目录（config.py）

#!/usr/bin/env python3
"""
生成 v3 QA 数据集：三类问题
  1. Issue 驱动题（~40%）：从真实 Issue 出发，人类视角的问题
  2. 代码理解题（~30%）：SWE-QA 风格，基于图中实体
  3. 工作流题（~30%）：开发者决策支持

用法：python generate_qa_v3.py
"""
from __future__ import annotations

import json
import re
import random
import pandas as pd
from pathlib import Path

from neo4j_writer import get_driver
from config import NEO4J_DATABASE

random.seed(42)
OUTPUT = Path(__file__).resolve().parent / "llama_cpp_QA_v3.csv"
GITHUB_DATA = Path(__file__).resolve().parent / "experiments" / "github_pr_issue_data.json"


def clean_issue_body(body: str, max_len: int = 300) -> str:
    """清洗 Issue body，去掉 markdown 模板、checklist 等噪声"""
    if not body:
        return ""
    # 去掉 markdown 模板头（# 或 ### 开头的模板段落）
    body = re.sub(r'#{1,4}\s*(Name and Version|Prerequisites|Operating systems?|GGML backends?|'
                  r'Expected Behavior|Current Behavior|Steps to Reproduce|Environment|'
                  r'Please answer the following|Context|Hardware|Problem description|'
                  r'Possible Solution|First Bad Commit|Relevant log output|'
                  r'Anything else|Which llama\.cpp modules|Command line|Feature Description|'
                  r'Possible Implementation|steps to reproduce)[^\n]*\n?', '', body, flags=re.IGNORECASE)
    # 去掉 checklist
    body = re.sub(r'- \[[ xX✅]\] .+\n?', '', body)
    # 去掉代码块
    body = re.sub(r'```[\s\S]*?```', '', body)
    # 去掉版本号行和技术日志
    body = re.sub(r'version: \d+.*\n?', '', body)
    body = re.sub(r'built with .*\n?', '', body)
    body = re.sub(r'load_backend:.*\n?', '', body)
    body = re.sub(r'ggml_\w+_init:.*\n?', '', body)
    body = re.sub(r'llama-\w+ --version.*\n?', '', body)
    body = re.sub(r'\./llama-\w+.*\n?', '', body)
    body = re.sub(r'commit [0-9a-f]{10,}.*\n?', '', body)
    body = re.sub(r'Author:.*\n?', '', body)
    # 去掉 "Please answer the following..." 整句
    body = re.sub(r'Please answer the following[^.]*\.', '', body)
    # 去掉 URL
    body = re.sub(r'https?://\S+', '', body)
    # 去掉 markdown 图片/链接残留
    body = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', body)
    # 去掉连续空行和空白
    body = re.sub(r'\n{2,}', '\n', body)
    body = body.strip()
    # 取有意义的行（>15字符，不是纯技术日志）
    lines = []
    for l in body.split('\n'):
        l = l.strip()
        if len(l) < 15:
            continue
        if l.startswith(('Device ', 'compute capability', 'llama_', 'ggml_', '- [', 'I am running')):
            continue
        if re.match(r'^[\d.]+\s*$', l):  # 纯版本号
            continue
        lines.append(l)
    result = ' '.join(lines)
    return result[:max_len] if result else ""


def load_github_data():
    data = json.loads(GITHUB_DATA.read_text(encoding="utf-8"))
    issues_by_num = {i["number"]: i for i in data["issues"]}
    prs_by_num = {p["number"]: p for p in data["prs"]}
    return issues_by_num, prs_by_num


def query_graph(driver, db):
    """从图中提取所有需要的数据"""
    result = {}
    with driver.session(database=db) as s:
        # FIXES 边
        r = s.run("""
            MATCH (pr:PullRequest)-[:FIXES]->(i:Issue)
            RETURN pr.number AS pr_num, pr.title AS pr_title, pr.body AS pr_body,
                   i.number AS issue_num, i.title AS issue_title, i.body AS issue_body
        """)
        result["fixes"] = [dict(rec) for rec in r]

        # 高 fan_in 函数（被很多函数调用的基础设施）
        r = s.run("""
            MATCH (f:Function)
            WHERE f.fan_in >= 10 AND f.file_path IS NOT NULL AND f.annotation_json IS NOT NULL
            RETURN f.name AS name, f.file_path AS file, f.fan_in AS fan_in, f.fan_out AS fan_out,
                   f.annotation_json AS ann
            ORDER BY f.fan_in DESC LIMIT 50
        """)
        result["high_fan_in"] = [dict(rec) for rec in r]

        # 高 fan_out 函数（复杂调度函数）
        r = s.run("""
            MATCH (f:Function)
            WHERE f.fan_out >= 8 AND f.file_path IS NOT NULL AND f.annotation_json IS NOT NULL
            RETURN f.name AS name, f.file_path AS file, f.fan_in AS fan_in, f.fan_out AS fan_out,
                   f.annotation_json AS ann
            ORDER BY f.fan_out DESC LIMIT 50
        """)
        result["high_fan_out"] = [dict(rec) for rec in r]

        # 叶子函数（被调用但不调用别人）
        r = s.run("""
            MATCH (f:Function)
            WHERE f.is_leaf = true AND f.fan_in >= 3 AND f.annotation_json IS NOT NULL
            RETURN f.name AS name, f.file_path AS file, f.fan_in AS fan_in,
                   f.annotation_json AS ann
            ORDER BY f.fan_in DESC LIMIT 30
        """)
        result["leaf_funcs"] = [dict(rec) for rec in r]

        # 被最多 PR 改动的函数
        r = s.run("""
            MATCH (pr:PullRequest)-[:TOUCHES]->(f:Function)
            WHERE f.annotation_json IS NOT NULL
            RETURN f.name AS name, f.file_path AS file, f.fan_in AS fan_in, f.fan_out AS fan_out,
                   f.annotation_json AS ann, count(DISTINCT pr) AS pr_count,
                   collect(DISTINCT pr.title)[..5] AS pr_titles
            ORDER BY pr_count DESC LIMIT 30
        """)
        result["hot_funcs"] = [dict(rec) for rec in r]

        # 文件级统计
        r = s.run("""
            MATCH (f:Function)
            WHERE f.file_path IS NOT NULL
            RETURN f.file_path AS file, count(f) AS func_count,
                   collect(f.name)[..10] AS funcs,
                   sum(f.fan_in) AS total_fan_in, sum(f.fan_out) AS total_fan_out
            ORDER BY func_count DESC LIMIT 30
        """)
        result["file_stats"] = [dict(rec) for rec in r]

        # 调用链示例 - 跨文件优先，打散来源
        r = s.run("""
            MATCH (caller:Function)-[:CALLS]->(callee:Function)
            WHERE caller.fan_out >= 3 AND callee.fan_in >= 3
                  AND caller.annotation_json IS NOT NULL AND callee.annotation_json IS NOT NULL
                  AND caller.file_path <> callee.file_path
                  AND NOT caller.file_path CONTAINS 'test'
                  AND NOT callee.file_path CONTAINS 'test'
            RETURN caller.name AS caller, caller.file_path AS caller_file,
                   callee.name AS callee, callee.file_path AS callee_file,
                   caller.annotation_json AS caller_ann, callee.annotation_json AS callee_ann
            LIMIT 80
        """)
        result["call_pairs"] = [dict(rec) for rec in r]

        # Issue 按类型
        r = s.run("""
            MATCH (i:Issue)
            RETURN i.number AS num, i.title AS title, i.body AS body, i.labels AS labels
        """)
        result["all_issues"] = [dict(rec) for rec in r]

        # 跨文件调用（不同文件之间的依赖）
        r = s.run("""
            MATCH (caller:Function)-[:CALLS]->(callee:Function)
            WHERE caller.file_path <> callee.file_path
                  AND caller.annotation_json IS NOT NULL AND callee.annotation_json IS NOT NULL
                  AND caller.fan_out >= 3
            RETURN caller.name AS caller, caller.file_path AS caller_file,
                   callee.name AS callee, callee.file_path AS callee_file,
                   caller.annotation_json AS caller_ann, callee.annotation_json AS callee_ann
            LIMIT 30
        """)
        result["cross_file_calls"] = [dict(rec) for rec in r]

        # 中等复杂度函数（放宽条件，排除测试和vendor）
        r = s.run("""
            MATCH (f:Function)
            WHERE f.fan_in >= 2 AND f.fan_in <= 20 AND f.fan_out >= 2 AND f.fan_out <= 15
                  AND f.annotation_json IS NOT NULL AND f.file_path IS NOT NULL
                  AND NOT f.file_path CONTAINS 'test'
                  AND NOT f.file_path CONTAINS 'vendor'
            RETURN f.name AS name, f.file_path AS file, f.fan_in AS fan_in, f.fan_out AS fan_out,
                   f.annotation_json AS ann
            ORDER BY f.fan_in * f.fan_out DESC LIMIT 30
        """)
        result["mid_funcs"] = [dict(rec) for rec in r]

        # 同文件内函数对（排除测试和vendor，每个文件只取一对，确保多样性）
        r = s.run("""
            MATCH (f1:Function)-[:CALLS]->(f2:Function)
            WHERE f1.file_path = f2.file_path AND f1.name <> f2.name
                  AND f1.annotation_json IS NOT NULL AND f2.annotation_json IS NOT NULL
                  AND NOT f1.file_path CONTAINS 'test'
                  AND NOT f1.file_path CONTAINS 'vendor'
            WITH f1.file_path AS file,
                 collect({func1: f1.name, func2: f2.name, ann1: f1.annotation_json,
                          ann2: f2.annotation_json, f1_out: f1.fan_out, f2_in: f2.fan_in})[0] AS pair
            RETURN file, pair.func1 AS func1, pair.func2 AS func2,
                   pair.ann1 AS ann1, pair.ann2 AS ann2,
                   pair.f1_out AS f1_out, pair.f2_in AS f2_in
            ORDER BY file LIMIT 20
        """)
        result["same_file_calls"] = [dict(rec) for rec in r]

    return result


def get_summary(ann_json):
    """从 annotation_json 提取 summary"""
    if not ann_json:
        return ""
    try:
        ann = json.loads(ann_json) if isinstance(ann_json, str) else ann_json
        return ann.get("summary", "")
    except:
        return ""


def generate_issue_driven(graph_data, issues_by_num, prs_by_num):
    """第一类：Issue 驱动题（人类真实遇到的问题）"""
    rows = []

    # 1a. 有 FIXES 边的：完整的 问题→修复 链路
    for fix in graph_data["fixes"]:
        issue_body = clean_issue_body(fix["issue_body"], 300)
        pr_body = clean_issue_body(fix["pr_body"], 300)
        issue_title = fix["issue_title"]

        # 提取症状关键词
        if "crash" in issue_title.lower() or "error" in issue_title.lower():
            q_template = "我在使用 llama.cpp 时遇到了类似的问题：{symptom}。可能是什么原因导致的？应该怎么排查？"
        elif "performance" in issue_title.lower() or "slow" in issue_title.lower() or "degradation" in issue_title.lower():
            q_template = "我发现 llama.cpp 的{component}性能下降了，可能是什么原因？有没有已知的修复方案？"
        else:
            q_template = "llama.cpp 中出现了这个问题：{symptom}。这是什么原因？社区是怎么解决的？"

        # 从 title 提取症状和组件
        symptom = issue_title.split(": ", 1)[-1] if ": " in issue_title else issue_title
        component = issue_title.split(": ")[0].replace("Eval bug", "").strip() if ": " in issue_title else "某模块"
        if not component or component == "Misc. bug":
            component = "某模块"

        question = q_template.format(symptom=symptom[:80], component=component)
        answer = f"这个问题在 Issue #{fix['issue_num']} 中有记录。{issue_body[:200]}\n\n修复方案：PR #{fix['pr_num']}（{fix['pr_title']}）。{pr_body[:200]}"
        evidence = f"Issue #{fix['issue_num']}, PR #{fix['pr_num']}"

        rows.append({
            "类别": "Issue驱动", "子类": "Bug修复链路",
            "问题": question,
            "答案": answer,
            "Evidence": evidence,
            "实体": f"Issue #{fix['issue_num']}",
        })

    # 1b. Bug 类 Issue（无 FIXES 边但有丰富描述）
    bug_issues = [i for i in graph_data["all_issues"]
                  if any(kw in (i["title"] or "").lower() for kw in ["bug", "error", "crash", "fail", "broken"])]
    random.shuffle(bug_issues)
    for issue in bug_issues[:15]:
        body = clean_issue_body(issue["body"], 300)
        if len(body) < 30:
            continue
        title = issue["title"]
        symptom = title.split(": ", 1)[-1] if ": " in title else title

        question = f"我遇到了这个问题：{symptom[:80]}。有人遇到过类似的情况吗？可能的原因和解决方向是什么？"
        answer = f"Issue #{issue['num']} 记录了这个问题。{body}"
        rows.append({
            "类别": "Issue驱动", "子类": "Bug排查",
            "问题": question,
            "答案": answer,
            "Evidence": f"Issue #{issue['num']}",
            "实体": f"Issue #{issue['num']}",
        })

    # 1c. Feature Request 类 Issue
    feat_issues = [i for i in graph_data["all_issues"]
                   if any(kw in (i["title"] or "").lower() for kw in ["feature", "support", "request", "add", "implement"])]
    random.shuffle(feat_issues)
    for issue in feat_issues[:8]:
        body = clean_issue_body(issue["body"], 300)
        if len(body) < 30:
            continue
        title = issue["title"]
        feature = title.split(": ", 1)[-1] if ": " in title else title

        question = f"llama.cpp 目前支持 {feature[:60]} 吗？社区有没有相关的讨论或计划？"
        answer = f"Issue #{issue['num']} 讨论了这个需求。{body}"
        rows.append({
            "类别": "Issue驱动", "子类": "Feature需求",
            "问题": question,
            "答案": answer,
            "Evidence": f"Issue #{issue['num']}",
            "实体": f"Issue #{issue['num']}",
        })

    # 1d. Performance 类 Issue
    perf_issues = [i for i in graph_data["all_issues"]
                   if any(kw in (i["title"] or "").lower() for kw in ["perf", "slow", "speed", "optim", "memory", "oom"])]
    random.shuffle(perf_issues)
    for issue in perf_issues[:6]:
        body = clean_issue_body(issue["body"], 300)
        if len(body) < 30:
            continue
        title = issue["title"]
        question = f"llama.cpp 推理时遇到性能问题：{title.split(': ', 1)[-1][:60]}。有什么优化建议？"
        answer = f"Issue #{issue['num']} 讨论了类似问题。{body}"
        rows.append({
            "类别": "Issue驱动", "子类": "性能问题",
            "问题": question,
            "答案": answer,
            "Evidence": f"Issue #{issue['num']}",
            "实体": f"Issue #{issue['num']}",
        })

    return rows


def generate_code_understanding(graph_data):
    """第二类：代码理解题（SWE-QA 风格）"""
    rows = []
    used = set()

    # 2a. Purpose：高 fan_in 函数的作用
    for f in graph_data["high_fan_in"][:10]:
        if f["name"] in used:
            continue
        used.add(f["name"])
        summary = get_summary(f["ann"])
        question = f"函数 {f['name']} 在 llama.cpp 中扮演什么角色？为什么有这么多地方调用它？"
        answer = f"{f['name']} 定义在 {f['file']}，被 {f['fan_in']} 个函数调用，是系统基础设施。"
        if summary:
            answer += f" {summary}"
        rows.append({
            "类别": "代码理解", "子类": "Purpose",
            "问题": question, "答案": answer,
            "Evidence": f["file"], "实体": f["name"],
        })

    # 2b. Architecture：文件级架构
    for fs in graph_data["file_stats"][:8]:
        fname = fs["file"].split("/")[-1]
        if fname in used:
            continue
        used.add(fname)
        funcs_str = ", ".join(fs["funcs"][:8])
        question = f"{fs['file']} 这个文件的职责是什么？包含哪些核心函数？"
        answer = f"{fs['file']} 包含 {fs['func_count']} 个函数，核心函数包括：{funcs_str}。总 fan_in={fs['total_fan_in']}，fan_out={fs['total_fan_out']}。"
        rows.append({
            "类别": "代码理解", "子类": "Architecture",
            "问题": question, "答案": answer,
            "Evidence": fs["file"], "实体": fname,
        })

    # 2c. Data/Control Flow：调用链（确保文件多样性）
    seen_pairs = set()
    seen_caller_files = set()
    for pair in graph_data["call_pairs"]:
        key = (pair["caller"], pair["callee"])
        if key in seen_pairs:
            continue
        # 确保 caller 文件多样性
        if pair["caller_file"] in seen_caller_files:
            continue
        seen_pairs.add(key)
        seen_caller_files.add(pair["caller_file"])
        if len([r for r in rows if r["子类"] == "Data/Control Flow"]) >= 8:
            break
        caller_sum = get_summary(pair["caller_ann"])
        callee_sum = get_summary(pair["callee_ann"])
        question = f"函数 {pair['caller']} 为什么要调用 {pair['callee']}？这两个函数之间是什么关系？"
        answer = f"{pair['caller']}（{pair['caller_file']}）调用了 {pair['callee']}（{pair['callee_file']}）。"
        if caller_sum:
            answer += f" 调用者：{caller_sum}"
        if callee_sum:
            answer += f" 被调用者：{callee_sum}"
        rows.append({
            "类别": "代码理解", "子类": "Data/Control Flow",
            "问题": question, "答案": answer,
            "Evidence": f"{pair['caller_file']}, {pair['callee_file']}",
            "实体": f"{pair['caller']} -> {pair['callee']}",
        })

    # 2d. Concept/Definition：叶子函数的语义
    for f in graph_data["leaf_funcs"][:6]:
        if f["name"] in used:
            continue
        used.add(f["name"])
        summary = get_summary(f["ann"])
        question = f"函数 {f['name']} 的具体实现逻辑是什么？它的输入输出是什么？"
        answer = f"{f['name']} 定义在 {f['file']}，是叶子函数（不调用其他函数），被 {f['fan_in']} 个函数调用。"
        if summary:
            answer += f" {summary}"
        rows.append({
            "类别": "代码理解", "子类": "Concept/Definition",
            "问题": question, "答案": answer,
            "Evidence": f["file"], "实体": f["name"],
        })

    # 2e. 跨文件依赖：模块间耦合
    seen_cross = set()
    for pair in graph_data.get("cross_file_calls", []):
        key = (pair["caller_file"], pair["callee_file"])
        if key in seen_cross:
            continue
        seen_cross.add(key)
        if len([r for r in rows if r["子类"] == "Dependency"]) >= 4:
            break
        caller_sum = get_summary(pair["caller_ann"])
        callee_sum = get_summary(pair["callee_ann"])
        question = f"{pair['caller_file']} 中的 {pair['caller']} 为什么依赖 {pair['callee_file']} 中的 {pair['callee']}？这两个文件之间是什么关系？"
        answer = f"{pair['caller']} 跨文件调用了 {pair['callee']}，说明这两个模块存在耦合。"
        if caller_sum:
            answer += f" 调用者功能：{caller_sum}"
        if callee_sum:
            answer += f" 被调用者功能：{callee_sum}"
        rows.append({
            "类别": "代码理解", "子类": "Dependency",
            "问题": question, "答案": answer,
            "Evidence": f"{pair['caller_file']}, {pair['callee_file']}",
            "实体": f"{pair['caller']} -> {pair['callee']}",
        })

    # 2f. 中等复杂度函数的设计意图
    for f in graph_data.get("mid_funcs", []):
        if f["name"] in used:
            continue
        used.add(f["name"])
        if len([r for r in rows if r["子类"] == "Design Rationale"]) >= 4:
            break
        summary = get_summary(f["ann"])
        question = f"函数 {f['name']} 既被 {f['fan_in']} 个函数调用，又调用了 {f['fan_out']} 个函数。它在系统中处于什么层级？设计意图是什么？"
        answer = f"{f['name']}（{f['file']}）是一个中间层函数，fan_in={f['fan_in']}，fan_out={f['fan_out']}，起到承上启下的作用。"
        if summary:
            answer += f" {summary}"
        rows.append({
            "类别": "代码理解", "子类": "Design Rationale",
            "问题": question, "答案": answer,
            "Evidence": f["file"], "实体": f["name"],
        })

    return rows


def generate_workflow(graph_data):
    """第三类：工作流题（开发者决策支持）"""
    rows = []
    used = set()

    # 3a. 影响分析：我要改某个函数，风险多大？
    # 用 high_fan_in 的后半段，避免和代码理解 Purpose 题重叠
    for f in graph_data["high_fan_in"][10:20]:
        if f["name"] in used:
            continue
        used.add(f["name"])
        summary = get_summary(f["ann"])
        question = f"我想修改函数 {f['name']}，这样做的风险有多大？会影响哪些上游调用者？"
        answer = f"{f['name']}（{f['file']}）被 {f['fan_in']} 个函数调用，修改风险较高。"
        if summary:
            answer += f" 功能：{summary}"
        answer += " 建议修改前做充分的回归测试。"
        rows.append({
            "类别": "工作流", "子类": "影响分析",
            "问题": question, "答案": answer,
            "Evidence": f["file"], "实体": f["name"],
        })

    # 3b. 改动热点：哪些函数最近被频繁修改？
    for f in graph_data["hot_funcs"][:6]:
        if f["name"] in used:
            continue
        used.add(f["name"])
        pr_titles = "; ".join(f["pr_titles"][:3])
        question = f"函数 {f['name']} 最近被频繁修改，是不是设计有问题？历史上改它容易出什么问题？"
        answer = f"{f['name']}（{f['file']}）被 {f['pr_count']} 个 PR 修改过。最近的 PR 包括：{pr_titles}。"
        summary = get_summary(f["ann"])
        if summary:
            answer += f" 功能：{summary}"
        rows.append({
            "类别": "工作流", "子类": "改动热点",
            "问题": question, "答案": answer,
            "Evidence": f["file"], "实体": f["name"],
        })

    # 3c. 模块稳定性：某个模块最近 bug 多不多？
    # 按文件路径前缀聚合 Issue
    file_prefixes = {}
    for fix in graph_data["fixes"]:
        title = fix["pr_title"] or ""
        # 从 PR title 提取模块名（如 "vulkan:", "server:", "ggml-cuda:"）
        if ":" in title:
            module = title.split(":")[0].strip().lower()
            file_prefixes.setdefault(module, []).append(fix)

    for module, fixes in file_prefixes.items():
        if len(fixes) < 2:
            continue
        issue_nums = [f"#{f['issue_num']}" for f in fixes]
        pr_nums = [f"#{f['pr_num']}" for f in fixes]
        question = f"llama.cpp 的 {module} 模块最近稳定吗？有没有频繁出 bug 的趋势？"
        answer = f"{module} 模块最近有 {len(fixes)} 个已修复的 bug：Issue {', '.join(issue_nums)}，对应修复 PR {', '.join(pr_nums)}。"
        rows.append({
            "类别": "工作流", "子类": "模块稳定性",
            "问题": question, "答案": answer,
            "Evidence": ", ".join(issue_nums + pr_nums),
            "实体": module,
        })

    # 3d. 复杂度评估：某个函数太复杂了，怎么拆？（用后半段避免重叠）
    for f in graph_data["high_fan_out"][6:14]:
        if f["name"] in used:
            continue
        used.add(f["name"])
        if len([r for r in rows if r["子类"] == "复杂度评估"]) >= 6:
            break
        summary = get_summary(f["ann"])
        question = f"函数 {f['name']} 调用了 {f['fan_out']} 个子函数，复杂度很高。有什么重构建议？"
        answer = f"{f['name']}（{f['file']}）fan_out={f['fan_out']}，是系统中最复杂的函数之一。"
        if summary:
            answer += f" 功能：{summary}"
        answer += " 建议考虑按职责拆分为多个子函数，降低认知复杂度。"
        rows.append({
            "类别": "工作流", "子类": "复杂度评估",
            "问题": question, "答案": answer,
            "Evidence": f["file"], "实体": f["name"],
        })

    # 3e. 新人入门：我想贡献代码，从哪里开始？
    # 找 fan_in 低、fan_out 低的简单函数
    simple_files = [fs for fs in graph_data["file_stats"]
                    if fs["func_count"] <= 10 and fs["total_fan_out"] <= 20]
    for fs in simple_files[:3]:
        fname = fs["file"].split("/")[-1]
        if fname in used:
            continue
        used.add(fname)
        funcs_str = ", ".join(fs["funcs"][:5])
        question = f"我是 llama.cpp 的新贡献者，想从简单的模块开始。{fs['file']} 适合新手入手吗？"
        answer = f"{fs['file']} 包含 {fs['func_count']} 个函数（{funcs_str}），总 fan_out={fs['total_fan_out']}，复杂度较低，适合新手了解项目结构。"
        rows.append({
            "类别": "工作流", "子类": "新人入门",
            "问题": question, "答案": answer,
            "Evidence": fs["file"], "实体": fname,
        })

    # 3f. Code Review：审查某个文件的内聚性
    seen_files = set()
    for pair in graph_data.get("same_file_calls", []):
        f = pair["file"]
        if f in seen_files:
            continue
        seen_files.add(f)
        if len([r for r in rows if r["子类"] == "Code Review"]) >= 6:
            break
        fname = f.split("/")[-1]
        question = f"我在 review {f} 的代码，{pair['func1']} 和 {pair['func2']} 之间的调用关系合理吗？这个文件的内聚性如何？"
        ann1_sum = get_summary(pair["ann1"])
        ann2_sum = get_summary(pair["ann2"])
        answer = f"在 {f} 中，{pair['func1']}（fan_out={pair['f1_out']}）调用了 {pair['func2']}（fan_in={pair['f2_in']}）。"
        if ann1_sum:
            answer += f" {pair['func1']}：{ann1_sum}"
        if ann2_sum:
            answer += f" {pair['func2']}：{ann2_sum}"
        rows.append({
            "类别": "工作流", "子类": "Code Review",
            "问题": question, "答案": answer,
            "Evidence": f, "实体": fname,
        })

    # 3g. 依赖风险：跨模块依赖是否合理
    seen_deps = set()
    for pair in graph_data.get("cross_file_calls", []):
        key = (pair["caller_file"], pair["callee_file"])
        if key in seen_deps:
            continue
        seen_deps.add(key)
        if len([r for r in rows if r["子类"] == "依赖风险"]) >= 4:
            break
        question = f"{pair['caller_file']} 依赖了 {pair['callee_file']}，这个跨模块依赖合理吗？如果后者接口变了，影响范围多大？"
        answer = f"{pair['caller']}（{pair['caller_file']}）调用了 {pair['callee']}（{pair['callee_file']}），存在跨模块耦合。"
        callee_sum = get_summary(pair["callee_ann"])
        if callee_sum:
            answer += f" 被依赖函数功能：{callee_sum}"
        answer += " 如果被依赖接口变更，需要同步更新调用方。"
        rows.append({
            "类别": "工作流", "子类": "依赖风险",
            "问题": question, "答案": answer,
            "Evidence": f"{pair['caller_file']}, {pair['callee_file']}",
            "实体": f"{pair['caller']} -> {pair['callee']}",
        })

    return rows


def main():
    driver = get_driver()
    driver.verify_connectivity()

    issues_by_num, prs_by_num = load_github_data()
    graph_data = query_graph(driver, NEO4J_DATABASE)
    driver.close()

    print("生成 Issue 驱动题...")
    issue_rows = generate_issue_driven(graph_data, issues_by_num, prs_by_num)
    print(f"  生成 {len(issue_rows)} 题")

    print("生成代码理解题...")
    code_rows = generate_code_understanding(graph_data)
    print(f"  生成 {len(code_rows)} 题")

    print("生成工作流题...")
    workflow_rows = generate_workflow(graph_data)
    print(f"  生成 {len(workflow_rows)} 题")

    all_rows = issue_rows + code_rows + workflow_rows
    random.shuffle(all_rows)

    # 截取到 100 题
    if len(all_rows) > 100:
        # 按类别均衡截取
        by_cat = {}
        for r in all_rows:
            by_cat.setdefault(r["类别"], []).append(r)
        targets = {"Issue驱动": 40, "代码理解": 30, "工作流": 30}
        final = []
        for cat, target in targets.items():
            pool = by_cat.get(cat, [])
            final.extend(pool[:target])
        all_rows = final[:100]

    df = pd.DataFrame(all_rows)
    df.to_csv(OUTPUT, index=False, encoding="utf-8")

    print(f"\n总题数: {len(df)}")
    print(f"\n类别分布:")
    print(df["类别"].value_counts().to_string())
    print(f"\n子类分布:")
    print(df["子类"].value_counts().to_string())
    print(f"\n已保存: {OUTPUT}")


if __name__ == "__main__":
    main()
