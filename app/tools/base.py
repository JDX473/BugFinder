"""数据源适配器抽象与查询护栏。

所有对日志/指标系统的访问都通过这里的协议，将来接真实环境（ES/Prometheus）
时实现同一协议即可，Agent 编排层不感知底层方言差异（PRD §5.1/5.2）。

查询护栏（PRD §5.1 执行校验层）：
  - 时间窗必带
  - 查询大小上限（返回条数）
  - 允许的索引/指标名白名单（防非法查询放大）
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.schema.models import LogRecord, MetricSeries, TimeRange


@runtime_checkable
class LogQuery(Protocol):
    """日志查询协议。"""

    def query_logs(
        self,
        time_range: TimeRange,
        *,
        filter_expression: str = "",
        index: str = "",
        trace_id: str | None = None,
        limit: int = 1000,
    ) -> list[LogRecord]:
        """按时间窗 + 可选过滤/索引/traceId 检索日志，返回 LogRecord 列表。"""
        ...


@runtime_checkable
class MetricQuery(Protocol):
    """指标查询协议。"""

    def query_metric(
        self,
        metric: str,
        time_range: TimeRange,
        *,
        labels: dict[str, str] | None = None,
        step_seconds: int = 60,
    ) -> MetricSeries:
        """查询一个指标的时序数据。"""
        ...


class QueryGuard:
    """查询护栏：时间窗必带、白名单校验、上限校验。"""

    def __init__(
        self,
        *,
        allowed_indices: set[str] | None = None,
        allowed_metrics: set[str] | None = None,
        max_hits: int = 1000,
    ):
        self.allowed_indices = allowed_indices or set()
        self.allowed_metrics = allowed_metrics or set()
        self.max_hits = max_hits

    def check_index(self, index: str) -> str:
        if index and self.allowed_indices and index not in self.allowed_indices:
            raise ValueError(f"索引 {index!r} 不在白名单内")
        return index

    def check_metric(self, metric: str) -> str:
        if self.allowed_metrics and metric not in self.allowed_metrics:
            raise ValueError(f"指标 {metric!r} 不在白名单内")
        return metric

    def check_limit(self, limit: int) -> int:
        if limit > self.max_hits:
            raise ValueError(f"查询上限 {limit} 超过允许的最大值 {self.max_hits}")
        return limit

    def check_time_range(self, time_range: TimeRange) -> TimeRange:
        # TimeRange 自身保证 start <= end
        return time_range
