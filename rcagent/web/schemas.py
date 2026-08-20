"""Web API 契约(pydantic 模型,FastAPI 自动生成 /docs)。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class StartRunRequest(BaseModel):
    job_id: str = Field(..., description="demo job id(见 GET /api/jobs);作为数据源")
    anomaly: str | None = Field(None, description="自定义异常描述;留空用任务的默认描述")
    variant: str = Field("full", description="full|react|no_experts|no_jsonregen|no_obsk|no_obs_head")
    decode: str = Field("greedy", description="greedy|sampling")
    mock: bool = Field(True, description="mock 模式无需 API key")


class RunSummary(BaseModel):
    run_id: str
    job_id: str
    status: str
    steps: int
    started: float
    finished: float
    mode: str
    variant: str
    decode: str
    source: str


class RunSnapshot(BaseModel):
    meta: dict
    trajectory: dict | None
    events: list[dict]


class SnapshotContent(BaseModel):
    key: str
    content: str


class CancelResponse(BaseModel):
    ok: bool
