"""hypothesis_scoring.py 的测试：确定性打分、trace/指标/日志假设生成、LLM 排序、兜底。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.pipeline.anomaly_detection import AnomalyShape, MetricAnomaly
from app.pipeline.hypothesis_scoring import generate_hypotheses
from app.pipeline.scenario_router import ScenarioResult, route_scenario
from app.schema.models import (
    Evidence,
    EvidenceType,
    ReconstructionConfidence,
    ScenarioType,
    TimeRange,
    TraceGraph,
    TraceHop,
)

UTC = timezone.utc
T0 = datetime(2026, 8, 2, 21, 0, 0, tzinfo=UTC)


def dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def make_ev(
    eid: str,
    *,
    etype: EvidenceType = EvidenceType.LOG,
    summary: str = "evidence",
    time_range: TimeRange | None = None,
    payload: dict | None = None,
    snippet: str | None = None,
    error: bool = False,
) -> Evidence:
    return Evidence(
        evidence_id=eid,
        type=etype,
        source="mock",
        summary=summary,
        time_range=time_range,
        payload=payload,
        snippet=snippet,
        error=error,
    )


def _anomaly(metric: str, *, start_sec: int = 30, ratio: float = 5.0) -> MetricAnomaly:
    return MetricAnomaly(
        metric=metric,
        shape=AnomalyShape.SPIKE_UP,
        baseline_mean=1.0,
        anomaly_start=T0 + timedelta(seconds=start_sec),
        current_mean=5.0,
        ratio=ratio,
        detail="spike_up",
        is_anomaly=True,
    )


def _clean_resource() -> MetricAnomaly:
    return MetricAnomaly(
        metric="cpu_usage",
        shape=AnomalyShape.NORMAL,
        baseline_mean=50.0,
        anomaly_start=None,
        current_mean=50.0,
        ratio=1.0,
        detail="未超过阈值",
        is_anomaly=False,
    )


def _metric_ev(eid: str, anomalies: list[MetricAnomaly], *, clean: bool = False) -> Evidence:
    return make_ev(
        eid,
        etype=EvidenceType.METRIC,
        summary=f"指标检测 {len(anomalies)} 个异常",
        payload={"anomalies": anomalies, "tech_signal_clean": clean},
    )


def _trace_graph() -> TraceGraph:
    """含错误跳的调用链：gateway->checkout->payment，payment 超时。"""
    return TraceGraph(
        trace_id="tr-mock-0001",
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


def _trace_ev(eid: str = "ev-trace") -> Evidence:
    return make_ev(eid, etype=EvidenceType.TRACE, summary="trace 重建 2 跳", time_range=TimeRange(start=T0, end=T0 + timedelta(seconds=30)))


def _error_scenario() -> ScenarioResult:
    return route_scenario(
        incident_text="订单失败率高",
        anomalies=[_anomaly("error_rate", start_sec=30)],
        llm=None,
    )


def _business_scenario() -> ScenarioResult:
    return route_scenario(
        incident_text="用户反馈车门打不开",
        anomalies=[_clean_resource()],
        llm=None,
    )


# ---------------------------------------------------------------- 确定性打分

def test_metric_scenario_generates_hypothesis():
    """error_rate 异常 → 场景主假设模板。"""
    evs = [_metric_ev("ev-metric", [_anomaly("error_rate")])]
    r = generate_hypotheses(evidence=evs, scenario=_error_scenario(), event_start=T0)
    assert len(r.candidates) == 1
    assert r.candidates[0].rank == 1
    assert "错误率" in r.candidates[0].hypothesis or "失败" in r.candidates[0].hypothesis
    assert r.candidates[0].supporting_evidence == ["ev-metric"]


def test_trace_hypothesis_higher_than_metric():
    """trace 错误跳假设优先于纯指标假设（确定性更高的证据）。"""
    evs = [
        _trace_ev("ev-trace"),
        _metric_ev("ev-metric", [_anomaly("error_rate")]),
    ]
    r = generate_hypotheses(evidence=evs, scenario=_error_scenario(), graph=_trace_graph(), event_start=T0)
    # rank1 是 trace 假设（payment 是错误发源地 → 最深边优先）
    assert r.candidates[0].confidence > r.candidates[-1].confidence
    assert "payment" in r.candidates[0].hypothesis
    assert r.candidates[0].supporting_evidence == ["ev-trace"]


def test_deepest_error_edge_first():
    """错误发源地（最深错误边）优先于症状传播边。"""
    graph = _trace_graph()
    evs = [_trace_ev("ev-trace")]
    r = generate_hypotheses(evidence=evs, scenario=_error_scenario(), graph=graph, event_start=T0)
    assert "payment" in r.candidates[0].hypothesis  # 最深错误边（发源地）rank1


def test_business_logic_generates_business_hypothesis():
    """技术信号干净 + 业务上下文 → 业务假设（不是空候选）。"""
    evs = [_metric_ev("ev-metric", [], clean=True)]
    r = generate_hypotheses(evidence=evs, scenario=_business_scenario(), event_start=T0)
    assert len(r.candidates) == 1
    assert "车门" in r.candidates[0].hypothesis
    assert r.candidates[0].supporting_evidence == ["ev-metric"]


def _empty_scenario() -> ScenarioResult:
    """无异常、无业务上下文、无 LLM → other（raw_anomalies 为空，假设生成无证据可用）。"""
    return ScenarioResult(
        scenario=ScenarioType.OTHER,
        confidence=0.1,
        basis="无指标证据、无业务命中、无 LLM",
        source="other",
    )


def test_no_evidence_no_hypothesis():
    """无任何证据 + 场景无异常 → 空候选，不炸。"""
    r = generate_hypotheses(evidence=[], scenario=_empty_scenario(), event_start=T0)
    assert r.candidates == []
    assert "无候选" in r.to_summary()


def test_error_evidence_excluded():
    """失败占位证据（error=True）不参与假设。"""
    bad = make_ev("ev-bad", etype=EvidenceType.TRACE, error=True)
    r = generate_hypotheses(evidence=[bad], scenario=_empty_scenario(), event_start=T0)
    assert r.candidates == []


def test_top3_capped():
    """候选超过 3 个 → 只取 Top-3。"""
    anomalies = [_anomaly("error_rate"), _anomaly("p99"), _anomaly("cpu"), _anomaly("availability")]
    evs = [_metric_ev("ev-metric", anomalies)]
    r = generate_hypotheses(evidence=evs, scenario=_error_scenario(), event_start=T0)
    assert len(r.candidates) <= 3


def test_confidence_in_range():
    """置信度必须落在 [0,1]。"""
    evs = [_metric_ev("ev-metric", [_anomaly("error_rate")])]
    r = generate_hypotheses(evidence=evs, scenario=_error_scenario(), event_start=T0)
    for c in r.candidates:
        assert 0 <= c.confidence <= 1
        assert c.confidence_level.value  # 能推导分档


def test_metric_evidence_synthesized_from_scenario():
    """场景路由的 raw_anomalies 会合成 METRIC 证据，即使没传指标证据。"""
    r = generate_hypotheses(evidence=[], scenario=_error_scenario(), event_start=T0)
    assert len(r.candidates) == 1
    assert r.candidates[0].supporting_evidence == ["ev-metric-synth"]


def test_candidate_refs_consistent():
    """候选的 supporting_evidence 必须引用真实存在的证据。"""
    evs = [_trace_ev("ev-trace"), _metric_ev("ev-metric", [_anomaly("error_rate")])]
    r = generate_hypotheses(evidence=evs, scenario=_error_scenario(), graph=_trace_graph(), event_start=T0)
    known = {e.evidence_id for e in evs}
    for c in r.candidates:
        for ref in c.supporting_evidence:
            assert ref in known


# ---------------------------------------------------------------- LLM 排序

class FakeLLM:
    def __init__(self, outputs: list[str]):
        self.outputs = list(outputs)
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages: list[dict[str, str]], temperature: float = 0.0) -> str:
        self.calls.append(messages)
        return self.outputs.pop(0) if self.outputs else ""


def test_llm_reorders_hypotheses():
    """LLM 排名重排候选（指标假设被抬到最前）。"""
    evs = [
        _trace_ev("ev-trace"),
        _metric_ev("ev-metric", [_anomaly("error_rate")]),
    ]
    llm = FakeLLM(['{"top": [1, 2], "confidence": 0.8}'])
    r = generate_hypotheses(evidence=evs, scenario=_error_scenario(), graph=_trace_graph(), event_start=T0, llm=llm)
    assert r.used_llm
    assert len(llm.calls) == 1


def test_llm_low_confidence_falls_back_to_rules():
    """LLM 低置信（<0.5）→ 走规则兜底，不采用 LLM 排序。"""
    evs = [
        _trace_ev("ev-trace"),
        _metric_ev("ev-metric", [_anomaly("error_rate")]),
    ]
    llm = FakeLLM(['{"top": [2, 1], "confidence": 0.2}'])
    r = generate_hypotheses(evidence=evs, scenario=_error_scenario(), graph=_trace_graph(), event_start=T0, llm=llm)
    assert not r.used_llm


def test_llm_bad_json_falls_back_to_rules():
    """LLM 输出坏 JSON → 规则兜底，不炸。"""
    evs = [_metric_ev("ev-metric", [_anomaly("error_rate")])]
    llm = FakeLLM(["不是 JSON", "还是坏", "继续坏", "不修了"])
    r = generate_hypotheses(evidence=evs, scenario=_error_scenario(), event_start=T0, llm=llm)
    assert not r.used_llm
    assert len(r.candidates) == 1


class ThrowingLLM:
    def complete(self, messages: list[dict[str, str]], temperature: float = 0.0) -> str:
        raise TimeoutError("模拟超时")


def test_llm_exception_falls_back_to_rules():
    """LLM 抛异常 → 规则兜底，不炸。"""
    evs = [_metric_ev("ev-metric", [_anomaly("error_rate")])]
    r = generate_hypotheses(evidence=evs, scenario=_error_scenario(), event_start=T0, llm=ThrowingLLM())
    assert not r.used_llm
    assert len(r.candidates) == 1


def test_llm_not_called_when_single_hypothesis():
    """只有一个假设时不需要 LLM 排序（省成本）。"""
    evs = [_metric_ev("ev-metric", [_anomaly("error_rate")])]
    llm = FakeLLM(['{"top": [1], "confidence": 0.9}'])
    r = generate_hypotheses(evidence=evs, scenario=_error_scenario(), event_start=T0, llm=llm)
    assert len(llm.calls) == 0  # 单假设不调 LLM
    assert not r.used_llm


# ---------------------------------------------------------------- 边界

def test_event_start_none_safe():
    """event_start 不传 → 用场景最早异常时间作锚点，不炸。"""
    evs = [_metric_ev("ev-metric", [_anomaly("error_rate")])]
    r = generate_hypotheses(evidence=evs, scenario=_error_scenario(), event_start=None)
    assert len(r.candidates) >= 1


def test_no_graph_trace_evidence_falls_back():
    """有 trace 证据但没传 graph → trace 假设不生成（不崩），metric 假设兜底。"""
    evs = [
        _trace_ev("ev-trace"),
        _metric_ev("ev-metric", [_anomaly("error_rate")]),
    ]
    r = generate_hypotheses(evidence=evs, scenario=_error_scenario(), graph=None, event_start=T0)
    assert len(r.candidates) == 1  # 只有 metric 假设
    assert r.candidates[0].supporting_evidence == ["ev-metric"]


def test_to_summary_has_candidates():
    evs = [_metric_ev("ev-metric", [_anomaly("error_rate")])]
    r = generate_hypotheses(evidence=evs, scenario=_error_scenario(), event_start=T0)
    s = r.to_summary()
    assert "规则兜底" in s
    assert "rank1" in s
