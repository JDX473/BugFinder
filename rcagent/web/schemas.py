"""Web API 契约(pydantic 模型,FastAPI 自动生成 /docs)。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class StartRunRequest(BaseModel):
    job_id: str | None = Field(None, description="实例 ID(评估/回放模式);留空 = 实时排查模式(im 环境)")
    anomaly: str | None = Field(None, description="异常描述: 实时模式必填,评估模式留空用案例默认")
    detect_time: str | None = Field(None, description="检测时刻(ISO);留空: 评估模式用案例,实时模式用当前时刻")
    variant: str = Field("full", description="full|react|no_experts|no_jsonregen|no_obsk|no_obs_head")
    decode: str = Field("greedy", description="greedy|sampling")
    mock: bool = Field(True, description="mock 模式无需 API key(仅评估模式支持)")
    env: str = Field("demo", description="demo(合成)| im(QuantumLink IM 真实服务)")


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
