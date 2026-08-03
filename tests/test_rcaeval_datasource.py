"""rcaeval_datasource.py 的测试：case 索引、指标读取、事件构造、真实数据评估回归。"""

from __future__ import annotations

import csv
from datetime import datetime, timezone

import pytest

from app.tools.rcaeval_datasource import RcaEvalDataError, RcaEvalMetricSource
from app.schema.models import IncidentSource, TimeRange

# 测试用迷你数据集：tmp_path 下构造 2 个 case
import os
import pathlib


@pytest.fixture
def mini_root(tmp_path) -> str:
    """构造迷你 RE1-OB 数据集：2 个 case。"""
    # adservice_cpu/1
    d = tmp_path / "RE1-OB" / "adservice_cpu" / "1"
    d.mkdir(parents=True)
    (d / "inject_time.txt").write_text("1685364577")
    with open(d / "data.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "adservice_cpu", "adservice_mem", "frontend_cpu"])
        # 前 10 秒正常，后 10 秒 CPU 飙升
        for i in range(20):
            t = 1685364577 - 10 + i
            cpu = 5.0 if i < 10 else 95.0
            w.writerow([t, cpu, 100.0, 3.0])

    # cartservice_mem/1
    d2 = tmp_path / "RE1-OB" / "cartservice_mem" / "1"
    d2.mkdir(parents=True)
    (d2 / "inject_time.txt").write_text("1685365000")
    with open(d2 / "data.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "cartservice_mem", "cartservice_cpu"])
        for i in range(20):
            t = 1685365000 - 10 + i
            mem = 50.0 if i < 10 else 400.0
            w.writerow([t, mem, 4.0])

    return str(tmp_path)


# ---------------------------------------------------------------- 索引

class TestIndex:
    def test_indexes_nested_cases(self, mini_root):
        """递归索引嵌套目录（RE1-OB/RE1-OB/ 结构）。"""
        src = RcaEvalMetricSource(mini_root)
        cases = src.list_cases()
        assert len(cases) == 2
        assert "adservice_cpu/1" in cases
        assert "cartservice_mem/1" in cases

    def test_case_metadata(self, mini_root):
        src = RcaEvalMetricSource(mini_root)
        c = src.case("adservice_cpu/1")
        assert c.service == "adservice"
        assert c.fault == "cpu"
        assert c.instance == 1
        assert c.ground_truth == "adservice_cpu"
        assert c.inject_time.tzinfo is not None  # aware UTC

    def test_missing_case_raises(self, mini_root):
        src = RcaEvalMetricSource(mini_root)
        with pytest.raises(RcaEvalDataError):
            src.case("nonexistent/1")

    def test_missing_root_raises(self, tmp_path):
        with pytest.raises(RcaEvalDataError):
            RcaEvalMetricSource(str(tmp_path / "no-such-dir"))


# ---------------------------------------------------------------- 查询

class TestQuery:
    def test_metric_series(self, mini_root):
        src = RcaEvalMetricSource(mini_root)
        s = src.metric_series("adservice_cpu/1", "adservice_cpu")
        assert len(s.points) == 20
        assert max(p.value for p in s.points) == 95.0  # 故障段
        assert s.metric == "adservice_cpu"

    def test_query_metric_with_case_label(self, mini_root):
        src = RcaEvalMetricSource(mini_root)
        # 协议实现：labels['case'] 定位 case
        tr = TimeRange(
            start=datetime(2023, 5, 29, 12, 49, 27, tzinfo=timezone.utc),
            end=datetime(2023, 5, 29, 12, 50, 27, tzinfo=timezone.utc),
        )
        s = src.query_metric("adservice_cpu", tr, labels={"case": "adservice_cpu/1"})
        assert len(s.points) == 20

    def test_query_metric_no_case_returns_empty(self, mini_root):
        src = RcaEvalMetricSource(mini_root)
        tr = TimeRange(
            start=datetime(2023, 5, 29, 12, 49, 27, tzinfo=timezone.utc),
            end=datetime(2023, 5, 29, 12, 50, 27, tzinfo=timezone.utc),
        )
        s = src.query_metric("adservice_cpu", tr)  # 无 case label
        assert s.points == []

    def test_anomaly_series_all_columns(self, mini_root):
        src = RcaEvalMetricSource(mini_root)
        series = src.anomaly_series("adservice_cpu/1")
        cols = {s.metric for s in series}
        assert cols == {"adservice_cpu", "adservice_mem", "frontend_cpu"}


# ---------------------------------------------------------------- 事件构造

class TestIncident:
    def test_incident_construction(self, mini_root):
        src = RcaEvalMetricSource(mini_root)
        inc = src.incident_for("adservice_cpu/1")
        assert inc.source == IncidentSource.ALERT_WEBHOOK
        assert inc.alert.labels["service"] == "adservice"
        assert inc.alert.labels["metric"] == "adservice_cpu"
        assert "cpu" in inc.alert.title
        # 时间窗覆盖注入时刻
        assert inc.alert.starts_at == datetime(2023, 5, 29, 12, 49, 37, tzinfo=timezone.utc)
