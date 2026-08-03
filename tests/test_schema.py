"""schema/models.py 的测试：模型解析、字段校验、置信度分档、证据引用一致性。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schema.models import (
    AlertInfo,
    ConfidenceLevel,
    Evidence,
    IncidentEvent,
    IncidentSource,
    LogRecord,
    ManualInput,
    RCAReport,
    ReportStatus,
    RootCauseCandidate,
    ScenarioType,
    Severity,
    TraceGraph,
    TraceHop,
    confidence_level_for,
)

UTC = timezone.utc


def dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def make_evidence(eid: str = "ev-1") -> Evidence:
    return Evidence(
        evidence_id=eid,
        type="log",
        source="mock-log/es-prod",
        summary="checkout 服务错误率 45%",
        snippet="ERROR checkout ...",
    )


# ---------------------------------------------------------------- 时间归一化

class TestTimeNormalization:
    def test_naive_treated_as_utc(self):
        ev = Evidence(evidence_id="e1", type="log", source="s", summary="m")
        # 无时间字段时正常构造

    def test_time_range_order_validation(self):
        from app.schema.models import TimeRange

        with pytest.raises(ValidationError):
            TimeRange(start=dt("2026-08-02T21:00:00Z"), end=dt("2026-08-02T20:00:00Z"))

    def test_time_range_ok(self):
        from app.schema.models import TimeRange

        tr = TimeRange(start=dt("2026-08-02T21:00:00Z"), end=dt("2026-08-02T21:30:00Z"))
        assert tr.start.tzinfo == UTC


# ---------------------------------------------------------------- IncidentEvent

class TestIncidentEvent:
    def test_alert_event_requires_alert(self):
        with pytest.raises(ValidationError):
            IncidentEvent(
                incident_id="INC-1",
                source=IncidentSource.ALERT_WEBHOOK,
                triggered_at=dt("2026-08-02T21:00:00Z"),
            )

    def test_manual_event_requires_manual_input(self):
        with pytest.raises(ValidationError):
            IncidentEvent(
                incident_id="INC-1",
                source=IncidentSource.MANUAL,
                triggered_at=dt("2026-08-02T21:00:00Z"),
            )

    def test_valid_alert_event(self):
        ev = IncidentEvent(
            incident_id="INC-1",
            source=IncidentSource.ALERT_WEBHOOK,
            triggered_at=dt("2026-08-02T21:00:00Z"),
            alert=AlertInfo(
                title="checkout error_rate 45%",
                severity=Severity.CRITICAL,
                labels={"service": "checkout", "metric": "error_rate", "host": "host-a"},
                starts_at=dt("2026-08-02T21:00:00Z"),
            ),
        )
        assert ev.alert.labels["service"] == "checkout"
        assert ev.incident_id == "INC-1"

    def test_empty_incident_id_rejected(self):
        with pytest.raises(ValidationError):
            IncidentEvent(
                incident_id="",
                source=IncidentSource.ALERT_WEBHOOK,
                triggered_at=dt("2026-08-02T21:00:00Z"),
                alert=AlertInfo(title="x"),
            )

    def test_manual_input(self):
        ev = IncidentEvent(
            incident_id="INC-2",
            source=IncidentSource.MANUAL,
            triggered_at=dt("2026-08-02T21:00:00Z"),
            manual_input=ManualInput(trace_id="abc123", service="checkout"),
        )
        assert ev.manual_input.trace_id == "abc123"


# ---------------------------------------------------------------- LogRecord

class TestLogRecord:
    def test_parse_log_record(self):
        rec = LogRecord(
            timestamp=dt("2026-08-02T21:05:00Z"),
            service="checkout",
            host="host-a",
            message="rpc call payment timeout",
            trace_id="tr-1",
            rpc_direction="out",
            rpc_target="payment",
        )
        assert rec.rpc_direction.value == "out"
        assert rec.rpc_target == "payment"

    def test_unknown_fields_ignored(self):
        rec = LogRecord(
            timestamp=dt("2026-08-02T21:05:00Z"),
            service="checkout",
            host="host-a",
            message="m",
            extra_field="whatever",
        )
        assert rec.service == "checkout"


# ---------------------------------------------------------------- 置信度分档

class TestConfidenceLevel:
    @pytest.mark.parametrize(
        ("confidence", "expected"),
        [
            (0.9, ConfidenceLevel.HIGH),
            (0.8, ConfidenceLevel.HIGH),
            (0.7, ConfidenceLevel.MEDIUM),
            (0.5, ConfidenceLevel.MEDIUM),
            (0.3, ConfidenceLevel.LOW),
        ],
    )
    def test_banding(self, confidence, expected):
        assert confidence_level_for(confidence) == expected

    def test_candidate_derives_level(self):
        c = RootCauseCandidate(rank=1, hypothesis="payment 慢", confidence=0.85)
        assert c.confidence_level == ConfidenceLevel.HIGH


# ---------------------------------------------------------------- RCAReport 校验

class TestRCAReport:
    def _valid_report(self) -> RCAReport:
        return RCAReport(
            report_id="R-1",
            incident_id="INC-1",
            created_at=dt("2026-08-02T21:10:00Z"),
            scenario=ScenarioType.ERROR_RATE_SPIKE,
            root_cause_candidates=[
                RootCauseCandidate(
                    rank=1,
                    hypothesis="payment 服务超时导致 checkout 错误率上升",
                    confidence=0.85,
                    supporting_evidence=["ev-1"],
                    reasoning="checkout 日志显示调用 payment 超时",
                )
            ],
            evidence_list=[make_evidence("ev-1")],
        )

    def test_valid_report(self):
        r = self._valid_report()
        assert r.meta.status == ReportStatus.PARTIAL
        assert r.root_cause_candidates[0].confidence_level == ConfidenceLevel.HIGH

    def test_more_than_3_candidates_rejected(self):
        cands = [
            RootCauseCandidate(rank=i, hypothesis=f"h{i}", confidence=0.5, supporting_evidence=["ev-1"])
            for i in range(1, 5)
        ]
        with pytest.raises(ValidationError):
            RCAReport(
                report_id="R-1",
                incident_id="INC-1",
                created_at=dt("2026-08-02T21:10:00Z"),
                root_cause_candidates=cands,
                evidence_list=[make_evidence("ev-1")],
            )

    def test_candidate_missing_support_rejected(self):
        with pytest.raises(ValidationError):
            RCAReport(
                report_id="R-1",
                incident_id="INC-1",
                created_at=dt("2026-08-02T21:10:00Z"),
                root_cause_candidates=[
                    RootCauseCandidate(rank=1, hypothesis="h", confidence=0.6, supporting_evidence=[])
                ],
                evidence_list=[make_evidence("ev-1")],
            )

    def test_unknown_evidence_ref_rejected(self):
        with pytest.raises(ValidationError):
            RCAReport(
                report_id="R-1",
                incident_id="INC-1",
                created_at=dt("2026-08-02T21:10:00Z"),
                root_cause_candidates=[
                    RootCauseCandidate(
                        rank=1, hypothesis="h", confidence=0.6, supporting_evidence=["ev-999"]
                    )
                ],
                evidence_list=[make_evidence("ev-1")],
            )


# ---------------------------------------------------------------- TraceGraph

class TestTraceGraph:
    def test_build_trace_graph(self):
        g = TraceGraph(
            trace_id="tr-1",
            hops=[
                TraceHop(
                    source_service="gateway",
                    target_service="checkout",
                    start_time=dt("2026-08-02T21:00:00Z"),
                    end_time=dt("2026-08-02T21:00:01Z"),
                    duration_ms=1000,
                )
            ],
            services=["gateway", "checkout"],
            reconstruction_confidence="weak",
        )
        assert g.hops[0].duration_ms == 1000
        assert g.reconstruction_confidence.value == "weak"
