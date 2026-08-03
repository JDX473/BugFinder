"""报告 Web API（PRD §4.5 RCA-040 报告 Web 页面）。

FastAPI 提供三个端点，复用 RCAWorkflow + mock 数据源：
  - GET /api/incidents          → 可用事件列表（mock 数据源预设的故障样例）
  - POST /api/incidents/{id}/investigate → 触发一次调查并产出报告
  - GET  /api/reports/{report_id} → 报告详情（候选/证据/时间线/修复/审计）

设计：
  - 无状态：不落库（骨架阶段），报告存内存 dict，按 report_id 取
  - 复用 RCAWorkflow（确定性 + mock），触发即产出完整 RCAReport
  - 错误处理：未知事件/报告 → 404；调查失败 → 500 + 错误信息
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.graph.workflow import RCAWorkflow
from app.pipeline.event_normalizer import normalize_alert_payload

app = FastAPI(title="RCA Agent 报告服务", version="0.1.0")

# 前端页面（单 HTML，无构建）
_STATIC_DIR = Path(__file__).resolve().parent / "static"
if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
    app.get("/")(lambda: HTMLResponse((_STATIC_DIR / "index.html").read_text(encoding="utf-8")))

# 内存报告存储（骨架阶段；接真实环境换 Postgres）
_reports: dict[str, Any] = {}

# 工作流实例（骨架阶段固定 mock + 纯规则）
_workflow = RCAWorkflow()

# mock 数据源预设的故障样例（演示用，PRD §5.1 事件输入）
_MOCK_ALERTS: list[dict] = [
    {
        "incident_id": "INC-mock-0001",
        "title": "checkout error_rate 异常 45%",
        "severity": "critical",
        "service": "checkout",
        "timestamp": "2026-08-02T21:00:00Z",
        "trace_id": "tr-mock-0001",
        "desc": "支付链路错误率飙升（gateway→checkout→payment 超时）",
    },
    {
        "incident_id": "INC-mock-0002",
        "title": "用户反馈车门打不开",
        "severity": "warning",
        "service": "car-door",
        "timestamp": "2026-08-02T21:00:00Z",
        "trace_id": "tr-mock-0003",
        "desc": "业务故障：技术信号干净，业务规则拒绝（行程未开始）",
    },
]


def _to_dict(incident: dict) -> dict:
    """事件列表项（不含内部工作流状态）。"""
    return {
        "incident_id": incident["incident_id"],
        "title": incident["title"],
        "severity": incident["severity"],
        "service": incident["service"],
        "triggered_at": incident["timestamp"],
        "desc": incident["desc"],
    }


@app.get("/api/incidents")
def list_incidents() -> dict:
    """可用事件列表（mock 预设的故障样例）。"""
    return {"incidents": [_to_dict(a) for a in _MOCK_ALERTS]}


@app.post("/api/incidents/{incident_id}/investigate")
def investigate(incident_id: str) -> dict:
    """触发一次调查，产出报告。返回报告摘要 + report_id。"""
    alert = next((a for a in _MOCK_ALERTS if a["incident_id"] == incident_id), None)
    if alert is None:
        raise HTTPException(status_code=404, detail=f"事件 {incident_id} 不存在（可用: {', '.join(a['incident_id'] for a in _MOCK_ALERTS)}）")

    # 归一化 → 工作流调查
    incident = normalize_alert_payload(alert)
    try:
        out = _workflow.invoke(incident)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"调查失败: {e}")

    report = out["report"]
    _reports[report.report_id] = report

    return {
        "report_id": report.report_id,
        "incident_id": report.incident_id,
        "scenario": report.scenario.value,
        "status": report.meta.status.value,
        "n_candidates": len(report.root_cause_candidates),
    }


@app.get("/api/reports/{report_id}")
def get_report(report_id: str) -> dict:
    """报告详情（完整 RCAReport，Pydantic 序列化）。"""
    report = _reports.get(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"报告 {report_id} 不存在（先 POST /investigate 触发）")
    return report.model_dump(mode="json")


@app.get("/api/reports")
def list_reports() -> dict:
    """已产出的报告列表（内存）。"""
    return {
        "reports": [
            {"report_id": r.report_id, "incident_id": r.incident_id, "scenario": r.scenario.value, "status": r.meta.status.value}
            for r in _reports.values()
        ]
    }
