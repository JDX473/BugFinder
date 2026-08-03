"""scenario_router.py 的测试：指标映射、多指标最早异常、业务白名单、LLM 兜底、other 兜底。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.pipeline.anomaly_detection import AnomalyShape, MetricAnomaly
from app.pipeline.scenario_router import (
    BusinessWhitelist,
    ScenarioResult,
    ScenarioRoutingError,
    metric_to_scenario,
    route_scenario,
)
from app.schema.models import BusinessContext, ScenarioType

T0 = datetime(2026, 8, 2, 21, 0, 0, tzinfo=timezone.utc)


def _anomaly(metric: str, *, start_sec: int = 30, ratio: float = 5.0, shape: AnomalyShape = AnomalyShape.SPIKE_UP, is_anomaly: bool = True) -> MetricAnomaly:
    """构造一个 MetricAnomaly。异常指标带起始时间；正常指标无起始时间。"""
    return MetricAnomaly(
        metric=metric,
        shape=shape,
        baseline_mean=1.0,
        anomaly_start=datetime(2026, 8, 2, 21, start_sec, tzinfo=timezone.utc) if is_anomaly else None,
        current_mean=10.0 if is_anomaly else 1.0,
        ratio=ratio if is_anomaly else 1.0,
        is_anomaly=is_anomaly,
    )


def _clean_resource() -> MetricAnomaly:
    """技术信号干净的代表：健康资源指标。"""
    return _anomaly("cpu_usage", is_anomaly=False)


# ---------------------------------------------------------------- 指标 → 场景映射

def test_metric_to_scenario_mapping():
    assert metric_to_scenario("checkout_error_rate") == ScenarioType.ERROR_RATE_SPIKE
    assert metric_to_scenario("p99_latency") == ScenarioType.LATENCY_SPIKE
    assert metric_to_scenario("response_time") == ScenarioType.LATENCY_SPIKE
    assert metric_to_scenario("cpu_usage") == ScenarioType.RESOURCE_SATURATION
    assert metric_to_scenario("availability") == ScenarioType.AVAILABILITY_DROP
    assert metric_to_scenario("unknown_metric") is None


def test_route_metric_error_rate():
    r = route_scenario(incident_text="订单失败率高", anomalies=[_anomaly("error_rate")], llm=None)
    assert r.scenario == ScenarioType.ERROR_RATE_SPIKE
    assert r.source == "metric"
    assert r.earliest_anomaly is not None
    assert r.confidence == 0.9


def test_route_metric_resource():
    r = route_scenario(incident_text="cpu 打满", anomalies=[_anomaly("cpu_usage")], llm=None)
    assert r.scenario == ScenarioType.RESOURCE_SATURATION


def test_route_metric_latency_and_availability():
    assert route_scenario(incident_text="x", anomalies=[_anomaly("p99")], llm=None).scenario == ScenarioType.LATENCY_SPIKE
    assert route_scenario(incident_text="x", anomalies=[_anomaly("availability")], llm=None).scenario == ScenarioType.AVAILABILITY_DROP


def test_route_metric_unmapped_indicator():
    # 指标异常但指标名无法映射 → 不能归场景，走 other（有异常≠能路由）
    r = route_scenario(incident_text="x", anomalies=[_anomaly("weird_metric")], llm=None)
    assert r.scenario == ScenarioType.OTHER


def test_route_multiple_metrics_takes_earliest():
    # error_rate 后异常、p99 先异常 → 主场景 latency_spike（时间先验）
    r = route_scenario(
        incident_text="延迟和错误率都高",
        anomalies=[_anomaly("error_rate", start_sec=40), _anomaly("p99_latency", start_sec=35)],
        llm=None,
    )
    assert r.scenario == ScenarioType.LATENCY_SPIKE
    assert r.earliest_anomaly.metric == "p99_latency"


def test_route_multiple_metrics_no_start_keeps_input_order():
    # 都没有起始时间 → 按输入序取第一个
    r = route_scenario(
        incident_text="x",
        anomalies=[
            MetricAnomaly(metric="error_rate", shape=AnomalyShape.SPIKE_UP, baseline_mean=1.0, anomaly_start=None, current_mean=5.0, ratio=5.0, is_anomaly=True),
            MetricAnomaly(metric="p99", shape=AnomalyShape.SPIKE_UP, baseline_mean=1.0, anomaly_start=None, current_mean=5.0, ratio=5.0, is_anomaly=True),
        ],
        llm=None,
    )
    assert r.scenario == ScenarioType.ERROR_RATE_SPIKE


# ---------------------------------------------------------------- 业务白名单

def test_route_business_logic():
    r = route_scenario(incident_text="用户反馈车门打不开", anomalies=[_clean_resource()], llm=None)
    assert r.scenario == ScenarioType.BUSINESS_LOGIC
    assert r.source == "business"
    assert r.business_context.entity == "车门"
    assert r.business_context.symptom == "打不开"
    assert r.business_context.source == "rule"


def test_route_business_logic_multiple_matches_first_wins():
    # "车门打不开"（白名单第 1 条）在 "无法开门"（第 2 条）之前 → 命中打不开（优先级）
    # 用同时含两个症状短语的文本来验证"前面的先匹配"不变量。
    r = route_scenario(incident_text="用户反馈车门打不开，也尝试了无法开门", anomalies=[_clean_resource()], llm=None)
    assert r.scenario == ScenarioType.BUSINESS_LOGIC
    assert r.business_context.symptom == "打不开"  # 前面的条目优先


def test_business_whitelist_first_wins_invariant():
    # 白名单顺序优先不变量：同一文本含多个命中条目，取最靠前的
    wl = BusinessWhitelist(entries=(("无法开门", "车门", "无法开门"), ("车门打不开", "车门", "打不开")))
    assert wl.match("无法开门 车门打不开") == ("车门", "无法开门")  # 自定义顺序下取前条目
    wl2 = BusinessWhitelist(entries=(("车门打不开", "车门", "打不开"), ("无法开门", "车门", "无法开门")))
    assert wl2.match("无法开门 车门打不开") == ("车门", "打不开")  # 调换顺序后取新前条目


def test_route_business_whitelist_not_hit():
    # 技术信号干净但文本未命中业务白名单 → 不归 business_logic
    r = route_scenario(incident_text="奇怪的告警:模块变慢", anomalies=[_clean_resource()], llm=None)
    assert r.scenario == ScenarioType.OTHER


def test_route_business_requires_clean_signal():
    # 有异常指标 → 即使业务命中也不归 business_logic（指标优先）
    r = route_scenario(incident_text="车门打不开", anomalies=[_anomaly("error_rate")], llm=None)
    assert r.scenario == ScenarioType.ERROR_RATE_SPIKE
    # 但业务上下文仍抽取（供报告用）
    assert r.business_context.entity == "车门"


def test_route_business_no_metrics_observed():
    # 没有观测到任何指标（连健康资源指标都没有）→ 不归 business_logic
    r = route_scenario(incident_text="车门打不开", anomalies=[], llm=None)
    assert r.scenario == ScenarioType.OTHER


def test_route_business_requires_resource_metric():
    # 观测到的是健康业务指标（error_rate normal），但没观测到资源指标 → 不归 business_logic
    r = route_scenario(incident_text="车门打不开", anomalies=[_anomaly("error_rate", is_anomaly=False)], llm=None)
    assert r.scenario == ScenarioType.OTHER


# ---------------------------------------------------------------- LLM 兜底

class FakeLLM:
    """可编程返回预设输出序列的假模型（与 test_ask_json 一致）。"""

    def __init__(self, outputs: list[str]):
        self.outputs = list(outputs)
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages: list[dict[str, str]], temperature: float = 0.0) -> str:
        self.calls.append(messages)
        return self.outputs.pop(0) if self.outputs else ""


def test_route_llm_fallback_when_no_rule_signal():
    # 无指标证据、无业务命中 → LLM 判定
    llm = FakeLLM(['{"scenario": "latency_spike", "confidence": 0.6, "business_entity": "", "business_symptom": ""}'])
    r = route_scenario(incident_text="订单很慢", anomalies=[], llm=llm)
    assert r.scenario == ScenarioType.LATENCY_SPIKE
    assert r.source == "llm"
    assert r.confidence == 0.6
    assert len(llm.calls) == 1


def test_route_llm_can_return_business_logic():
    # 技术信号干净但白名单未命中 → LLM 判 business_logic（业务语义兜底）
    llm = FakeLLM(['{"scenario": "business_logic", "confidence": 0.7, "business_entity": "充电桩", "business_symptom": "充不进电"}'])
    r = route_scenario(incident_text="充电桩充不进电", anomalies=[_clean_resource()], llm=llm)
    assert r.scenario == ScenarioType.BUSINESS_LOGIC
    assert r.business_context.entity == "充电桩"
    assert r.business_context.symptom == "充不进电"
    assert r.business_context.source == "llm"


def test_route_llm_bad_json_falls_to_other():
    # LLM 连续输出坏 JSON → ask_json 兜底 other（0.1 置信），不炸
    llm = FakeLLM(["这不是 JSON", "还是坏", "继续坏", "不修了"])
    r = route_scenario(incident_text="x", anomalies=[], llm=llm)
    assert r.scenario == ScenarioType.OTHER
    assert r.source == "other"
    assert r.confidence == 0.1


def test_route_llm_invalid_scenario_value_safe():
    # LLM 返回枚举外的场景值 → ask_json 的 enum 校验直接拒绝，走兜底落 other（不抛异常）
    llm = FakeLLM(['{"scenario": "bogus", "confidence": 0.8, "business_entity": "", "business_symptom": ""}'])
    r = route_scenario(incident_text="x", anomalies=[], llm=llm)
    assert r.scenario == ScenarioType.OTHER
    assert r.source == "other"


def test_route_llm_none_disables_llm():
    # llm=None → 不走 LLM 兜底（确定性），直接 other
    r = route_scenario(incident_text="订单很慢", anomalies=[], llm=None)
    assert r.scenario == ScenarioType.OTHER
    assert r.source == "other"


# ---------------------------------------------------------------- 边界

def test_route_empty_incident_text():
    r = route_scenario(incident_text="", anomalies=[], llm=None)
    assert r.scenario == ScenarioType.OTHER


def test_business_whitelist_empty_raises():
    with pytest.raises(ScenarioRoutingError):
        BusinessWhitelist(entries=())


def test_business_whitelist_custom():
    wl = BusinessWhitelist(entries=(("充电桩充不进电", "充电桩", "充不进电"),))
    assert wl.match("充电桩充不进电") == ("充电桩", "充不进电")
    assert wl.match("无关文本") is None


def test_scenario_result_summary():
    r = route_scenario(incident_text="用户反馈车门打不开", anomalies=[_clean_resource()], llm=None)
    s = r.to_summary()
    assert "business_logic" in s
    assert "业务上下文 车门/打不开" in s


# ---------------------------------------------------------------- 评审补强（修复后的行为）

def test_metric_to_scenario_no_bare_substring_matching():
    # 精确词匹配：含 rt/load 子串的业务指标不再误映射（评审 #2/#5）
    assert metric_to_scenario("cart_abandonment_rate") is None
    assert metric_to_scenario("support_ticket_count") is None
    assert metric_to_scenario("download_success_rate") == ScenarioType.AVAILABILITY_DROP  # success 词命中
    assert metric_to_scenario("p99_latency") == ScenarioType.LATENCY_SPIKE
    assert metric_to_scenario("checkout_error_rate") == ScenarioType.ERROR_RATE_SPIKE


def test_route_metric_unmappable_earliest_still_routes():
    # 最早异常不可映射时，仍取可映射的异常路由（评审 #3/#7）
    r = route_scenario(
        incident_text="x",
        anomalies=[_anomaly("network_bytes_in", start_sec=10), _anomaly("error_rate", start_sec=40)],
        llm=None,
    )
    assert r.scenario == ScenarioType.ERROR_RATE_SPIKE
    assert r.earliest_anomaly.metric == "network_bytes_in"  # 仍记录最早者供假设打分


def test_route_metric_earliest_kept_when_mappable():
    # 最早异常本身可映射 → 它就是主场景驱动者
    r = route_scenario(
        incident_text="x",
        anomalies=[_anomaly("p99", start_sec=20), _anomaly("error_rate", start_sec=40)],
        llm=None,
    )
    assert r.scenario == ScenarioType.LATENCY_SPIKE
    assert r.earliest_anomaly.metric == "p99"


def test_route_tech_clean_requires_sufficient_data():
    # 点数不足的资源指标不算"观测到健康"（评审 #1）：不能据此归 business_logic
    insufficient = MetricAnomaly(
        metric="cpu_usage", shape=AnomalyShape.NORMAL, baseline_mean=0.5,
        anomaly_start=None, current_mean=0.5, ratio=1.0,
        detail="点数不足（3 < 8），无法判定", is_anomaly=False,
    )
    r = route_scenario(incident_text="用户反馈车门打不开", anomalies=[insufficient], llm=None)
    assert r.scenario == ScenarioType.OTHER  # 不是 business_logic


def test_route_incident_text_none_safe():
    # free_text/annotations 语义可为 None（评审 #9）：不崩溃，落 other
    r = route_scenario(incident_text=None, anomalies=[], llm=None)
    assert r.scenario == ScenarioType.OTHER
    r2 = route_scenario(incident_text=None, anomalies=[_clean_resource()], llm=None)
    assert r2.scenario == ScenarioType.OTHER  # None 文本不命中白名单


def test_business_whitelist_no_bare_entity_word():
    # 白名单不再用裸实体词"车门"（评审 #8/#12）：纯技术告警含"车门"不误判 business_logic
    r = route_scenario(incident_text="车门服务正在灰度发布，配置下发延迟", anomalies=[_clean_resource()], llm=None)
    assert r.scenario == ScenarioType.OTHER
    # 症状短语仍命中
    r2 = route_scenario(incident_text="用户反馈车门打不开", anomalies=[_clean_resource()], llm=None)
    assert r2.scenario == ScenarioType.BUSINESS_LOGIC


def test_route_llm_low_confidence_falls_to_other():
    # LLM 低置信（<0.5）不当作权威路由决策（评审 #4）
    llm = FakeLLM(['{"scenario": "latency_spike", "confidence": 0.05}'])
    r = route_scenario(incident_text="x", anomalies=[], llm=llm)
    assert r.scenario == ScenarioType.OTHER
    assert r.source == "other"
    assert r.confidence == 0.1


def test_route_llm_returns_other_clamps_confidence():
    # LLM 返回 OTHER + 高置信 → 钳制到 0.1（与确定性 other 出口一致，评审 #13）
    llm = FakeLLM(['{"scenario": "other", "confidence": 0.9}'])
    r = route_scenario(incident_text="x", anomalies=[], llm=llm)
    assert r.scenario == ScenarioType.OTHER
    assert r.confidence == 0.1


def test_route_llm_extra_fields_tolerated():
    # LLM 附带 reason 等多余字段不被拒绝（评审 #14）
    llm = FakeLLM(['{"scenario": "latency_spike", "confidence": 0.8, "reason": "看起来是延迟问题"}'])
    r = route_scenario(incident_text="x", anomalies=[], llm=llm)
    assert r.scenario == ScenarioType.LATENCY_SPIKE
    assert r.source == "llm"


def test_route_metric_priority_does_not_call_llm():
    # 指标证据可路由时，LLM 完全不参与（确定性/省成本，评审 #20）
    llm = FakeLLM(['{"scenario": "other", "confidence": 0.1}'])
    r = route_scenario(incident_text="x", anomalies=[_anomaly("error_rate")], llm=llm)
    assert r.scenario == ScenarioType.ERROR_RATE_SPIKE
    assert r.source == "metric"
    assert len(llm.calls) == 0  # LLM 未被调用


class ThrowingLLM:
    """complete 直接抛异常（模拟生产网络/超时，评审 #19）。"""

    def __init__(self):
        self.calls = 0

    def complete(self, messages: list[dict[str, str]], temperature: float = 0.0) -> str:
        self.calls += 1
        raise TimeoutError("模拟 DeepSeek 超时")


def test_route_llm_exception_falls_to_other():
    # LLM 兜底自身抛异常不炸掉路由（评审 #19）
    llm = ThrowingLLM()
    r = route_scenario(incident_text="x", anomalies=[], llm=llm)
    assert r.scenario == ScenarioType.OTHER
    assert r.source == "other"
    assert llm.calls == 1


def test_business_context_model():
    bc = BusinessContext(entity="车门", symptom="打不开", action="开门", confidence=1.0, source="rule")
    assert bc.is_present
    assert BusinessContext().is_present is False
