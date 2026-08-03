"""Web API 测试（FastAPI TestClient）：事件列表、触发调查、报告详情、错误处理、流式调查。"""

from __future__ import annotations

import json

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


class TestManualInvestigate:
    def test_manual_text(self, client):
        """给 Agent 发消息：自由文本触发调查（RCA-002）。"""
        r = client.post("/api/investigate", json={"free_text": "用户反馈支付失败", "service": "checkout"})
        assert r.status_code == 200
        d = r.json()
        assert d["report_id"]
        assert d["status"] == "completed"
        assert d["scenario"] == "error_rate_spike"

    def test_manual_business_text(self, client):
        """业务文本 → business_logic + 业务上下文。"""
        r = client.post("/api/investigate", json={"free_text": "用户反馈车门打不开", "service": "car-door"})
        d = r.json()
        assert d["scenario"] == "business_logic"
        # 报告里业务上下文正确
        rid = d["report_id"]
        rep = client.get(f"/api/reports/{rid}").json()
        assert rep["business_context"]["entity"] == "车门"

    def test_manual_empty_text_422(self, client):
        r = client.post("/api/investigate", json={"free_text": "  "})
        assert r.status_code == 422

    def test_manual_no_service(self, client):
        """无 service 也能调查（降级：无指标过滤）。"""
        r = client.post("/api/investigate", json={"free_text": "订单很慢"})
        assert r.status_code == 200
        assert r.json()["report_id"]


class TestStreaming:
    def test_stream_emits_progress_then_report(self, client):
        """流式调查：先逐步事件，最后报告。"""
        with client.stream("GET", "/api/investigate/stream?free_text=用户反馈支付失败&service=checkout") as r:
            assert r.status_code == 200
            assert "text/event-stream" in r.headers["content-type"]
            body = "".join(r.iter_text())
        events = [json.loads(l[6:]) for l in body.split("\n\n") if l.startswith("data: ")]
        # 至少 7 步 + 报告
        steps = [e for e in events if e["type"] == "step"]
        reports = [e for e in events if e["type"] == "report"]
        assert len(steps) >= 7
        assert len(reports) == 1
        assert reports[0]["report"]["scenario"] == "error_rate_spike"
        assert reports[0]["report"]["root_cause_candidates"]

    def test_stream_steps_in_order(self, client):
        """步骤按 1..7 顺序发出。"""
        with client.stream("GET", "/api/investigate/stream?free_text=订单很慢") as r:
            body = "".join(r.iter_text())
        events = [json.loads(l[6:]) for l in body.split("\n\n") if l.startswith("data: ")]
        steps = [e["step"] for e in events if e["type"] == "step"]
        assert steps == sorted(steps)
        assert steps[0] == 1 and steps[-1] == 7

    def test_stream_empty_text_422(self, client):
        r = client.get("/api/investigate/stream?free_text=")
        assert r.status_code == 422


class TestHome:
    def test_home_page(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "RCA Agent 报告台" in r.text
        assert "给 Agent 发消息" in r.text  # 手动触发入口存在
