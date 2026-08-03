"""Mock 数据源：离线开发/测试用，模拟线上日志与指标。

设计场景（PRD §5.3 的典型故障）：
  gateway → checkout → payment 三层调用，checkout 调用 payment 超时，
  导致错误传播到上层。日志按时间顺序记录，含 rpc_direction/rpc_target。

构造目标：给定 traceId "tr-mock-0001"，重建算法应能还原
  gateway → checkout → payment 的调用链，并定位 checkout→payment 这跳慢/错。

注意：时间戳是刻意构造的"故障窗口"（如 2026-08-02 21:00:00 起的若干秒），
调用方向字段缺失会触发"弱重建"路径（见 trace_reconstruction 的测试）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.schema.models import LogRecord, MetricPoint, MetricSeries, TimeRange
from app.tools.base import LogQuery, MetricQuery

# 统一基准时间：故障起点（PRD 示例用 UTC）
_BASE = "2026-08-02T21:00:00Z"


def _log(offset_sec: int, service: str, host: str, message: str, *, level="info", trace_id="tr-mock-0001", rpc_direction=None, rpc_target=None, exception=None) -> LogRecord:
    return LogRecord(
        timestamp=_ts(offset_sec),
        service=service,
        host=host,
        message=message,
        level=level,
        trace_id=trace_id,
        rpc_direction=rpc_direction,
        rpc_target=rpc_target,
        exception=exception,
    )


def _ts(offset_sec: int):
    return datetime.fromisoformat(_BASE.replace("Z", "+00:00")) + timedelta(seconds=offset_sec)


class MockLogDatasource(LogQuery):
    """内存日志数据源。query_logs 支持按时间窗/服务/错误/索引过滤。"""

    def __init__(self):
        self.logs = build_mock_logs()
        self._trace_logs = {tr: [l for l in self.logs if l.trace_id == tr] for tr in {l.trace_id for l in self.logs}}

    def query_logs(
        self,
        time_range: TimeRange,
        *,
        filter_expression: str = "",
        index: str = "",
        trace_id: str | None = None,
        limit: int = 1000,
    ) -> list[LogRecord]:
        # 按 traceId 优先
        if trace_id:
            result = self._trace_logs.get(trace_id, [])
        else:
            result = list(self.logs)
        # 时间窗过滤
        result = [l for l in result if time_range.start <= l.timestamp <= time_range.end]
        # filter_expression 简化：支持 "level:error" 或 "service:xxx"
        if filter_expression:
            result = self._apply_filter(result, filter_expression)
        return result[:limit]

    @staticmethod
    def _apply_filter(logs: list[LogRecord], expr: str) -> list[LogRecord]:
        # 极简过滤器：支持 "level:error"、"service:checkout"
        if expr.startswith("level:"):
            lvl = expr.split(":", 1)[1].strip()
            return [l for l in logs if l.level == lvl]
        if expr.startswith("service:"):
            svc = expr.split(":", 1)[1].strip()
            return [l for l in logs if l.service == svc]
        return logs


class MockMetricDatasource(MetricQuery):
    """内存指标数据源。返回构造的异常/正常时序。"""

    def __init__(self):
        self.series = build_mock_metrics()

    def query_metric(
        self,
        metric: str,
        time_range: TimeRange,
        *,
        labels: dict[str, str] | None = None,
        step_seconds: int = 60,
    ) -> MetricSeries:
        key = metric
        if key in self.series:
            pts = [p for p in self.series[key].points if time_range.start <= p.ts <= time_range.end]
            return MetricSeries(metric=metric, labels=self.series[key].labels, points=pts)
        # 未知指标返回空
        return MetricSeries(metric=metric, labels=labels or {}, points=[])


# ---------------------------------------------------------------- 构造数据

def build_mock_logs() -> list[LogRecord]:
    """构造一段带 traceId 的三层调用日志。"""
    logs = []
    # ---- 故障 trace tr-mock-0001：gateway → checkout → payment，payment 超时 ----
    logs.append(_log(0, "gateway", "host-gw-1", "receive /order", rpc_direction="in", rpc_target="gateway"))
    logs.append(_log(1, "gateway", "host-gw-1", "call checkout /order", rpc_direction="out", rpc_target="checkout"))
    logs.append(_log(2, "checkout", "host-co-1", "receive /order", rpc_direction="in", rpc_target="checkout"))
    logs.append(_log(3, "checkout", "host-co-1", "call payment /charge", rpc_direction="out", rpc_target="payment"))
    # payment 处理超时（10 秒后）返回错误
    logs.append(_log(13, "payment", "host-pay-1", "receive /charge", rpc_direction="in", rpc_target="payment"))
    logs.append(_log(15, "payment", "host-pay-1", "ERROR timeout processing /charge", level="error", exception="TimeoutException"))
    # checkout 捕获超时，向上抛错
    logs.append(_log(16, "checkout", "host-co-1", "ERROR payment timeout", level="error", exception="TimeoutException"))
    logs.append(_log(17, "gateway", "host-gw-1", "ERROR checkout failed", level="error", exception="RemoteException"))

    # ---- 噪音日志（验证日志聚类降噪：heartbeat/健康检查/info 不进异常簇）----
    logs.append(_log(5, "checkout", "host-co-1", "heartbeat ok", level="info"))
    logs.append(_log(6, "payment", "host-pay-1", "health check passed", level="info"))
    logs.append(_log(7, "gateway", "host-gw-1", "connection pool acquired 10 conns", level="info"))

    # ---- 正常 trace tr-mock-0002（基线对比用，payment 正常返回）----
    logs.append(_log(10000, "gateway", "host-gw-1", "receive /order ok", rpc_direction="in", rpc_target="gateway", trace_id="tr-mock-0002"))
    logs.append(_log(10001, "gateway", "host-gw-1", "call checkout ok", rpc_direction="out", rpc_target="checkout", trace_id="tr-mock-0002"))
    logs.append(_log(10002, "checkout", "host-co-1", "receive ok", rpc_direction="in", rpc_target="checkout", trace_id="tr-mock-0002"))
    logs.append(_log(10003, "checkout", "host-co-1", "call payment ok", rpc_direction="out", rpc_target="payment", trace_id="tr-mock-0002"))
    logs.append(_log(10004, "payment", "host-pay-1", "receive ok", rpc_direction="in", rpc_target="payment", trace_id="tr-mock-0002"))
    logs.append(_log(10006, "payment", "host-pay-1", "ok", trace_id="tr-mock-0002"))
    logs.append(_log(10007, "checkout", "host-co-1", "ok", trace_id="tr-mock-0002"))
    logs.append(_log(10008, "gateway", "host-gw-1", "ok", trace_id="tr-mock-0002"))

    return logs


def build_mock_metrics() -> dict[str, MetricSeries]:
    """构造 checkout.error_rate 在故障窗口的异常时序（MAD/3σ 检测用）。"""
    t0 = datetime.fromisoformat(_BASE.replace("Z", "+00:00"))
    points = []
    for i in range(60):  # 60 分钟，前 30 分钟正常 ~1%，后 30 分钟飙到 40%
        ts = t0 - timedelta(minutes=60 - i)
        val = 0.01 if i < 30 else 0.40
        points.append(MetricPoint(ts=ts, value=val))
    return {
        "checkout_error_rate": MetricSeries(
            metric="checkout_error_rate",
            labels={"service": "checkout"},
            points=points,
        )
    }
