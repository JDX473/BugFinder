"""event_normalizer.py 的测试：脏载荷归一化、traceId 提取、去重、时间解析。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.pipeline.event_normalizer import (
    AlertDedupStore,
    EventNormalizeError,
    dedup_key,
    normalize_alert_payload,
)
from app.schema.models import IncidentSource, Severity

UTC = timezone.utc


def test_normalize_dirty_payload():
    # 字段名混乱、无时区、severity 中文
    payload = {
        "message": "【紧急】订单模块 error_rate 45%！@oncall 快看！！",
        "severity": "严重",
        "ts": 1735884000,  # epoch 秒
        "host": "host-a",
        "metric": "order_error_rate",
    }
    ev = normalize_alert_payload(payload)
    assert ev.source == IncidentSource.ALERT_WEBHOOK
    assert ev.alert.severity == Severity.CRITICAL  # 严重 → critical
    assert ev.alert.labels["host"] == "host-a"
    assert ev.alert.labels["metric"] == "order_error_rate"
    assert ev.triggered_at.tzinfo == UTC  # epoch 时间归一化到 UTC


def test_normalize_extracts_trace_id():
    payload = {
        "message": "checkout 服务报错 traceId: abc123def456abc123def456abc123de",
        "severity": "warning",
    }
    ev = normalize_alert_payload(payload)
    # 32 位 hex 被提取
    assert ev.alert.labels.get("trace_id")


def test_normalize_severity_mapping():
    cases = [
        ("critical", Severity.CRITICAL),
        ("严重", Severity.CRITICAL),
        ("p0", Severity.CRITICAL),
        ("warning", Severity.WARNING),
        ("warn", Severity.WARNING),
        ("警告", Severity.WARNING),
        ("info", Severity.INFO),
        ("unknown_thing", Severity.WARNING),  # 未知 → warning
    ]
    for raw, expected in cases:
        ev = normalize_alert_payload({"message": "x", "severity": raw})
        assert ev.alert.severity == expected, f"{raw!r} should map to {expected}"


def test_normalize_missing_title_raises():
    with pytest.raises(EventNormalizeError):
        normalize_alert_payload({"severity": "warning"})  # 无 title/message


def test_time_window_fallback():
    # 无显式窗口 → 用告警时间前后 30 分钟
    payload = {"message": "x", "ts": 1735884000}
    ev = normalize_alert_payload(payload)
    assert ev.alert.starts_at is not None
    assert ev.alert.starts_at <= ev.triggered_at
    assert ev.alert.starts_at >= ev.triggered_at - timedelta(minutes=31)


def test_dedup_key_same_window():
    p1 = {"message": "error_rate high", "ts": 1735884000, "service": "checkout", "metric": "error_rate"}
    p2 = {"message": "error_rate high (repeated)", "ts": 1735884200, "service": "checkout", "metric": "error_rate"}
    e1 = normalize_alert_payload(p1)
    e2 = normalize_alert_payload(p2)
    assert dedup_key(e1) == dedup_key(e2)  # 同 service+metric+窗口 → 重复


def test_dedup_key_different_service():
    p1 = {"message": "error_rate high", "ts": 1735884000, "service": "checkout", "metric": "error_rate"}
    p2 = {"message": "error_rate high", "ts": 1735884000, "service": "payment", "metric": "error_rate"}
    assert dedup_key(normalize_alert_payload(p1)) != dedup_key(normalize_alert_payload(p2))


def test_dedup_store():
    store = AlertDedupStore(ttl_minutes=120)
    p = {"message": "error_rate high", "ts": 1735884000, "service": "checkout", "metric": "error_rate"}
    e1 = normalize_alert_payload(p)
    assert store.is_duplicate(e1) is False  # 首次 → 不重复
    assert store.is_duplicate(e1) is True  # 重复
    # 不同 metric → 新事件
    p2 = dict(p, metric="latency")
    assert store.is_duplicate(normalize_alert_payload(p2)) is False
