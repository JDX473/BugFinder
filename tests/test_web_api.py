"""Web API 测试（FastAPI TestClient）：事件列表、触发调查、报告详情、错误处理。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.web.api import app


@pytest.fixture
def client():
    return TestClient(app)


class TestIncidents:
    def test_list_incidents(self, client):
        r = client.get("/api/incidents")
        assert r.status_code == 200
        incidents = r.json()["incidents"]
        assert len(incidents) >= 2
        assert incidents[0]["incident_id"]
        assert incidents[0]["title"]
        assert incidents[0]["service"]

    def test_incident_fields(self, client):
        r = client.get("/api/incidents")
        inc = r.json()["incidents"][0]
        assert {"incident_id", "title", "severity", "service", "triggered_at", "desc"} <= set(inc.keys())


class TestInvestigate:
    def test_investigate_produces_report(self, client):
        r = client.post("/api/incidents/INC-mock-0001/investigate")
        assert r.status_code == 200
        d = r.json()
        assert d["report_id"]
        assert d["incident_id"] == "INC-mock-0001"
        assert d["status"] == "completed"
        assert d["n_candidates"] >= 1

    def test_investigate_business_scenario(self, client):
        r = client.post("/api/incidents/INC-mock-0002/investigate")
        assert r.status_code == 200
        d = r.json()
        assert d["scenario"] == "business_logic"

    def test_unknown_incident_404(self, client):
        r = client.post("/api/incidents/NO-SUCH/investigate")
        assert r.status_code == 404
        assert "不存在" in r.json()["detail"]


class TestReportDetail:
    def test_get_report(self, client):
        # 先触发
        rid = client.post("/api/incidents/INC-mock-0001/investigate").json()["report_id"]
        r = client.get(f"/api/reports/{rid}")
        assert r.status_code == 200
        d = r.json()
        assert d["report_id"] == rid
        assert d["scenario"] == "error_rate_spike"
        assert d["root_cause_candidates"]
        assert d["evidence_list"]
        assert d["timeline"]
        assert d["audit_trail"]

    def test_metric_evidence_not_failed(self, client):
        """回归：ev-metric 必须是正常证据（WorkflowState 缺字段曾导致指标被丢弃）。"""
        rid = client.post("/api/incidents/INC-mock-0001/investigate").json()["report_id"]
        d = client.get(f"/api/reports/{rid}").json()
        metric_evs = [e for e in d["evidence_list"] if e["evidence_id"] == "ev-metric"]
        assert metric_evs
        assert metric_evs[0]["error"] is False
        assert "异常" in metric_evs[0]["summary"]

    def test_unknown_report_404(self, client):
        r = client.get("/api/reports/NOPE")
        assert r.status_code == 404

    def test_list_reports(self, client):
        client.post("/api/incidents/INC-mock-0001/investigate")
        r = client.get("/api/reports")
        assert r.status_code == 200
        assert r.json()["reports"]


class TestHome:
    def test_home_page(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "RCA Agent 报告台" in r.text
