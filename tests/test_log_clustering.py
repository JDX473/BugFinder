"""log_clustering.py 的测试：规则过滤、模板聚类、簇摘要、合并截断。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.pipeline.log_clustering import (
    LogCluster,
    LogClusteringError,
    LogNoiseFilter,
    _extract_exception_type,
    cluster_logs,
    normalize_template,
)
from app.schema.models import LogRecord

T0 = datetime(2026, 8, 2, 21, 0, 0, tzinfo=timezone.utc)


def _log(offset_sec: int, message: str, *, level="error", service="checkout", host="host-a", trace_id=None, exception=None) -> LogRecord:
    return LogRecord(
        timestamp=T0 + timedelta(seconds=offset_sec),
        service=service,
        host=host,
        message=message,
        level=level,
        trace_id=trace_id,
        exception=exception,
    )


def test_noise_filter_marker_and_level():
    f = LogNoiseFilter()
    assert f.is_noise(_log(0, "heartbeat received ok", level="info")) is True  # 命中黑名单
    assert f.is_noise(_log(1, "something normal happened", level="info")) is True  # info 低于 warn 门槛
    assert f.is_noise(_log(2, "some error happened", level="error")) is False  # 高等级非噪音 → 保留


def test_noise_filter_min_level_param():
    f = LogNoiseFilter(min_level="info")
    assert f.is_noise(_log(0, "something normal happened", level="info")) is False


def test_noise_filter_empty_markers_raises():
    with pytest.raises(LogClusteringError):
        LogNoiseFilter(noise_markers=())


def test_normalize_template_numbers():
    # 数字被占位，稳定段保留
    assert normalize_template("ERROR payment timeout after 15000 ms, retry 3 times") == "ERROR payment timeout after {num} ms, retry {num} times"
    # 十六进制、千分位、小数、冒号时间都被归一
    assert normalize_template("alloc 0x1f4 bytes, cost 1,234.56 ms at 21:00:03") == "alloc {num} bytes, cost {num} ms at {num}"


def test_normalize_template_key_value():
    # id 类键值整体占位并保留键名；冒号后缀的键值也被归一
    assert normalize_template("order id=12345 failed") == "order {id} failed"
    assert normalize_template("requestId: abc-def-123 paid") == "{requestId} paid"


def test_normalize_template_ip_and_whitespace():
    # IP[:port] 占位；多余空白压缩为单空格
    assert normalize_template("connect  10.1.2.3:8080  failed") == "connect {ip} failed"
    assert normalize_template("  two   spaces  ") == "two spaces"


def test_cluster_same_template():
    # 同一模板、不同变量值 → 归为一个簇，变量归一化正确
    logs = [
        _log(0, "ERROR payment timeout after 15000 ms"),
        _log(1, "ERROR payment timeout after 30000 ms"),
    ]
    r = cluster_logs(logs)
    assert r.clustered_count == 2
    assert r.noise_count == 0
    assert len(r.clusters) == 1
    c = r.clusters[0]
    assert c.count == 2
    assert "ERROR payment timeout after {num} ms" == c.template


def test_cluster_splits_different_templates():
    logs = [
        _log(0, "ERROR payment timeout after 15000 ms"),
        _log(1, "INFO checkout started", level="info"),
    ]
    # info 低于 warn 门槛 → 被过滤掉
    r = cluster_logs(logs)
    assert r.clustered_count == 1
    assert len(r.clusters) == 1
    assert "INFO checkout started" not in r.clusters[0].template


def test_noise_stats():
    logs = [
        _log(0, "heartbeat ok", level="info"),  # 噪音
        _log(1, "ERROR payment timeout after 15000 ms"),
    ]
    r = cluster_logs(logs)
    assert r.total_logs == 2
    assert r.noise_count == 1
    assert r.clustered_count == 1
    assert len(r.clusters) == 1


def test_cluster_metadata():
    # 计数、错误占比、服务去重、时间范围、异常类型
    logs = [
        _log(0, "ERROR payment timeout after 15000 ms", level="error", exception="TimeoutException", service="checkout"),
        _log(1, "ERROR payment timeout after 30000 ms", level="error", exception="TimeoutException", service="checkout"),
        _log(2, "WARN retry payment after 5000 ms", level="warn", service="payment"),
    ]
    r = cluster_logs(logs)
    assert len(r.clusters) == 2
    timeout_cluster = [c for c in r.clusters if "timeout" in c.template][0]
    assert timeout_cluster.count == 2
    assert timeout_cluster.exception_type == "TimeoutException"
    assert timeout_cluster.error_ratio == 1.0
    assert timeout_cluster.services == ["checkout"]
    assert timeout_cluster.first_timestamp == T0
    assert timeout_cluster.last_timestamp == T0 + timedelta(seconds=1)
    assert timeout_cluster.level == "error"
    retry_cluster = [c for c in r.clusters if "retry" in c.template][0]
    assert retry_cluster.error_ratio == 0.0  # warn 不算 error


def test_cluster_sorted_by_count():
    logs = [
        _log(0, "ERROR timeout after 15000 ms"),
        _log(1, "ERROR timeout after 20000 ms"),
        _log(2, "WARN slow response 1000 ms"),
        _log(3, "WARN slow response 2000 ms"),
        _log(4, "WARN slow response 3000 ms"),
    ]
    r = cluster_logs(logs)
    assert len(r.clusters) == 2
    assert r.clusters[0].count == 3  # 高频在前
    assert "slow response" in r.clusters[0].template


def test_max_representatives_truncated():
    logs = [_log(i, f"ERROR timeout after {i * 100} ms") for i in range(10)]
    r = cluster_logs(logs, max_representatives=2)
    assert r.clusters[0].count == 10
    assert len(r.clusters[0].representatives) == 2


def test_max_clusters_merged_into_other():
    # 10 条互不相同的消息（稳定词不同 → 10 个模板），合并后 2 高频 + 1 聚合
    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india", "juliet"]
    logs = [_log(i, f"ERROR unique msg {words[i]}") for i in range(10)]
    r = cluster_logs(logs, max_clusters=3)
    assert len(r.clusters) == 3
    assert r.clusters[-1].template.startswith("other")
    assert r.clusters[-1].count == 8  # 低频 8 个合并且计数正确
    # 总计数守恒
    assert sum(c.count for c in r.clusters) == 10


def test_empty_input():
    r = cluster_logs([])
    assert r.total_logs == 0
    assert r.clusters == []


def test_extract_exception_type():
    assert _extract_exception_type(_log(0, "x", exception="java.util.concurrent.TimeoutException")) == "TimeoutException"
    assert _extract_exception_type(_log(0, "TimeoutException: read timed out")) == "TimeoutException"
    assert _extract_exception_type(_log(0, "some generic message")) is None


def test_cluster_summary():
    logs = [
        _log(0, "ERROR payment timeout after 15000 ms", exception="TimeoutException"),
        _log(1, "ERROR payment timeout after 30000 ms", exception="TimeoutException"),
    ]
    r = cluster_logs(logs)
    summary = r.clusters[0].to_summary()
    assert "timeout" in summary
    assert "计数 2" in summary
    assert "TimeoutException" in summary
    assert "错误占比 100%" in summary


def test_result_summary():
    logs = [_log(0, "heartbeat ok", level="info"), _log(1, "ERROR timeout after 15000 ms")]
    r = cluster_logs(logs)
    s = r.to_summary()
    assert "噪音过滤 1 条" in s
    assert "有效 1 条" in s
