"""QA 实验运行器 — 批量跑 benchmark、记录结果、支持 checkpoint"""
from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .pipeline import QAPipeline
from .models import QAResult

logger = logging.getLogger(__name__)


class QARunner:
    """
    QA 实验运行器。

    支持：
    - 单题运行（调试）
    - 批量 benchmark（含 checkpoint）
    - 多配置对比实验
    """

    def __init__(self, pipeline: QAPipeline, output_dir: Path | str = "./results"):
        self.pipeline = pipeline
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def run_single(self, question: str) -> QAResult:
        """运行单个问题（适合调试）"""
        logger.info("Running single question: %s", question[:60])
        return self.pipeline.run(question)

    def run_benchmark(
        self,
        questions: list[dict[str, Any]],
        output_path: Path | str | None = None,
        workers: int = 1,
        checkpoint_every: int = 20,
        limit: int = 0,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """
        批量运行 benchmark。

        Args:
            questions: 问题列表，每项至少包含 {"question": str}
            output_path: 输出 JSON 路径（默认 auto）
            workers: 并行 worker 数（建议 1-20）
            checkpoint_every: 每 N 题保存一次 checkpoint
            limit: 只跑前 N 题（0=全部）
            offset: 从第 N 题开始

        Returns:
            结果列表（dict）
        """
        rows = questions[offset:]
        if limit > 0:
            rows = rows[:limit]

        total = len(rows)
        if total == 0:
            logger.warning("No questions to run")
            return []

        if output_path is None:
            ts = time.strftime("%Y%m%d_%H%M%S")
            output_path = self.output_dir / f"qa_results_{ts}.json"
        else:
            output_path = Path(output_path)

        logger.info("Benchmark: %d questions, workers=%d, output=%s", total, workers, output_path)

        results: list[dict] = []
        completed = 0

        def _process_one(idx: int, row: dict) -> dict:
            question = row.get("question", "")
            if not question:
                return {
                    "index": idx,
                    "question": "",
                    "error": "Empty question",
                    "answer": "",
                }

            t0 = time.perf_counter()
            try:
                result = self.pipeline.run(question)
                latency = (time.perf_counter() - t0) * 1000
                return {
                    "index": idx,
                    "id": row.get("id", f"qa_{idx}"),
                    "question": question,
                    "answer": result.answer,
                    "retrieved_functions": [f.to_dict() for f in result.retrieved_functions],
                    "steps": [s.to_dict() for s in result.steps],
                    "total_latency_ms": round(result.total_latency_ms, 2),
                    "total_tokens": result.total_tokens,
                    "error": result.error,
                    "latency_ms": round(latency, 2),
                }
            except Exception as e:
                logger.exception("Question %d failed: %s", idx, e)
                return {
                    "index": idx,
                    "id": row.get("id", f"qa_{idx}"),
                    "question": question,
                    "answer": "",
                    "error": str(e),
                    "latency_ms": round((time.perf_counter() - t0) * 1000, 2),
                }

        if workers <= 1:
            # 串行执行
            for i, row in enumerate(rows):
                result = _process_one(i + offset, row)
                results.append(result)
                completed += 1
                if completed % checkpoint_every == 0 or completed == total:
                    self._save(results, output_path)
                    logger.info("Checkpoint: %d/%d", completed, total)
        else:
            # 并行执行
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(_process_one, i + offset, row): i
                    for i, row in enumerate(rows)
                }
                for future in as_completed(futures):
                    try:
                        result = future.result()
                        results.append(result)
                        completed += 1
                        if completed % checkpoint_every == 0 or completed == total:
                            self._save(results, output_path)
                            logger.info("Checkpoint: %d/%d", completed, total)
                    except Exception as e:
                        logger.error("Future error: %s", e)
                        completed += 1

        # 最终保存（按 index 排序）
        results.sort(key=lambda x: x.get("index", 0))
        self._save(results, output_path)
        logger.info("Done: %d/%d results saved to %s", len(results), total, output_path)
        return results

    def _save(self, results: list[dict], path: Path) -> None:
        """保存结果到 JSON"""
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)

    def compare_configs(
        self,
        questions: list[dict],
        configs: list[dict[str, Any]],
        output_dir: Path | str | None = None,
    ) -> dict[str, list[dict]]:
        """
        多配置对比实验。

        Args:
            configs: 每个配置是一个 dict，用于更新 pipeline 参数
                     如 [{"name": "baseline", "enable_react": False}, ...]

        Returns:
            {config_name: results}
        """
        if output_dir is None:
            output_dir = self.output_dir / "compare"
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        all_results = {}
        for cfg in configs:
            name = cfg.pop("name", "unnamed")
            # 创建新 pipeline（基于当前配置覆盖）
            # 注：这里假设 pipeline 支持参数更新
            logger.info("Running config: %s", name)
            results = self.run_benchmark(
                questions,
                output_path=output_dir / f"{name}.json",
                **cfg,
            )
            all_results[name] = results

        return all_results
