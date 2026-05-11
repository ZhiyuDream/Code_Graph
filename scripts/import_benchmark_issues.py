#!/usr/bin/env python3
"""
将 llama_cpp_issue_benchmark.with_answers.json 中的 Issue 导入 Neo4j。

创建 Issue 节点，字段包含 benchmark 特有的 difficulty、question_type、evidence、metadata 等。
可选：从 question/answer 中提取函数名，建立 MENTIONS 边到 Function 节点。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_CODE_GRAPH = Path(__file__).resolve().parent.parent
if str(_CODE_GRAPH) not in sys.path:
    sys.path.insert(0, str(_CODE_GRAPH))

from config import NEO4J_DATABASE
from src.neo4j_writer import get_driver

BENCHMARK_PATH = _CODE_GRAPH / "datasets" / "llama_cpp_issue_benchmark.with_answers.json"

# 从文本中提取可能的函数名（backtick 包裹或 xxx() 形式）
_RE_BACKTICK = re.compile(r'`([a-zA-Z_][a-zA-Z0-9_]{2,})`')
_RE_FUNC_CALL = re.compile(r'\b([a-zA-Z_][a-zA-Z0-9_]{2,})\s*\(')


def _extract_func_names(text: str) -> set[str]:
    """从文本中提取可能的函数名。"""
    if not text:
        return set()
    backtick = set(_RE_BACKTICK.findall(text))
    func_call = set(_RE_FUNC_CALL.findall(text))
    return backtick | func_call


def _load_benchmark() -> list[dict]:
    with open(BENCHMARK_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("questions", [])


def import_benchmark_issues(driver, database: str, create_mentions: bool = True) -> dict:
    """导入 benchmark issue 到 Neo4j。"""
    issues = _load_benchmark()
    print(f"加载 benchmark: {len(issues)} 条 issue")

    # 获取图中所有函数名（用于 MENTIONS 边）
    all_func_names: set[str] = set()
    if create_mentions:
        with driver.session(database=database) as s:
            r = s.run("MATCH (f:Function) RETURN f.name AS name")
            all_func_names = {rec["name"] for rec in r}
        print(f"图中函数名: {len(all_func_names)} 个")

    # 创建约束
    with driver.session(database=database) as s:
        try:
            s.run("CREATE CONSTRAINT IF NOT EXISTS FOR (n:Issue) REQUIRE n.id IS UNIQUE")
        except Exception:
            pass

    mentions_count = 0

    with driver.session(database=database) as s:
        for issue in issues:
            issue_id = issue.get("id", f"issue_{issue.get('issue_number', 0)}")
            number = issue.get("issue_number", 0)
            question = issue.get("question", "")
            answer = issue.get("answer", "")
            difficulty = issue.get("difficulty", "")
            question_type = issue.get("question_type", "")
            evidence = issue.get("evidence", {})
            metadata = issue.get("metadata", {})

            s.run(
                """
                MERGE (n:Issue {id: $id})
                SET n.number = $number,
                    n.title = $title,
                    n.body = $body,
                    n.question = $question,
                    n.answer = $answer,
                    n.difficulty = $difficulty,
                    n.question_type = $question_type,
                    n.state = $state,
                    n.labels = $labels,
                    n.total_comments = $total_comments,
                    n.human_comments = $human_comments,
                    n.created_year = $created_year,
                    n.quality_score = $quality_score,
                    n.primary_intent = $primary_intent,
                    n.primary_topic = $primary_topic,
                    n.source = "benchmark"
                """,
                id=issue_id,
                number=number,
                title=(metadata.get("title") or question)[:4096],
                body=answer[:65535],
                question=question[:4096],
                answer=answer[:65535],
                difficulty=difficulty,
                question_type=question_type,
                state=metadata.get("state", ""),
                labels=metadata.get("labels", []),
                total_comments=metadata.get("total_comments", 0),
                human_comments=metadata.get("human_comments", 0),
                created_year=metadata.get("created_year", 0),
                quality_score=metadata.get("quality_score", 0.0),
                primary_intent=evidence.get("primary_intent", ""),
                primary_topic=evidence.get("primary_topic", ""),
            )

            # MENTIONS 边：question + answer 中提到的函数名
            if create_mentions and all_func_names:
                text = f"{question}\n{answer}"
                mentioned = _extract_func_names(text) & all_func_names
                if mentioned:
                    result = s.run(
                        """
                        MATCH (i:Issue {id: $issue_id}), (func:Function)
                        WHERE func.name IN $names
                        MERGE (i)-[:MENTIONS]->(func)
                        RETURN count(*) AS cnt
                        """,
                        issue_id=issue_id,
                        names=list(mentioned),
                    )
                    mentions_count += result.single()["cnt"]

    # 验证
    with driver.session(database=database) as s:
        r = s.run("MATCH (n:Issue) WHERE n.source = 'benchmark' RETURN count(n) AS cnt")
        issue_cnt = r.single()["cnt"]
        r = s.run("""
            MATCH (i:Issue)-[r:MENTIONS]->(:Function)
            WHERE i.source = 'benchmark'
            RETURN count(r) AS cnt
        """)
        mentions_total = r.single()["cnt"]

    print(f"\n导入完成:")
    print(f"  Issue 节点: {issue_cnt}")
    print(f"  MENTIONS 边: {mentions_total}")

    return {
        "issues": issue_cnt,
        "mentions": mentions_total,
    }


def main() -> int:
    driver = get_driver()
    try:
        driver.verify_connectivity()
    except Exception as e:
        print(f"Neo4j 连接失败: {e}")
        return 1

    if not BENCHMARK_PATH.exists():
        print(f"Benchmark 文件不存在: {BENCHMARK_PATH}")
        return 1

    stats = import_benchmark_issues(driver, NEO4J_DATABASE)
    driver.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
