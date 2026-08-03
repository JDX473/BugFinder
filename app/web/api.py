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

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.graph.workflow import RCAWorkflow
from app.pipeline.event_normalizer import normalize_alert_payload
from app.schema.models import IncidentEvent, IncidentSource, ManualInput

app = FastAPI(title="RCA Agent 报告服务", version="0.1.0")

# 前端页面（单 HTML，无构建）
_STATIC_DIR = Path(__file__).resolve().parent / "static"
if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
    app.get("/")(lambda: HTMLResponse((_STATIC_DIR / "index.html").read_text(encoding="utf-8")))

# 内存报告存储（骨架阶段；接真实环境换 Postgres）
_reports: dict[str, Any] = {}

# 工作流实例：默认注入真实 DeepSeek（场景兜底/假设排序/ReAct 都用上）。
# 未配置 API key 时降级纯规则（create_deepseek_client 返回 None）。
def _build_workflow() -> RCAWorkflow:
    from app.llm.deepseek_llm import create_deepseek_client

    llm = create_deepseek_client()
    return RCAWorkflow(llm=llm)

_workflow = _build_workflow()

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


class ManualInvestigate(BaseModel):
    """手动触发调查的请求体（PRD RCA-002：自由文本/服务/traceId 发起调查）。"""

    free_text: str = Field(description="事件描述（告警标题或人工描述）")
    service: str | None = Field(default=None, description="关联服务名（过滤指标/日志）")
    trace_id: str | None = Field(default=None, description="traceId（链路重建用）")


@app.post("/api/investigate")
def investigate_manual(body: ManualInvestigate) -> dict:
    """手动触发调查（RCA-002）：给 Agent 发消息，用自由文本/服务/traceId 发起。"""
    if not body.free_text.strip():
        raise HTTPException(status_code=422, detail="free_text 不能为空")
    try:
        out = _workflow.invoke_manual(
            service=body.service, free_text=body.free_text, trace_id=body.trace_id
        )
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


@app.get("/api/investigate/stream")
async def investigate_stream(
    free_text: str,
    service: str | None = None,
    trace_id: str | None = None,
) -> Any:
    """流式调查（SSE）：实时推送每步进度，最后推送完整报告。

    前端用 fetch-stream 消费：每行 `data: {json}\n\n`。
    事件类型：step（进度）/ report（最终报告）/ error / interrupt。
    """
    if not free_text.strip():
        raise HTTPException(status_code=422, detail="free_text 不能为空")

    def gen():
        try:
            for event in _workflow.stream(
                _incident_from_manual(free_text, service, trace_id)
            ):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _incident_from_manual(free_text: str, service: str | None, trace_id: str | None) -> IncidentEvent:
    """构造手动触发的事件（RCA-002）。"""
    return IncidentEvent(
        incident_id=f"INC-manual-{int(datetime.now(timezone.utc).timestamp())}",
        source=IncidentSource.MANUAL,
        triggered_at=datetime.now(timezone.utc),
        manual_input=ManualInput(trace_id=trace_id, service=service, free_text=free_text),
    )


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
