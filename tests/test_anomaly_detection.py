"""anomaly_detection.py 的测试：各异常形态、阈值边界、摘要生成。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.pipeline.anomaly_detection import (
    AnomalyShape,
    detect_anomaly,
    detect_anomalies,
)
from app.schema.models import MetricPoint, MetricSeries

T0 = datetime(2026, 8, 2, 21, 0, 0, tzinfo=timezone.utc)


def _series(metric: str, values: list[float], *, step_sec: int = 60) -> MetricSeries:
    pts = [MetricPoint(ts=T0 + timedelta(seconds=i * step_sec), value=v) for i, v in enumerate(values)]
    return MetricSeries(metric=metric, labels={}, points=pts)


def test_spike_up():
    # 前 30 点 ~1，后 10 点 ~100（突增 100 倍）
    s = _series("error_rate", [1.0] * 30 + [100.0] * 10)
    a = detect_anomaly(s)
    assert a.is_anomaly
    assert a.shape == AnomalyShape.SPIKE_UP
    assert a.anomaly_start is not None
    assert a.ratio > 50


def test_spike_down():
    # 40 点，前 20 点 ~100（基线），后 20 点 ~1（突降 100 倍）
    s = _series("qps", [100.0] * 20 + [1.0] * 20)
    a = detect_anomaly(s)
    assert a.is_anomaly
    assert a.shape == AnomalyShape.SPIKE_DOWN
    assert a.ratio < 0.1


def test_rise_gradual():
    # 有噪声的基线 + 整体线性抬升：3σ 抓不到，MAD 能抓到（渐变）
    import random

    random.seed(42)
    base = [10.0 + random.uniform(-0.5, 0.5) for _ in range(20)]
    gradual = [10.0 + random.uniform(-0.5, 0.5) + i * 0.5 for i in range(1, 21)]  # ~10.5 → ~20
    s = _series("latency", base + gradual)
    a = detect_anomaly(s, mad_threshold=4.0, z_threshold=5.0)  # z_threshold 拉高，逼它走 MAD 分支
    assert a.is_anomaly
    assert a.shape == AnomalyShape.RISE


def test_normal_flat():
    s = _series("cpu", [50.0] * 40 + [51.0] * 20)
    a = detect_anomaly(s)
    assert not a.is_anomaly
    assert a.shape == AnomalyShape.NORMAL


def test_too_few_points():
    s = _series("cpu", [50.0] * 4)
    a = detect_anomaly(s)
    assert not a.is_anomaly
    assert "点数不足" in a.detail


def test_baseline_flat_and_detect_deviates():
    # 基线完全平稳（std=0），检测窗口偏离 → 突增
    s = _series("cpu", [50.0] * 30 + [80.0] * 10)
    a = detect_anomaly(s)
    assert a.is_anomaly
    assert a.shape == AnomalyShape.SPIKE_UP


def test_summary_format():
    s = _series("error_rate", [1.0] * 30 + [100.0] * 10)
    a = detect_anomaly(s)
    summary = a.to_summary()
    assert "error_rate" in summary
    assert "突增" in summary
    assert "2026-08-02" in summary  # 起始时间是 ISO 日期
    assert "倍" in summary


def test_batch_sorts_by_ratio():
    s1 = _series("a", [1.0] * 30 + [100.0] * 10)  # ratio 100
    s2 = _series("b", [50.0] * 30 + [55.0] * 10)  # ratio 1.1，normal
    s3 = _series("c", [10.0] * 30 + [200.0] * 10)  # ratio 20
    result = detect_anomalies([s1, s2, s3])
    assert [a.metric for a in result] == ["a", "c"]  # 只含异常，且按 ratio 降序
