"""Phase 0 CLI 原型：给定 traceId，重建粗糙调用链（PRD §5.3 / 第 10 章验收口径）。

用法（默认走 mock 数据源，无需任何配置）：
    python scripts/run_trace_rebuild.py tr-mock-0001
    python scripts/run_trace_rebuild.py --trace-id tr-mock-0001
    python scripts/run_trace_rebuild.py --list            # 列出 mock 里可用的 traceId
    python scripts/run_trace_rebuild.py --scenario "用户反馈支付失败" --service checkout
    python scripts/run_trace_rebuild.py --report "用户反馈支付失败" --service checkout   # 一键产出完整报告

接真实环境后（配置 RCA_DATA_SOURCE=real + ES 连接），本脚本逻辑不变，
只替换数据源实现。
"""

from __future__ import annotations

import argparse
import sys
import time as _time
from datetime import datetime, timedelta, timezone
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


def _fault_start() -> datetime:
    """mock 故障窗口起点（与 build_mock_metrics 的 t0 对齐）。"""
    return datetime.fromisoformat("2026-08-02T21:00:00+00:00")


def _event_time_window() -> tuple[datetime, datetime]:
    """事件时间窗：故障起点 ± 30 分钟（与 mock 指标序列时间窗对齐）。"""
    t0 = _fault_start()
    return t0 - timedelta(minutes=30), t0 + timedelta(minutes=30)


def _build_collected_evidence(log_src, metric_src, scenario, graph, trace_id, all_results) -> list[Evidence]:
    """按 PRD 6 步流程组装证据列表（日志/指标/trace/场景），供假设打分与报告。

    注意：`all_results` 必须与场景路由用同一份（同一服务过滤后的）指标检测结果——
    否则"技术信号干净"的判定（business_logic 前提）与指标证据不一致。
    """
    from app.pipeline.log_clustering import cluster_logs
    from app.schema.models import Evidence, EvidenceType, TimeRange

    evidence: list[Evidence] = []
    lo, hi = _event_time_window()

    # 场景证据（步骤 2 产物）
    evidence.append(
        Evidence(
            evidence_id="ev-scenario",
            type=EvidenceType.SCENARIO,
            source="scenario_router",
            summary=scenario.to_summary(),
            time_range=TimeRange(start=lo, end=hi),
            payload=scenario.to_dict(),
            confidence=scenario.confidence,
        )
    )

    # trace 证据（步骤 3 产物）
    if graph is not None:
        error_hops = [h for h in graph.hops if h.has_error]
        evidence.append(
            Evidence(
                evidence_id="ev-trace",
                type=EvidenceType.TRACE,
                source="trace_reconstruction",
                summary=(
                    f"trace {graph.trace_id} 重建 {len(graph.hops)} 跳"
                    f"（{graph.reconstruction_confidence.value}），{len(error_hops)} 跳携带错误"
                ),
                time_range=TimeRange(start=lo, end=hi),
                payload={
                    "trace_id": graph.trace_id,
                    "hop_count": len(graph.hops),
                    "error_hops": len(error_hops),
                },
            )
        )

    # 日志证据（步骤 4 产物：聚类降噪后的异常簇摘要）
    trace_logs = [l for l in log_src.logs if l.trace_id == trace_id]
    if trace_logs:
        cluster = cluster_logs(trace_logs)
        abnormal = [c for c in cluster.clusters if c.level in ("error", "fatal", "critical") or c.error_ratio > 0]
        if abnormal:
            top = abnormal[0]
            evidence.append(
                Evidence(
                    evidence_id="ev-log",
                    type=EvidenceType.LOG,
                    source="log_clustering",
                    summary=f"日志异常簇：{top.template}（{top.count} 条）",
                    time_range=TimeRange(start=lo, end=hi),
                    payload={"cluster_template": top.template, "cluster_count": top.count},
                    snippet=top.representatives[0] if top.representatives else None,
                )
            )

    # 指标证据（步骤 5 产物：与场景路由同一份检测结果，含正常序列）
    abnormal = [a for a in all_results if a.is_anomaly]
    evidence.append(
        Evidence(
            evidence_id="ev-metric",
            type=EvidenceType.METRIC,
            source="anomaly_detection",
            summary=f"指标检测 {len(all_results)} 个序列，{len(abnormal)} 个异常",
            time_range=TimeRange(start=lo, end=hi),
            payload={"anomalies": abnormal, "tech_signal_clean": not abnormal},
        )
    )

    return evidence


def _print_report(report) -> None:
    """以可读形式打印一份 RCAReport。"""
    print("=" * 60)
    print(f"RCA 报告 {report.report_id}  （事件 {report.incident_id}）")
    print(f"场景: {report.scenario.value}  状态: {report.meta.status.value}")
    if report.business_context.is_present:
        print(
            f"业务上下文: {report.business_context.entity}/{report.business_context.symptom}"
            f"（来源 {report.business_context.source.value}）"
        )
    print("-" * 60)

    if report.root_cause_candidates:
        print("根因候选：")
        for c in report.root_cause_candidates:
            print(f"  rank{c.rank} [{c.confidence:.2f} {c.confidence_level.value}] {c.hypothesis}")
            if c.reasoning:
                print(f"       ↳ {c.reasoning}")
            if c.supporting_evidence:
                print(f"       支持证据: {', '.join(c.supporting_evidence)}")
            if c.refuting_evidence:
                print(f"       反驳证据: {', '.join(c.refuting_evidence)}")
    else:
        print("根因候选：无（证据不足以生成假设）")
        if report.meta.human_feedback and report.meta.human_feedback.get("validation_violations"):
            print(f"  降级说明: {report.meta.human_feedback['validation_violations']}")

    print("-" * 60)
    print("时间线：")
    for t in report.timeline:
        print(f"  {t.at:%H:%M:%S} [{t.significance.value}] {t.event}")

    print("-" * 60)
    print("修复建议：")
    for r in report.remediation_suggestions:
        print(f"  [{r.priority.value}] {r.action}")

    print("-" * 60)
    print(f"证据 {len(report.evidence_list)} 条，审计 {len(report.audit_trail)} 条，"
          f"耗时 {report.meta.duration_sec}s，token {report.meta.total_token_cost}")


def _run_report(args) -> int:
    """--report 全流程：场景→trace→日志→指标→假设→报告。"""
    from app.pipeline.anomaly_detection import detect_anomaly
    from app.pipeline.hypothesis_scoring import generate_hypotheses
    from app.pipeline.report_generation import generate_report
    from app.pipeline.scenario_router import route_scenario
    from app.tools.mock_datasource import MockMetricDatasource
    from app.tools.trace_reconstruction import TraceReconstructionError, rebuild_trace

    t0 = _time.time()
    lo, hi = _event_time_window()
    event_start = _fault_start()  # 事件起点用故障起点（21:00），不用窗口起点（20:30）

    # 事件文本：--scenario 优先，否则用告警模板
    incident_text = args.scenario or "checkout error_rate 异常（示例告警）"
    # traceId：优先 --trace-id 显式指定；位置参数只在形如 tr-mock-* 时采用
    # （--report 下位置参数常是场景文本，不能误当 traceId）
    trace_id = args.trace_id_opt or (
        args.trace_id if args.trace_id and args.trace_id.startswith("tr-mock-") else None
    )

    # 步骤 2：场景路由（指标检测全部结果，含正常 → 判断"技术信号干净"）
    metric_src = MockMetricDatasource()
    series = list(metric_src.series.values())
    if args.service:
        series = [s for s in series if s.labels.get("service") == args.service]
    all_results = [detect_anomaly(s) for s in series]
    scenario = route_scenario(incident_text=incident_text, anomalies=all_results, llm=None)
    print(f"步骤2 场景: {scenario.to_summary()}")

    # 步骤 3：trace 重建（tr-mock-0001 含完整调用链与错误跳）
    log_src = _build_source()
    graph = None
    if trace_id:
        try:
            graph = rebuild_trace(log_src, trace_id)
            print(f"步骤3 trace: {graph.trace_id} 重建 {len(graph.hops)} 跳（{graph.reconstruction_confidence.value}）")
        except TraceReconstructionError as e:
            print(f"步骤3 trace: 重建失败（{e}），假设打分将缺 trace 证据")

    # 步骤 4+5：收集证据（日志簇 + 指标 + trace + 场景）——指标用与场景路由
    # 同一份 all_results（同一服务过滤），保证"技术信号干净"判定一致
    evidence = _build_collected_evidence(log_src, metric_src, scenario, graph, trace_id, all_results)
    print(f"步骤4/5 证据: 共 {len(evidence)} 条（{', '.join(e.type.value for e in evidence)}）")

    # 步骤 6：假设生成/打分（纯规则确定性模式）
    hyps = generate_hypotheses(evidence=evidence, scenario=scenario, graph=graph, event_start=event_start, llm=None)
    print(f"步骤6 假设: {hyps.to_summary()}")
    for c in hyps.candidates:
        print(f"  rank{c.rank} [{c.confidence:.2f}] {c.hypothesis}")

    # 步骤 7：报告生成（纯确定性组装 + 校验降级）
    report = generate_report(
        report_id="R-mock-0001",
        incident_id="INC-mock-0001",
        event_start=event_start,
        scenario=scenario,
        hypotheses=hyps,
        evidence_list=evidence,
        graph=graph,
        duration_sec=int(_time.time() - t0),
    )
    print()
    _print_report(report)
    return 0


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
    parser.add_argument(
        "--report",
        action="store_true",
        help="一键产出完整 RCAReport（mock 数据全流程：场景→trace→日志→指标→假设→报告）",
    )
    args = parser.parse_args(argv)

    if args.list:
        ids = _list_trace_ids()
        if not ids:
            print("（无可用 traceId）")
        for tid in ids:
            print(tid)
        return 0

    if args.report:
        return _run_report(args)

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
