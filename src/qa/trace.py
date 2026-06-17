"""实验追踪记录器 — 标准化记录每步召回、时延、token消耗"""
from __future__ import annotations

import time
from typing import Optional

from .models import StepTrace, QAResult


class TraceRecorder:
    """
    全程记录QA pipeline每一步的：
    - 召回了哪些函数
    - 搜索关键字是什么
    - 耗时多少
    - 消耗多少token
    - 新增了多少信息（info_gain）
    """

    def __init__(self):
        self.steps: list[StepTrace] = []
        self._step_counter = 0
        self._phase_start: float = 0.0
        self._pipeline_start: float = 0.0

    def start_pipeline(self) -> None:
        """整个pipeline开始"""
        self._pipeline_start = time.perf_counter()
        self._phase_start = self._pipeline_start

    def start_phase(self) -> None:
        """新phase开始，重置phase计时器"""
        self._phase_start = time.perf_counter()

    def record(
        self,
        phase: str,
        action: str = "",
        query: str = "",
        retrieved: Optional[list[str]] = None,
        token_usage: Optional[dict[str, int]] = None,
        info_gain: int = 0,
    ) -> StepTrace:
        """记录一步"""
        self._step_counter += 1
        latency = (time.perf_counter() - self._phase_start) * 1000

        step = StepTrace(
            step=self._step_counter,
            phase=phase,
            action=action,
            query=query,
            retrieved=retrieved or [],
            latency_ms=latency,
            token_usage=dict(token_usage) if token_usage else {},
            info_gain=info_gain,
        )
        self.steps.append(step)
        self._phase_start = time.perf_counter()  # 重置phase计时
        return step

    def record_error(self, phase: str, error: str) -> StepTrace:
        """记录错误"""
        return self.record(phase=phase, action="error", query=error[:500])

    def finalize(self, result: QAResult) -> None:
        """把记录汇总到结果对象"""
        result.steps = self.steps

        # 总时延 = 各步之和（或从pipeline_start算）
        if self._pipeline_start:
            result.total_latency_ms = (time.perf_counter() - self._pipeline_start) * 1000
        else:
            result.total_latency_ms = sum(s.latency_ms for s in self.steps)

        # 累计token
        total: dict[str, int] = {"prompt": 0, "completion": 0, "total": 0, "reasoning": 0}
        for s in self.steps:
            for k in total:
                total[k] += s.token_usage.get(k, 0)
        result.total_tokens = total

    def get_summary(self) -> dict[str, Any]:
        """获取追踪摘要"""
        phases = {}
        for s in self.steps:
            phases.setdefault(s.phase, {"count": 0, "latency_ms": 0.0})
            phases[s.phase]["count"] += 1
            phases[s.phase]["latency_ms"] += s.latency_ms

        return {
            "total_steps": self._step_counter,
            "total_latency_ms": round(sum(s.latency_ms for s in self.steps), 2),
            "phases": phases,
            "total_info_gain": sum(s.info_gain for s in self.steps),
        }
