"""RCA Agent 统一数据模型（PRD §8 的唯一权威实现）。

所有组件共享这里的数据契约：事件接收、证据管理、报告生成、评测打分
都读写这些模型。字段定义改一处、全局生效，杜绝同名不同义。

时间约定：全项目统一使用 `UTCDateTime`——naive 时间视为 UTC，aware 时间归一化到 UTC。
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)


def _ensure_utc(v: datetime) -> datetime:
    """把 naive 时间当作 UTC；aware 时间归一化到 UTC。"""
    if v.tzinfo is None:
        return v.replace(tzinfo=timezone.utc)
    return v.astimezone(timezone.utc)


UTCDateTime = Annotated[datetime, AfterValidator(_ensure_utc)]


class BaseSchema(BaseModel):
    """统一基类：字符串去空白；未知字段忽略（宽容解析外部数据）。"""

    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")


# ---------------------------------------------------------------- 枚举

class IncidentSource(StrEnum):
    ALERT_WEBHOOK = "alert_webhook"
    MANUAL = "manual"


class Severity(StrEnum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


class EvidenceType(StrEnum):
    LOG = "log"
    METRIC = "metric"
    TRACE = "trace"
    SCENARIO = "scenario"
    INFRA = "infra"
    HISTORY = "history"


class ScenarioType(StrEnum):
    """PRD §6.2 步骤 2 的场景枚举。桶 = 可执行的 SOP 剧本。"""

    LATENCY_SPIKE = "latency_spike"
    ERROR_RATE_SPIKE = "error_rate_spike"
    RESOURCE_SATURATION = "resource_saturation"
    AVAILABILITY_DROP = "availability_drop"
    OTHER = "other"


class ConfidenceLevel(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ReconstructionConfidence(StrEnum):
    STRONG = "strong"
    WEAK = "weak"


class TimelineSignificance(StrEnum):
    CAUSE = "cause"
    SYMPTOM = "symptom"
    CONTEXT = "context"


class RemediationPriority(StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"


class ReportStatus(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class RpcDirection(StrEnum):
    """RPC 调用方向（PRD §5.1 日志埋点，链路重建依赖）。"""

    OUT = "out"  # 本服务发起调用
    IN = "in"  # 本服务接收请求


# ---------------------------------------------------------------- 时间与公共小件

class TimeRange(BaseSchema):
    """时间窗口。start <= end 由 validator 保证。"""

    start: UTCDateTime
    end: UTCDateTime

    @field_validator("end")
    @classmethod
    def _check_order(cls, v: datetime, info: Any) -> datetime:
        start = info.data.get("start")
        if start is not None and v < start:
            raise ValueError(f"end({v}) 早于 start({start})")
        return v


def confidence_level_for(confidence: float) -> ConfidenceLevel:
    """PRD §4.4 RCA-032 置信度分档：>=0.8 high，0.5~0.8 medium，<0.5 low。"""
    if confidence >= 0.8:
        return ConfidenceLevel.HIGH
    if confidence >= 0.5:
        return ConfidenceLevel.MEDIUM
    return ConfidenceLevel.LOW


# ---------------------------------------------------------------- 数据信号模型

class LogRecord(BaseSchema):
    """日志行（PRD §5.1 字段约定）。`@timestamp` 映射为 `timestamp`。"""

    timestamp: UTCDateTime
    service: str
    host: str
    message: str
    trace_id: str | None = None
    level: str = "info"
    rpc_direction: RpcDirection | None = None
    rpc_target: str | None = None
    exception: str | None = None


class MetricPoint(BaseSchema):
    ts: UTCDateTime
    value: float


class MetricSeries(BaseSchema):
    metric: str
    labels: dict[str, str] = Field(default_factory=dict)
    points: list[MetricPoint] = Field(default_factory=list)


# ---------------------------------------------------------------- 事件输入（PRD §8.1）

class AlertInfo(BaseSchema):
    """告警载荷（来自 webhook，RCA-004 事件解析的目标）。"""

    title: str
    severity: Severity = Severity.WARNING
    labels: dict[str, str] = Field(default_factory=dict)
    starts_at: UTCDateTime | None = None
    annotations: str | None = None


class ManualInput(BaseSchema):
    """手动触发/补录输入（RCA-002）。"""

    trace_id: str | None = None
    service: str | None = None
    time_window: TimeRange | None = None
    free_text: str | None = None


class IncidentEvent(BaseSchema):
    """一次故障事件（统一封装输入）。PRD §8.1。"""

    incident_id: str
    source: IncidentSource
    triggered_at: UTCDateTime
    alert: AlertInfo | None = None
    manual_input: ManualInput | None = None

    @field_validator("incident_id")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        if not v:
            raise ValueError("incident_id 不能为空")
        return v

    @model_validator(mode="after")
    def _check_trigger(self) -> IncidentEvent:
        if self.source == IncidentSource.ALERT_WEBHOOK and self.alert is None:
            raise ValueError("source=alert_webhook 必须携带 alert")
        if self.source == IncidentSource.MANUAL and self.manual_input is None:
            raise ValueError("source=manual 必须携带 manual_input")
        return self


# ---------------------------------------------------------------- 证据（PRD §8.2）

class Evidence(BaseSchema):
    """统一证据模型。调查每一步的产物都压成一条 Evidence 写入共享状态。"""

    evidence_id: str
    type: EvidenceType
    source: str
    summary: str
    time_range: TimeRange | None = None
    payload: dict[str, Any] | None = None
    snippet: str | None = None  # 原文快照/证据内容
    error: bool = False  # True = 失败占位证据，报告如实标注"该信号缺失/失败"
    confidence: float | None = Field(default=None, ge=0, le=1)


# ---------------------------------------------------------------- 链路重建（PRD §5.3）

class TraceHop(BaseSchema):
    """调用链的一跳：source_service 调用 target_service。"""

    source_service: str
    target_service: str
    start_time: UTCDateTime
    end_time: UTCDateTime
    duration_ms: float
    has_error: bool = False
    error_summary: str | None = None


class TraceGraph(BaseSchema):
    """traceId 重建的调用链（trace_reconstruction 的确定性输出）。"""

    trace_id: str
    hops: list[TraceHop] = Field(default_factory=list)
    services: list[str] = Field(default_factory=list)  # 该 trace 出现过的全部服务
    reconstruction_confidence: ReconstructionConfidence = ReconstructionConfidence.WEAK
    coverage_note: str | None = None  # 覆盖范围/埋点完整性说明


# ---------------------------------------------------------------- 报告输出（PRD §8.3）

class RootCauseCandidate(BaseSchema):
    """根因候选。rank 1 为最可能。"""

    rank: int = Field(ge=1)
    hypothesis: str
    confidence: float = Field(ge=0, le=1)
    supporting_evidence: list[str] = Field(default_factory=list)
    refuting_evidence: list[str] = Field(default_factory=list)
    reasoning: str = Field(default="", max_length=500)
    reconstruction_confidence: ReconstructionConfidence = ReconstructionConfidence.WEAK

    @computed_field  # type: ignore[prop-decorator]
    @property
    def confidence_level(self) -> ConfidenceLevel:
        """由 confidence 自动推导（PRD §4.4），禁止手工传，杜绝不一致。"""
        return confidence_level_for(self.confidence)


class TimelineEvent(BaseSchema):
    at: UTCDateTime
    event: str
    evidence_id: str | None = None
    significance: TimelineSignificance = TimelineSignificance.CONTEXT


class RemediationSuggestion(BaseSchema):
    priority: RemediationPriority = RemediationPriority.P1
    action: str
    rationale: str = ""  # 关联的假设/证据说明


class AuditEntry(BaseSchema):
    """单步执行记录（RCA-060/061）。query 写入前须脱敏。"""

    step: str
    tool: str
    query: str = ""
    hits: int = 0
    at: UTCDateTime
    llm_summary: str | None = None


class ReportMeta(BaseSchema):
    total_token_cost: int = 0
    duration_sec: int = 0
    status: ReportStatus = ReportStatus.PARTIAL
    human_feedback: dict[str, Any] | None = None


class RCAReport(BaseSchema):
    """结构化根因报告（PRD §8.3）。校验规则见 §8.4。"""

    report_id: str
    incident_id: str
    created_at: UTCDateTime
    scenario: ScenarioType = ScenarioType.OTHER
    # evidence_list 必须先于 root_cause_candidates 校验，
    # 候选的 supporting/refuting 引用一致性校验依赖 evidence_list 已就绪。
    evidence_list: list[Evidence] = Field(default_factory=list)
    root_cause_candidates: list[RootCauseCandidate] = Field(default_factory=list)
    timeline: list[TimelineEvent] = Field(default_factory=list)
    remediation_suggestions: list[RemediationSuggestion] = Field(default_factory=list)
    audit_trail: list[AuditEntry] = Field(default_factory=list)
    meta: ReportMeta = Field(default_factory=ReportMeta)

    # ---- PRD §8.4 校验规则 ----
    @field_validator("root_cause_candidates")
    @classmethod
    def _max_3_candidates(cls, v: list[RootCauseCandidate]) -> list[RootCauseCandidate]:
        if len(v) > 3:
            raise ValueError("根因候选最多 3 个")
        return v

    @field_validator("root_cause_candidates")
    @classmethod
    def _candidates_need_support(cls, v: list[RootCauseCandidate]) -> list[RootCauseCandidate]:
        for c in v:
            if not c.supporting_evidence:
                raise ValueError(f"候选 rank={c.rank} 缺少 supporting_evidence")
        return v

    @field_validator("root_cause_candidates")
    @classmethod
    def _evidence_ids_consistent(cls, v: list[RootCauseCandidate], info: Any) -> list[RootCauseCandidate]:
        known = {ev.evidence_id for ev in info.data.get("evidence_list", [])}
        for c in v:
            for eid in [*c.supporting_evidence, *c.refuting_evidence]:
                if eid not in known:
                    raise ValueError(f"候选 rank={c.rank} 引用了未知证据 {eid}")
        return v
