#!/usr/bin/env python3
"""
评估 QA 结果中答案引用的文件路径是否存在幻觉。

改进点（相比简单正则提取）：
1. 区分项目名 `llama.cpp`（目录）和文件 `src/llama.cpp`
2. 对 basename-only 路径（如 `arg.cpp`），搜索仓库所有子目录做 basename 匹配
3. 对省略子目录前缀的路径（如 `common.cpp`），尝试在 `common/` 下匹配

用法：
    python eval_path_hallucination.py -i results/xxx.json -r /data/users/zzy/RUC/llama.cpp
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from collections import Counter


# 扩展名集合
_SOURCE_EXTS = {"cpp", "c", "h", "hpp", "cc", "cxx", "py", "js", "go", "rs", "java", "md"}


def extract_paths(text: str) -> list[str]:
    """从文本中提取候选文件路径。"""
    # 模式1: 反引号包裹的路径 `path/to/file.cpp`
    paths = []
    for m in re.findall(r'`([^`]+?)`', text):
        if "." in m and ("/" in m or "\\" in m):
            paths.append(m.replace("\\", "/"))

    # 模式2: 无反引号的路径，如 "common/arg.cpp:284" 或 "src/llama.cpp"
    pattern = r'(?<![\w/])([a-zA-Z0-9_\-]+(?:/[a-zA-Z0-9_\-]+)*\.(?:' + "|".join(_SOURCE_EXTS) + r'))(?![\w.])'
    for m in re.finditer(pattern, text):
        paths.append(m.group(1))

    return paths


def validate_path(path: str, repo: Path, basename_cache: dict[str, list[Path]] | None = None) -> dict:
    """
    验证单个路径是否真实存在。
    返回 {"path": str, "exists": bool, "resolved": str|None, "reason": str}
    """
    # 特殊情况：llama.cpp 作为独立词通常是项目名/目录名
    if path == "llama.cpp":
        return {"path": path, "exists": False, "resolved": None, "reason": "llama.cpp 是项目目录名，不是文件"}

    # 尝试字面路径
    literal = repo / path
    if literal.exists() and literal.is_file():
        return {"path": path, "exists": True, "resolved": str(literal.relative_to(repo)), "reason": "字面路径匹配"}

    # basename 搜索
    basename = path.split("/")[-1]
    if basename_cache is not None:
        matches = basename_cache.get(basename, [])
    else:
        matches = list(repo.rglob(basename))

    if matches:
        return {
            "path": path, "exists": True,
            "resolved": str(matches[0].relative_to(repo)),
            "reason": f"basename 匹配: {len(matches)} 处"
        }

    # 尝试 common/ 前缀（llama.cpp 中很多文件在 common/ 下）
    for prefix in ["common/", "src/", "ggml/src/ggml-rpc/", "examples/"]:
        alt = repo / (prefix + path)
        if alt.exists() and alt.is_file():
            return {"path": path, "exists": True, "resolved": str(alt.relative_to(repo)), "reason": f"补全前缀 {prefix}"}

    return {"path": path, "exists": False, "resolved": None, "reason": "仓库中无此文件"}


def build_basename_cache(repo: Path) -> dict[str, list[Path]]:
    """预构建 basename -> [paths] 的缓存，加速批量验证。"""
    cache: dict[str, list[Path]] = {}
    for p in repo.rglob("*"):
        if p.is_file() and p.suffix.lstrip(".") in _SOURCE_EXTS:
            cache.setdefault(p.name, []).append(p)
    return cache


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-i", "--input", required=True, help="QA 结果 JSON")
    parser.add_argument("-r", "--repo", default="/data/users/zzy/RUC/llama.cpp", help="仓库根目录")
    parser.add_argument("-o", "--output", help="输出 JSON（可选）")
    args = parser.parse_args()

    repo = Path(args.repo)
    data = json.load(open(args.input, "r", encoding="utf-8"))

    print(f"加载 {len(data)} 题，仓库: {repo}")
    print("构建 basename 缓存...")
    cache = build_basename_cache(repo)
    print(f"  缓存了 {len(cache)} 个不同 basename\n")

    all_stats = []
    total_paths = 0
    total_exists = 0

    for item in data:
        answer = item.get("answer", "") or item.get("generated", "")
        paths = extract_paths(answer)
        results = [validate_path(p, repo, cache) for p in paths]

        exists_count = sum(1 for r in results if r["exists"])
        total_paths += len(paths)
        total_exists += exists_count

        stats = {
            "question": item.get("question", "")[:80],
            "paths": results,
            "path_count": len(paths),
            "exists_count": exists_count,
            "hallucination_rate": 1.0 - (exists_count / len(paths)) if paths else 0.0,
        }
        all_stats.append(stats)

    # 汇总
    overall_hallucination = 1.0 - (total_exists / total_paths) if total_paths else 0.0
    print(f"=== 路径幻觉统计 ===")
    print(f"总路径数: {total_paths}")
    print(f"真实存在: {total_exists}")
    print(f"幻觉比例: {overall_hallucination*100:.1f}%")
    print()

    # 最常见的不存在路径
    bad_paths = Counter()
    for s in all_stats:
        for r in s["paths"]:
            if not r["exists"]:
                bad_paths[r["path"]] += 1
    print("=== 最常见的不存在路径 ===")
    for p, n in bad_paths.most_common(15):
        print(f"  {n:3d}  {p}")
    print()

    # 每题幻觉率分布
    hrates = [s["hallucination_rate"] for s in all_stats if s["path_count"] > 0]
    if hrates:
        print(f"每题幻觉率: min={min(hrates)*100:.0f}%, max={max(hrates)*100:.0f}%, mean={sum(hrates)/len(hrates)*100:.1f}%")
        high_halluc = sum(1 for h in hrates if h >= 0.5)
        print(f"幻觉率 >= 50% 的题目: {high_halluc}/{len(hrates)}")

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(all_stats, f, ensure_ascii=False, indent=2)
        print(f"\n详细结果保存至: {args.output}")


if __name__ == "__main__":
    main()
