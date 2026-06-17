#!/usr/bin/env python3
"""
修复 posthoc_audit_benchmark_v2.json 中 gold_evidence 结构化解析失败的问题。

问题原因：
    gold_evidence_raw 中部分条目的 symbol 为空（``），导致原始 regex 无法匹配，
    使得 file / evidence_type 等字段丢失。

用法:
    python scripts/tools/fix_audit_evidence.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# 匹配格式:
#   E1 `file:line` type `symbol`：snippet
#   E1 `file:line` type ``：snippet   (symbol 为空)
EVIDENCE_RE = re.compile(
    r"E(\d+)\s+`([^`]+)`\s+(\w+)\s+`([^`]*)`：(.+)"
)


def _parse_file_line(file_line: str) -> tuple[str, int | None, int | None]:
    """Parse 'path:line' or 'path:start-end' into (path, start, end)."""
    if ":" not in file_line:
        return file_line, None, None

    file_path, line_part = file_line.rsplit(":", 1)
    line_start = line_end = None

    if "-" in line_part:
        ls, le = line_part.split("-", 1)
        if ls.isdigit():
            line_start = int(ls)
        if le.isdigit():
            line_end = int(le)
    else:
        if line_part.isdigit():
            line_start = line_end = int(line_part)

    return file_path, line_start, line_end


def parse_evidence(raw_text: str) -> list[dict]:
    """Parse gold_evidence_raw into structured evidence list."""
    results: list[dict] = []
    seen_ids: set[str] = set()

    # 条目之间以 `；`（全角分号）或普通分号/换行分隔
    entries = re.split(r"[；;]\s*(?=E\d+\s+`)", raw_text)

    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue

        m = EVIDENCE_RE.match(entry)
        if not m:
            print(f"  WARNING: could not parse entry: {entry[:80]}")
            continue

        ev_id = f"E{m.group(1)}"
        file_line = m.group(2)
        ev_type = m.group(3)
        symbol = m.group(4) if m.group(4) else None
        snippet = m.group(5)

        file_path, line_start, line_end = _parse_file_line(file_line)

        # 处理重复 evidence_id（如 031 有两个 E8）
        if ev_id in seen_ids:
            base_id = ev_id
            suffix = 1
            ev_id = f"{base_id}_{suffix}"
            while ev_id in seen_ids:
                suffix += 1
                ev_id = f"{base_id}_{suffix}"
        seen_ids.add(ev_id)

        line_range = str(line_start) if line_start else ""
        if line_end and line_end != line_start:
            line_range = f"{line_start}-{line_end}"

        results.append(
            {
                "evidence_id": ev_id,
                "file": file_path,
                "line_start": line_start,
                "line_end": line_end,
                "line_range": line_range,
                "evidence_type": ev_type,
                "symbol": symbol,
                "supports": None,
                "snippet_summary": snippet,
                "raw": entry,
            }
        )

    return results


def main() -> None:
    json_path = Path("datasets/posthoc_audit_benchmark_v2.json")

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    fixed_count = 0
    for item in data["items"]:
        raw = item.get("gold_evidence_raw", "")

        has_problem = any(
            not ev.get("file") or not ev.get("evidence_type")
            for ev in item.get("gold_evidence", [])
        )

        ids = [ev.get("evidence_id") for ev in item.get("gold_evidence", [])]
        has_duplicate = len(ids) != len(set(ids))

        if not (has_problem or has_duplicate):
            continue

        qa_id = item["qa_id"]
        print(f"Fixing {qa_id} (problem={has_problem}, duplicate={has_duplicate})")

        new_evidence = parse_evidence(raw)
        item["gold_evidence"] = new_evidence

        # 同步更新 stats
        item["stats"]["num_gold_evidence"] = len(new_evidence)
        gold_files = list({ev["file"] for ev in new_evidence if ev.get("file")})
        item["stats"]["num_gold_files"] = len(gold_files)
        item["stats"]["gold_files"] = gold_files

        gold_functions = list({ev["symbol"] for ev in new_evidence if ev.get("symbol")})
        item["stats"]["num_gold_functions"] = len(gold_functions)
        item["stats"]["gold_functions"] = gold_functions

        ev_types = list({ev["evidence_type"] for ev in new_evidence if ev.get("evidence_type")})
        item["stats"]["evidence_types"] = ev_types
        item["stats"]["requires_cross_file_reasoning"] = len(gold_files) > 1

        fixed_count += 1

    # 备份原文件
    backup_path = json_path.with_suffix(".json.bak")
    with open(backup_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"Backup saved to {backup_path}")

    # 写回修复结果
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"Fixed {fixed_count} items, saved to {json_path}")


if __name__ == "__main__":
    main()
