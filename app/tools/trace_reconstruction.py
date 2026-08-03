"""traceId 聚合重建调用链（PRD §5.3 关键路径）。

流程（5 步）：
  1. 聚合：按 traceId + 时间窗，从日志系统拉回该 trace 的全部日志行
  2. 归一：按 service 分组，提取 timestamp / rpc_direction / rpc_target / 错误标记
  3. 重建：按时间序，用 out→in 匹配拼装调用链，计算每跳耗时与错误
  4. 定位慢/错节点：标记耗时异常或携带错误的跳
  5. 基线对比：与正常时段同类型 trace 对比（调用方传入基线耗时）

输出 TraceGraph。方向字段缺失时退化为"弱重建"（按服务时间序聚合成链）。

约束：
  - 重建质量依赖日志埋点（rpc_direction 缺失 → weak；traceId 空 → 无法重建）
  - 本模块纯确定性，不调用 LLM
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone

from app.schema.models import (
    LogRecord,
    ReconstructionConfidence,
    RpcDirection,
    TraceGraph,
    TraceHop,
)
from app.tools.base import LogQuery


class TraceReconstructionError(Exception):
    """无法重建（traceId 为空、聚合不到日志等）。"""


def rebuild_trace(
    log_source: LogQuery,
    trace_id: str,
    *,
    time_window_minutes: int = 30,
) -> TraceGraph:
    """按 traceId 重建调用链。

    流程：
    1. 确定时间窗：无显式窗口时，先做一次无界查询取最早/最晚日志时间，再回填窗口重新查询。
       （PRD §5.3：trace 开始时间 ± 宽裕窗）
    2. 聚合该 trace 全部日志。
    3. 归一 + 重建。
    """
    if not trace_id:
        raise TraceReconstructionError("trace_id 不能为空")

    logs = _fetch_all_trace_logs(log_source, trace_id)
    if not logs:
        raise TraceReconstructionError(f"trace {trace_id!r} 未聚合到任何日志（检查 traceId 与时间窗）")

    graph = _rebuild_from_logs(trace_id, logs)
    return graph


def _fetch_all_trace_logs(log_source: LogQuery, trace_id: str) -> list[LogRecord]:
    """先用无界时间窗拉取，避免外部接口无法只按 traceId 查询。"""
    # 用一个超宽窗口包裹，再按实际日志时间裁剪
    wide_start = datetime(1970, 1, 1, tzinfo=timezone.utc)
    wide_end = datetime(2100, 1, 1, tzinfo=timezone.utc)
    from app.schema.models import TimeRange

    result = log_source.query_logs(
        TimeRange(start=wide_start, end=wide_end),
        trace_id=trace_id,
        limit=100_000,
    )
    return sorted(result, key=lambda l: l.timestamp)


def _rebuild_from_logs(trace_id: str, logs: list[LogRecord]) -> TraceGraph:
    """从已聚合的日志重建调用链（纯确定性）。"""
    # 判断是否有 rpc_direction 字段可用
    has_direction = any(l.rpc_direction is not None for l in logs)

    if has_direction:
        hops = _rebuild_with_direction(logs)
        confidence = ReconstructionConfidence.STRONG
    else:
        hops = _rebuild_weak(logs)
        confidence = ReconstructionConfidence.WEAK

    services = sorted({l.service for l in logs})
    coverage_note = _coverage_note(logs, has_direction)
    return TraceGraph(
        trace_id=trace_id,
        hops=hops,
        services=services,
        reconstruction_confidence=confidence,
        coverage_note=coverage_note,
    )


def _rebuild_with_direction(logs: list[LogRecord]) -> list[TraceHop]:
    """按 out→in 匹配重建强调用链（两遍法）。

    第一遍（段错误表）：对每个服务，`in` 日志开启一个"请求段"，段内收集该
    服务的 error 日志（错误日志常是独立一行，在 in 之后、下一 in 之前）。
    第二遍（构建跳）：`out` 日志登记 pending；`in` 日志与最近的 pending 匹配
    成一跳。一跳的错误 = 下游服务该请求段内的错误（下游处理请求时出错）。

    语义正确性：checkout out → payment in，payment 段内报错 → 该跳 has_error。
    """
    # ---- 第一遍：按服务构建"请求段 -> 段错误"映射 ----
    # segment_key = (service, in_time) -> (error_exception or None)
    segment_errors: dict[tuple[str, object], str | None] = {}
    last_in: dict[str, object] = {}
    for l in logs:
        if l.rpc_direction == RpcDirection.IN:
            last_in[l.service] = l.timestamp
        elif l.level == "error":
            key = (l.service, last_in.get(l.service))
            if key in segment_errors:
                # 合并异常信息（保留第一个非空）
                segment_errors[key] = segment_errors[key] or l.exception
            elif last_in.get(l.service) is not None:
                segment_errors[key] = l.exception

    # ---- 第二遍：构建跳 ----
    hops: list[TraceHop] = []
    pending: dict[str, dict] = {}  # target_service -> {source, start}

    for l in logs:
        if l.rpc_direction == RpcDirection.OUT:
            target = l.rpc_target or l.service
            pending[target] = {"source": l.service, "start": l.timestamp}
        elif l.rpc_direction == RpcDirection.IN:
            matched_source = None
            matched = None
            for svc, p in pending.items():
                if p["start"] <= l.timestamp:
                    matched_source, matched = svc, p
                    break
            if matched is not None:
                # 该跳的错误 = 下游服务本请求段内的错误
                seg_err = segment_errors.get((l.service, l.timestamp))
                hops.append(
                    TraceHop(
                        source_service=matched["source"],
                        target_service=l.service,
                        start_time=matched["start"],
                        end_time=l.timestamp,
                        duration_ms=(l.timestamp - matched["start"]).total_seconds() * 1000,
                        has_error=seg_err is not None,
                        error_summary=seg_err,
                    )
                )
                del pending[matched_source]
            # 无 pending → 入口 in，不产生跳

    # 收尾：未匹配的 pending out（下游未回日志）记为超时跳
    for target, p in pending.items():
        hops.append(
            TraceHop(
                source_service=p["source"],
                target_service=target,
                start_time=p["start"],
                end_time=p["start"] + timedelta(seconds=1),
                duration_ms=1000.0,
                has_error=True,
                error_summary=p.get("error_summary") or "下游未返回（疑似超时）",
            )
        )

    return hops


def _rebuild_weak(logs: list[LogRecord]) -> list[TraceHop]:
    """无 rpc_direction 时退化的弱重建。

    按服务的时间序聚合成"链式猜测"：把相邻服务按首次出现时间串起来，
    每跳耗时 = 下游首日志与上游首日志的时间差。整体置信度 weak，报告须标注。
    """
    first_ts: dict[str, datetime] = {}
    for l in logs:
        if l.service not in first_ts:
            first_ts[l.service] = l.timestamp

    order = sorted(first_ts, key=lambda s: first_ts[s])
    hops: list[TraceHop] = []
    for i in range(len(order) - 1):
        src, dst = order[i], order[i + 1]
        hops.append(
            TraceHop(
                source_service=src,
                target_service=dst,
                start_time=first_ts[src],
                end_time=first_ts[dst],
                duration_ms=(first_ts[dst] - first_ts[src]).total_seconds() * 1000,
                has_error=any(l.service == dst and l.level == "error" for l in logs),
                error_summary="弱重建（无 rpc_direction 字段），调用关系为时间序猜测",
            )
        )
    return hops


def _coverage_note(logs: list[LogRecord], has_direction: bool) -> str:
    n = len(logs)
    services = sorted({l.service for l in logs})
    base = f"聚合到 {n} 条日志，涉及服务：{', '.join(services) or '无'}"
    if not has_direction:
        base += "；无 rpc_direction 字段，调用方向为弱重建（时间序猜测）"
    return base


# ---------------------------------------------------------------- 慢/错节点定位

def find_slow_or_error_hops(
    graph: TraceGraph,
    *,
    baseline_ms: dict[tuple[str, str], float] | None = None,
    slow_factor: float = 3.0,
) -> list[dict]:
    """定位慢/错节点（PRD §5.3 第 4 步）。

    - has_error 的跳直接标记
    - 无基线时：跳耗时超过全局中位数 slow_factor 倍视为异常
    - 有基线时：跳耗时超过该边基线 slow_factor 倍视为异常
    """
    if not graph.hops:
        return []

    findings: list[dict] = []
    durations = [h.duration_ms for h in graph.hops]
    median = statistics.median(durations) if durations else 0.0

    for h in graph.hops:
        is_slow = False
        reason = ""
        if baseline_ms:
            base = baseline_ms.get((h.source_service, h.target_service))
            if base and h.duration_ms > base * slow_factor:
                is_slow, reason = True, f"耗时 {h.duration_ms:.0f}ms > 基线 {base:.0f}ms × {slow_factor}"
        elif median and h.duration_ms > median * slow_factor:
            is_slow, reason = True, f"耗时 {h.duration_ms:.0f}ms > 全局中位 {median:.0f}ms × {slow_factor}"

        if is_slow or h.has_error:
            findings.append(
                {
                    "hop": h,
                    "is_slow": is_slow,
                    "has_error": h.has_error,
                    "reason": reason or ("携带错误日志" if h.has_error else ""),
                }
            )
    return findings


def build_baseline(
    log_source: LogQuery,
    *,
    edge: tuple[str, str],
    sample_trace_count: int = 10,
    time_window_minutes: int = 30,
) -> float | None:
    """从同类型正常 trace 构造一条边的基线耗时（毫秒）。暂为占位，Phase 2 完善。"""
    return None
