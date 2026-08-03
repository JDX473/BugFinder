"""report_generation.py 的测试：报告组装、校验降级、时间线、业务上下文、修复建议。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.pipeline.hypothesis_scoring import HypothesisScoringResult, generate_hypotheses
from app.pipeline.report_generation import (
    ReportValidationError,
    ReportValidator,
    generate_report,
)
from app.pipeline.scenario_router import ScenarioResult, route_scenario
from app.schema.models import (
    BusinessContext,
    Evidence,
    EvidenceType,
    ReconstructionConfidence,
    ReportStatus,
    RootCauseCandidate,
    ScenarioType,
    TimeRange,
    TimelineSignificance,
    TraceGraph,
    TraceHop,
)

UTC = timezone.utc


def dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _error_scenario() -> ScenarioResult:
    from app.pipeline.anomaly_detection import AnomalyShape, MetricAnomaly

    a = MetricAnomaly(
        metric="error_rate", shape=AnomalyShape.SPIKE_UP, baseline_mean=1.0,
        anomaly_start=dt("2026-08-02T21:00:00Z"), current_mean=5.0, ratio=5.0,
        detail="spike_up", is_anomaly=True,
    )
    return route_scenario(incident_text="订单失败率高", anomalies=[a], llm=None)


def _business_scenario() -> ScenarioResult:
    from app.pipeline.anomaly_detection import AnomalyShape, MetricAnomaly

    cpu = MetricAnomaly(
        metric="cpu_usage", shape=AnomalyShape.NORMAL, baseline_mean=50.0,
        anomaly_start=None, current_mean=50.0, ratio=1.0,
        detail="未超过阈值", is_anomaly=False,
    )
    return route_scenario(incident_text="用户反馈车门打不开", anomalies=[cpu], llm=None)


def _hypotheses() -> HypothesisScoringResult:
    """构造一个含 1 个候选的假设结果（复用 generate_hypotheses 保证真实）。"""
    from app.pipeline.anomaly_detection import AnomalyShape, MetricAnomaly

    a = MetricAnomaly(
        metric="error_rate", shape=AnomalyShape.SPIKE_UP, baseline_mean=1.0,
        anomaly_start=dt("2026-08-02T21:00:00Z"), current_mean=5.0, ratio=5.0,
        detail="spike_up", is_anomaly=True,
    )
    ev = Evidence(
        evidence_id="ev-metric",
        type=EvidenceType.METRIC,
        source="anomaly_detection",
        summary="指标检测 1 个异常",
        time_range=TimeRange(start=dt("2026-08-02T20:30:00Z"), end=dt("2026-08-02T21:30:00Z")),
        payload={"anomalies": [a], "tech_signal_clean": False},
    )
    return generate_hypotheses(
        evidence=[ev], scenario=_error_scenario(),
        event_start=dt("2026-08-02T21:00:00Z"), llm=None,
    )


def _empty_hypotheses() -> HypothesisScoringResult:
    return HypothesisScoringResult(candidates=[])


def _graph() -> TraceGraph:
    return TraceGraph(
        trace_id="tr-1",
        hops=[
            TraceHop(
                source_service="gateway", target_service="checkout",
                start_time=dt("2026-08-02T21:00:00Z"), end_time=dt("2026-08-02T21:00:01Z"),
                duration_ms=1000, has_error=False,
            ),
            TraceHop(
                source_service="checkout", target_service="payment",
                start_time=dt("2026-08-02T21:00:01Z"), end_time=dt("2026-08-02T21:00:11Z"),
                duration_ms=10000, has_error=True, error_summary="TimeoutException",
            ),
        ],
        services=["gateway", "checkout", "payment"],
        reconstruction_confidence=ReconstructionConfidence.STRONG,
    )


def _base_kwargs(**overrides) -> dict:
    kwargs = dict(
        report_id="R-1",
        incident_id="INC-1",
        event_start=dt("2026-08-02T21:00:00Z"),
        scenario=_error_scenario(),
        hypotheses=_hypotheses(),
        evidence_list=[
            Evidence(evidence_id="ev-metric", type=EvidenceType.METRIC, source="s", summary="m"),
        ],
        token_cost=1234,
        duration_sec=42,
    )
    kwargs.update(overrides)
    return kwargs


# ---------------------------------------------------------------- 基础组装

class TestGenerateReport:
    def test_basic_report(self):
        report = generate_report(**_base_kwargs())
        assert report.report_id == "R-1"
        assert report.incident_id == "INC-1"
        assert report.scenario == ScenarioType.ERROR_RATE_SPIKE
        assert report.meta.status == ReportStatus.COMPLETED
        assert report.meta.total_token_cost == 1234
        assert report.meta.duration_sec == 42
        assert len(report.root_cause_candidates) >= 1

    def test_candidates_rank_continuous(self):
        report = generate_report(**_base_kwargs())
        ranks = [c.rank for c in report.root_cause_candidates]
        assert ranks == list(range(1, len(ranks) + 1))

    def test_evidence_carried(self):
        evs = [Evidence(evidence_id="ev-1", type="log", source="s", summary="m")]
        report = generate_report(**_base_kwargs(evidence_list=evs))
        assert report.evidence_list == evs

    def test_audit_trail_written(self):
        report = generate_report(**_base_kwargs())
        steps = {a.step for a in report.audit_trail}
        assert "2_scenario" in steps
        assert "6_hypothesis" in steps

    def test_remediation_for_error_spike(self):
        report = generate_report(**_base_kwargs())
        assert report.remediation_suggestions  # 非空
        assert any("错误" in r.action for r in report.remediation_suggestions)

    def test_business_context_in_report(self):
        """business_logic 场景：业务上下文进报告 + 业务修复建议。"""
        report = generate_report(
            **_base_kwargs(scenario=_business_scenario(), hypotheses=_empty_hypotheses())
        )
        assert report.business_context.entity == "车门"
        assert report.business_context.symptom == "打不开"
        assert report.scenario == ScenarioType.BUSINESS_LOGIC
        assert any("业务规则" in r.action for r in report.remediation_suggestions)


# ---------------------------------------------------------------- 时间线

class TestTimeline:
    def test_timeline_has_cause_and_symptom(self):
        report = generate_report(**_base_kwargs(graph=_graph()))
        sigs = {t.significance for t in report.timeline}
        assert TimelineSignificance.CAUSE in sigs
        assert TimelineSignificance.SYMPTOM in sigs

    def test_timeline_sorted(self):
        report = generate_report(**_base_kwargs())
        times = [t.at for t in report.timeline]
        assert times == sorted(times)

    def test_timeline_event_start_is_symptom(self):
        report = generate_report(**_base_kwargs())
        trigger = [t for t in report.timeline if "事件触发" in t.event]
        assert trigger
        assert trigger[0].significance == TimelineSignificance.SYMPTOM


# ---------------------------------------------------------------- 校验降级

class TestReportValidator:
    def test_valid_candidates_pass(self):
        cand = RootCauseCandidate(rank=1, hypothesis="h", confidence=0.8, supporting_evidence=["ev-1"])
        ev = Evidence(evidence_id="ev-1", type="log", source="s", summary="m")
        usable, violations = ReportValidator().validate([cand], [ev])
        assert usable == [cand]
        assert violations == []

    def test_missing_support_dropped(self):
        cand = RootCauseCandidate(rank=1, hypothesis="h", confidence=0.8)
        usable, violations = ReportValidator().validate([cand], [])
        assert usable == []
        assert "supporting_evidence" in violations[0]

    def test_unknown_ref_dropped(self):
        cand = RootCauseCandidate(rank=1, hypothesis="h", confidence=0.8, supporting_evidence=["ev-999"])
        usable, violations = ReportValidator().validate([cand], [])
        assert usable == []
        assert "ev-999" in violations[0]

    def test_over_cap_trimmed(self):
        cands = [
            RootCauseCandidate(rank=i, hypothesis=f"h{i}", confidence=0.5, supporting_evidence=["ev-1"])
            for i in range(1, 5)
        ]
        ev = Evidence(evidence_id="ev-1", type="log", source="s", summary="m")
        usable, violations = ReportValidator().validate(cands, [ev])
        assert len(usable) == 3  # 上限 3
        assert violations  # 有降级说明

    def test_no_candidates(self):
        usable, violations = ReportValidator().validate([], [])
        assert usable == []
        assert violations == ["无候选"]


class TestDowngrade:
    def test_no_candidates_marks_partial(self):
        """候选为空 → 报告 meta 标 partial + 降级说明（不整份丢弃）。"""
        report = generate_report(**_base_kwargs(hypotheses=_empty_hypotheses()))
        assert report.meta.status == ReportStatus.PARTIAL
        assert "无候选" in report.meta.human_feedback["validation_violations"]
        assert report.root_cause_candidates == []

    def test_missing_support_marks_partial(self):
        """候选缺支持证据 → 被降级丢弃，报告仍能产出。"""
        cand = RootCauseCandidate(rank=1, hypothesis="h", confidence=0.8)
        hyps = HypothesisScoringResult(candidates=[cand])
        report = generate_report(
            **_base_kwargs(
                hypotheses=hyps,
                evidence_list=[Evidence(evidence_id="ev-1", type="log", source="s", summary="m")],
            )
        )
        assert report.meta.status == ReportStatus.PARTIAL
        assert report.root_cause_candidates == []
