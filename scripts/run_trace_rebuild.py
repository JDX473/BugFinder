"""Phase 0 CLI 原型：给定 traceId，重建粗糙调用链（PRD §5.3 / 第 10 章验收口径）。

用法（默认走 mock 数据源，无需任何配置）：
    python scripts/run_trace_rebuild.py tr-mock-0001
    python scripts/run_trace_rebuild.py --trace-id tr-mock-0001
    python scripts/run_trace_rebuild.py --list            # 列出 mock 里可用的 traceId

接真实环境后（配置 RCA_DATA_SOURCE=real + ES 连接），本脚本逻辑不变，
只替换数据源实现。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 保证 `python scripts/run_trace_rebuild.py` 直接运行时能找到 app 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _build_source():
    """骨架阶段固定用 mock 数据源；将来按 settings 切换 real。"""
    from app.tools.mock_datasource import MockLogDatasource

    return MockLogDatasource()


def _list_trace_ids() -> list[str]:
    src = _build_source()
    return sorted({l.trace_id for l in src.logs if l.trace_id})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按 traceId 重建调用链（Phase 0 CLI 原型）")
    parser.add_argument("trace_id", nargs="?", help="要重建的 traceId（mock 里可用 tr-mock-0001/0002）")
    parser.add_argument("--trace-id", dest="trace_id_opt", help="同位置参数，供习惯 --key 的调用")
    parser.add_argument("--list", action="store_true", help="列出 mock 数据源可用的 traceId")
    parser.add_argument("--cluster", action="store_true", help="对 trace 日志做聚类降噪，打印异常簇摘要")
    parser.add_argument(
        "--scenario",
        metavar="TEXT",
        help="场景路由演示：对给定事件文本做场景判定（mock 指标按 --service 过滤）",
    )
    parser.add_argument(
        "--service",
        metavar="SVC",
        help="场景路由演示用：限定该服务的指标序列（如 checkout / car-door）",
    )
    args = parser.parse_args(argv)

    if args.list:
        ids = _list_trace_ids()
        if not ids:
            print("（无可用 traceId）")
        for tid in ids:
            print(tid)
        return 0

    if args.scenario:
        from app.pipeline.anomaly_detection import detect_anomaly
        from app.pipeline.scenario_router import route_scenario
        from app.tools.mock_datasource import MockMetricDatasource

        # 事件时间窗：取事件所属服务的全部指标序列做异常检测（含正常结果，
        # 供路由判断"技术信号干净"）
        metric_src = MockMetricDatasource()
        series = list(metric_src.series.values())
        if args.service:
            series = [s for s in series if s.labels.get("service") == args.service]
        all_results = [detect_anomaly(s) for s in series]
        result = route_scenario(incident_text=args.scenario, anomalies=all_results, llm=None)
        print("场景路由：")
        print(result.to_summary())
        abnormal = [a for a in all_results if a.is_anomaly]
        if abnormal:
            print(f"异常指标 {len(abnormal)} 个：")
            for a in abnormal:
                print(f"  - {a.metric}: {a.shape.value} (ratio {a.ratio:.2f})")
        return 0

    trace_id = args.trace_id or args.trace_id_opt
    if not trace_id:
        parser.print_help()
        return 2

    from app.tools.trace_reconstruction import TraceReconstructionError, rebuild_trace, find_slow_or_error_hops

    src = _build_source()
    try:
        graph = rebuild_trace(src, trace_id)
    except TraceReconstructionError as e:
        print(f"[失败] {e}")
        return 1

    print(f"traceId: {graph.trace_id}")
    print(f"重建置信度: {graph.reconstruction_confidence.value}  ({graph.coverage_note})")
    print(f"涉及服务: {', '.join(graph.services)}")
    print("-" * 60)

    if not graph.hops:
        print("（无重建跳）")
    for i, h in enumerate(graph.hops, 1):
        flag = "  [错误]" if h.has_error else ""
        print(
            f"  {i}. {h.source_service} -> {h.target_service}"
            f"  {h.duration_ms:.0f}ms{flag}"
        )
        if h.error_summary:
            print(f"      ↳ {h.error_summary}")

    findings = find_slow_or_error_hops(graph)
    if findings:
        print("-" * 60)
        print("慢/错节点定位：")
        for f in findings:
            h = f["hop"]
            print(
                f"  - {h.source_service} -> {h.target_service}: "
                f"{'慢' if f['is_slow'] else ''}{'错' if f['has_error'] else ''} "
                f"({f['reason']})"
            )

    if args.cluster:
        from datetime import timedelta

        from app.pipeline.log_clustering import cluster_logs
        from app.schema.models import TimeRange

        # 对 trace 日志做聚类降噪（时间窗：trace 最早/最晚 ± 10 秒）
        trace_logs = [l for l in src.logs if l.trace_id == trace_id]
        if trace_logs:
            lo = min(l.timestamp for l in trace_logs) - timedelta(seconds=10)
            hi = max(l.timestamp for l in trace_logs) + timedelta(seconds=10)
            result = cluster_logs(src.query_logs(TimeRange(start=lo, end=hi)))
            print("-" * 60)
            print("日志聚类降噪：")
            print(result.to_summary())

    return 0


if __name__ == "__main__":
    sys.exit(main())
