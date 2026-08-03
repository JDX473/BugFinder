"""假设生成 / 打分（PRD §6.2 步骤 6、§2.2"LLM 直出终审结论"约束的落地）。

职责：基于已采集的 evidence + scenario + trace_graph，产出 **Top-3 候选根因**，
每个候选带置信度 + 支持证据 + 反驳证据 + 推理链。

架构原则（与全项目一致）：**确定性优先，LLM 次之**。
  - 规则负责"事实面"：从 trace 慢/错跳、指标异常、日志错误簇里生成**有证据支撑的
    假设**，并对每条假设做确定性打分——证据基础分 + 时间先验 + 跨信号一致性。
  - LLM 只做"排序/择优"：可选地对候选假设做 pairwise 排名（ask_json 强约束），
    规则按 LLM 排名重排并给总分；LLM 失败/未提供 → 纯规则确定性兜底。
  - **LLM 不生成根因文本**（生成假设 = 无界自由发挥，极易幻觉）；它只在规则假设
    集内做排序，且排序结果受规则分约束（低分假设不被 LLM 抬进 Top-3）。

确定性打分（hypothesis 分数 = 证据基础分 × 时间一致性系数 + 信号一致性奖励）：
  - 证据基础分：支持证据数量与类型加权（trace 慢/错跳 > 指标异常 > 日志错误簇）。
  - 时间先验：原因时间 ≤ 症状时间；假设引用的最早时间晚于事件起点会扣分。
  - 跨信号一致性：同一假设能同时用 trace 证据 + 指标证据 → 加分（多信号互证）。
  - 反驳证据扣分：存在证据与假设矛盾 → 直接按矛盾证据数下调置信度。

本模块纯逻辑，不直接依赖具体 LLM 实现——LLMClient 可注入（生产 DeepSeek /
测试 FakeLLM），`llm=None` 即纯规则确定性模式。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.llm.ask_json import ask_json
from app.llm.protocol import LLMClient
from app.pipeline.anomaly_detection import MetricAnomaly
from app.pipeline.scenario_router import ScenarioResult
from app.schema.models import (
    Evidence,
    EvidenceType,
    ReconstructionConfidence,
    RootCauseCandidate,
    ScenarioType,
    TraceGraph,
)

# LLM 排名结果被当作权威依据的置信度门槛：低于此值视为"排不出来"，按规则分兜底。
# 与场景路由的 _LLM_MIN_CONFIDENCE 同构——LLM"随口排"不该改写规则分排序。
_LLM_MIN_CONFIDENCE = 0.5

# 场景 → 主假设模板（假设文本留空由具体证据填充；文案是给报告/人看的）。
# 每个场景的假设都要能从证据里找到支撑（先打假设，再按证据过滤）。
_SCENARIO_HYPOTHESIS = {
    ScenarioType.LATENCY_SPIKE: "下游 {svc} 响应变慢导致延迟上升",
    ScenarioType.ERROR_RATE_SPIKE: "{svc} 调用下游失败导致错误率上升",
    ScenarioType.RESOURCE_SATURATION: "{svc} 资源（CPU/内存/连接）耗尽导致故障",
    ScenarioType.AVAILABILITY_DROP: "{svc} 可用性下降（请求失败/无响应）",
    ScenarioType.BUSINESS_LOGIC: "业务规则/状态异常导致功能不可用（{svc}）",
    ScenarioType.OTHER: "{svc} 出现无法归类的故障（需要人工介入）",
}

# 确定性打分权重（证据基础分）：trace 慢/错跳是最强事实（调用链级），
# 指标异常次之，日志错误簇再次。
_EVIDENCE_WEIGHT = {
    EvidenceType.TRACE: 3.0,
    EvidenceType.METRIC: 2.0,
    EvidenceType.LOG: 1.0,
    EvidenceType.SCENARIO: 0.5,
}
# 多信号一致性奖励上限（同假设同时有 trace+metric+log 三类证据时额外加分）
_MAX_CONSISTENCY_BONUS = 0.15
# 时间先验：事件起点之前多少分钟内的证据视为"原因侧"（越早越像因）
_REASON_LOOKBACK_MINUTES = 15
# 假设文本留空的兜底文案
_FALLBACK_HYPOTHESIS_TEXT = "未生成有证据支撑的假设"

# 解析后的证据视图：把原始 Evidence 转成假设生成/打分直接消费的结构。
@dataclass
class _EvidenceView:
    e: Evidence
    ref: str  # evidence_id 引用（候选支持证据用）
    ts: datetime | None  # 证据时间（锚点，时间先验用）
    trace_has_error: bool = False  # trace 证据：是否含错误/慢跳
    slow_edges: list[str] = field(default_factory=list)  # trace 证据：慢/错跳 "A->B"
    metric_anomalies: list[MetricAnomaly] = field(default_factory=list)  # 指标证据：异常序列
    metric_clean: bool = False  # 指标证据：是否观测到"技术信号干净"（business_logic 用）
    svc: str | None = None  # 证据涉及的负载服务（trace 边的下游 / 指标 / 日志的 service）


class HypothesisScoringError(Exception):
    """入口参数非法（空证据、白名单为空等）。"""


@dataclass
class HypothesisScoringResult:
    """假设打分结果：Top-3 候选 + 推理摘要。"""

    candidates: list[RootCauseCandidate]  # 已按 rank 排好（rank=1 最可能）
    basis: str = ""  # 打分依据摘要（审计/debug）
    used_llm: bool = False  # 是否走了 LLM 排序（False = 纯规则兜底）

    def to_summary(self) -> str:
        """一行摘要，供报告 / Evidence.summary。"""
        if not self.candidates:
            return "假设打分：无候选（未找到有证据支撑的假设）"
        head = "、".join(f"rank{i.rank} {i.hypothesis[:20]}…({i.confidence:.2f})" for i in self.candidates)
        return f"假设打分（{'LLM 排序' if self.used_llm else '规则兜底'}）：{head}"


def _ev_time(e: Evidence) -> datetime | None:
    """证据的时间锚点（时间先验用）。"""
    if e.time_range is not None:
        return e.time_range.start
    return None


def _parse_evidence(evs: list[Evidence], scenario: ScenarioResult, graph: TraceGraph | None) -> list[_EvidenceView]:
    """把 Evidence 解析成假设生成/打分直接消费的视图。"""
    views: list[_EvidenceView] = []
    for e in evs:
        if e.error:
            continue  # 失败占位证据不参与假设（报告里已如实标注缺失）
        v = _EvidenceView(e=e, ref=e.evidence_id, ts=_ev_time(e))
        if e.type == EvidenceType.TRACE and graph is not None:
            # 有 trace 证据：解析慢/错跳（用原始 graph，慢/错判断复用 trace 工具）
            from app.tools.trace_reconstruction import find_slow_or_error_hops

            findings = find_slow_or_error_hops(graph)
            v.trace_has_error = bool(findings)
            v.slow_edges = [f"{f['hop'].source_service}->{f['hop'].target_service}" for f in findings]
        elif e.type == EvidenceType.METRIC:
            payload = e.payload or {}
            anomalies = payload.get("anomalies") or []
            # 传入的异常可能是 dict 或 MetricAnomaly 对象（兼容两种调用形态）
            v.metric_anomalies = [a for a in anomalies if isinstance(a, MetricAnomaly) or getattr(a, "is_anomaly", False)]
            v.metric_clean = bool(payload.get("tech_signal_clean", False))
        elif e.type == EvidenceType.LOG:
            if graph is not None:
                # 日志证据的负载服务：取 trace 里最早出现的服务（日志往往是该服务的问题）
                if graph.services:
                    v.svc = graph.services[0]
            else:
                v.svc = None
        views.append(v)
    return views


def _build_trace_hypotheses(views: list[_EvidenceView]) -> list[dict]:
    """从 trace 慢/错跳生成假设（有调用链级事实支撑，最高优先级）。

    排序微调：**最深错误边优先**——错误发源地（target 不是任何错误边的 source）
    的边加分（如 gateway→checkout→payment 里 payment 是最深的发源地，优先于
    gateway→checkout 这条症状传播边）。错误向调用方传播，最深者即根因所在。
    """
    hyps: list[dict] = []
    seen: set[str] = set()
    edges: set[str] = set()
    for v in views:
        if v.trace_has_error:
            edges.update(v.slow_edges)

    # 错误发源地 = target 不是任何错误边的 source（无下游错误边）
    sources = {e.split("->")[0] for e in edges if "->" in e}

    for edge in sorted(edges):
        svc = edge.split("->")[1] if "->" in edge else edge
        if svc in seen:
            continue
        seen.add(svc)
        deepest = svc not in sources
        hyp = {
            "hypothesis": f"调用链 {edge} 出现慢/错（下游 {svc} 处理失败/超时），错误向调用方传播",
            "evidence": [v.ref for v in views if v.trace_has_error],
            "refuting": [],
            "priority": 3.3 if deepest else 3.0,  # 最深错误边（发源地）优先
            "earliest": min((v.ts for v in views if v.trace_has_error and v.ts), default=None),
            "svc": svc,
            "kind": "trace",
        }
        hyps.append(hyp)
    return hyps


def _build_metric_hypotheses(views: list[_EvidenceView], scenario: ScenarioResult) -> list[dict]:
    """从指标证据生成假设（绑定场景主假设模板）。

    两类来源：
      - 异常指标：有异常 → 场景主假设模板（错误率/延迟/资源/可用性）。
      - 技术信号干净（business_logic）：无异常指标，但观测到健康资源指标——
        "技术信号干净"本身就是一条可支撑假设的证据（业务规则/状态异常），
        此时用业务上下文生成业务假设。
    """
    hyps: list[dict] = []
    anomalies: list[MetricAnomaly] = []
    for v in views:
        anomalies.extend(v.metric_anomalies)

    # business_logic：技术信号干净 + 有业务上下文 → 业务假设
    if scenario.scenario == ScenarioType.BUSINESS_LOGIC:
        clean_views = [v for v in views if v.metric_clean]
        if not clean_views:
            return hyps
        bc = scenario.business_context
        refs = [v.ref for v in clean_views]
        entity = bc.entity or "业务实体"
        symptom = bc.symptom or "功能不可用"
        hyps.append({
            "hypothesis": f"{entity} {symptom}（技术信号干净，疑似业务规则/状态异常，非技术故障）",
            "evidence": refs,
            "refuting": [],
            "priority": 2.2,
            "earliest": None,
            "svc": entity,
            "kind": "business",
        })
        return hyps

    if not anomalies:
        return hyps

    # 指标证据的负载服务：优先取指标名前缀（checkout_error_rate → checkout）
    svc = None
    for v in views:
        if v.metric_anomalies:
            svc = v.svc or _svc_from_metric(v.metric_anomalies[0].metric)
            break
    if svc is None:
        svc = _svc_from_metric(anomalies[0].metric)

    template = _SCENARIO_HYPOTHESIS.get(scenario.scenario, _FALLBACK_HYPOTHESIS_TEXT)
    refs = [v.ref for v in views if v.metric_anomalies]
    # 指标证据基础分按场景给（与场景路由置信度对齐）
    priority = {
        ScenarioType.LATENCY_SPIKE: 2.5,
        ScenarioType.ERROR_RATE_SPIKE: 2.5,
        ScenarioType.RESOURCE_SATURATION: 2.0,
        ScenarioType.AVAILABILITY_DROP: 2.5,
        ScenarioType.OTHER: 1.0,
    }.get(scenario.scenario, 1.0)

    earliest = min((a.anomaly_start for a in anomalies if a.anomaly_start), default=None)
    hyp = {
        "hypothesis": template.format(svc=svc),
        "evidence": refs,
        "refuting": [],
        "priority": priority,
        "earliest": earliest,
        "svc": svc,
        "kind": "metric",
    }
    hyps.append(hyp)
    return hyps


def _svc_from_metric(metric: str) -> str:
    """从指标名猜负载服务（checkout_error_rate → checkout）；猜不出给 '该服务'。"""
    head = metric.split("_", 1)[0]
    return head if head else "该服务"


def _build_log_hypotheses(views: list[_EvidenceView]) -> list[dict]:
    """从日志错误簇生成假设。"""
    hyps: list[dict] = []
    for v in views:
        if not v.svc:
            continue
        # 该服务相关的全部日志证据引用
        refs = [x.ref for x in views if x.e.type == EvidenceType.LOG and x.svc == v.svc]
        hyp = {
            "hypothesis": f"{v.svc} 出现错误日志（{v.e.summary[:60]}）",
            "evidence": refs,
            "refuting": [],
            "priority": 1.5,
            "earliest": v.ts,
            "svc": v.svc,
            "kind": "log",
        }
        hyps.append(hyp)
    return hyps


def _score_hypothesis(
    hyp: dict,
    *,
    event_start: datetime,
    views: list[_EvidenceView],
    scenario: ScenarioResult,
) -> dict:
    """确定性打分：证据基础分 × 时间一致性 + 信号一致性奖励 - 反驳扣分。"""
    base = float(hyp["priority"])

    # ---- 时间先验：原因时间 ≤ 症状时间 ----
    time_coeff = 1.0
    earliest = hyp.get("earliest")
    if earliest is not None:
        # 证据时间不晚于事件起点 → 视为原因侧（不扣分）；越晚越像症状，扣分
        late_by = (earliest - event_start).total_seconds()
        if late_by > 0:
            time_coeff = max(0.6, 1.0 - late_by / (60 * 60))  # 最多扣 40%
    hyp["time_coeff"] = time_coeff

    # ---- 跨信号一致性：同假设的证据类型多样性 ----
    types = {v.e.type for v in views if v.ref in hyp["evidence"]}
    n_signals = len(types & {EvidenceType.TRACE, EvidenceType.METRIC, EvidenceType.LOG})
    consistency = min(_MAX_CONSISTENCY_BONUS, 0.05 * n_signals)
    hyp["consistency"] = consistency

    # ---- 反驳证据扣分 ----
    refute_penalty = 0.1 * len(hyp.get("refuting", []))

    score = base * time_coeff + consistency - refute_penalty
    # 归一化到 [0,1]：sigmod 式压缩把原始分（0.5~3.5）映射到有区分度的区间。
    # 单纯 max(0,min(1,score)) 会把 2.5 以上的分全撞到 1.0，Top-3 失去区分度。
    normalized = 1 / (1 + math.exp(-(score - 1.6)))
    hyp["score"] = round(normalized, 3)
    return hyp


def _to_candidate(hyp: dict, rank: int, *, reconstruction: ReconstructionConfidence) -> RootCauseCandidate:
    """把打分后的假设转成 RootCauseCandidate（置信度钳制到 [0,1]）。"""
    score = max(0.0, min(1.0, float(hyp["score"])))
    return RootCauseCandidate(
        rank=rank,
        hypothesis=str(hyp["hypothesis"]),
        confidence=round(score, 3),
        supporting_evidence=list(hyp.get("evidence", [])),
        refuting_evidence=list(hyp.get("refuting", [])),
        reasoning=_build_reasoning(hyp),
        reconstruction_confidence=reconstruction,
    )


def _build_reasoning(hyp: dict) -> str:
    """把打分依据压成推理链文本（≤500 字，报告里人看）。"""
    parts = []
    if hyp.get("kind") == "trace":
        parts.append("调用链存在慢/错跳")
    elif hyp.get("kind") == "metric":
        parts.append("指标异常")
    elif hyp.get("kind") == "log":
        parts.append("日志错误簇")
    if hyp.get("time_coeff", 1.0) < 1.0:
        parts.append("证据晚于事件起点，时间先验扣分")
    if hyp.get("consistency", 0.0) > 0:
        parts.append("多信号互证加分")
    if hyp.get("refuting"):
        parts.append(f"存在 {len(hyp['refuting'])} 条反驳证据")
    return "；".join(parts) if parts else "规则确定性打分"


# ---------------------------------------------------------------- LLM 排序

# LLM 排序输出：pairwise 排名（top 数组为按可能性降序的假设序号）。
# schema 不用 additionalProperties:False——DeepSeek 常附带说明字段（与场景路由一致）。
_LLM_RANK_SCHEMA = {
    "type": "object",
    "properties": {
        "top": {
            "type": "array",
            "items": {"type": "integer", "minimum": 1},
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string"},
    },
    "required": ["top", "confidence"],
}


def _llm_rank_prompt(scenario: ScenarioResult, hyps: list[dict]) -> tuple[str, str]:
    lines = []
    for i, h in enumerate(hyps, 1):
        lines.append(f"{i}. {h['hypothesis']}（规则分 {h['score']:.2f}，证据 {len(h['evidence'])} 条）")
    system = (
        "你是故障根因排序器。给定候选根因列表，按可能性从高到低输出它们的序号排列。"
        "只输出 JSON。规则分和证据条数是辅助信息，但你可以在认为规则分偏高/偏低时调整顺序。"
        "不确定时给低 confidence。"
    )
    user = (
        f"故障场景：{scenario.scenario.value}（来源 {scenario.source}，置信度 {scenario.confidence:.2f}）\n"
        f"候选假设：\n{chr(10).join(lines)}\n\n"
        "输出 JSON：top（假设序号数组，按可能性降序，必须包含全部候选序号各一次）、"
        "confidence（你对这次排序的把握，0~1）、reason（简述）。"
    )
    return system, user


def _apply_llm_rank(hyps: list[dict], top: list, confidence: float) -> list[dict]:
    """按 LLM 排名重排假设（缺失序号排末尾，保持规则分相对顺序）。

    `top` 里是 1-based 假设序号（对应 _llm_rank_prompt 里的编号）。校验序号
    在合法范围内，避免 LLM 输出越界序号导致 IndexError。
    """
    n = len(hyps)
    # 序号 → 下标（仅保留合法范围内的；LLM 可能漏排或乱排）
    pos: dict[int, int] = {}
    for p, idx in enumerate(top):
        if 1 <= idx <= n and idx not in pos:
            pos[idx] = p
    # 有合法排名的按排名排；没被排到的保持原相对顺序排在后部
    ranked = sorted(range(n), key=lambda i: (pos.get(i + 1, len(top)), -hyps[i]["score"]))
    return [hyps[i] for i in ranked]


def _recompute_scores_after_rank(hyps: list[dict]) -> list[dict]:
    """LLM 重排后按名次微调总分（名次越好分越高：rank1 保留，rank2 -0.05，rank3 -0.1）。"""
    for i, h in enumerate(hyps):
        h["score"] = round(max(0.0, h["score"] - 0.05 * i), 3)
    return hyps


# ---------------------------------------------------------------- 主入口

def _find_or_create_metric_evidence(evs: list[Evidence], scenario: ScenarioResult) -> list[Evidence]:
    """构造假设打分用的指标证据列表。

    场景路由只产出 ScenarioResult（含 raw_anomalies），不落 Evidence。若调用方
    未传指标证据，这里用 scenario.raw_anomalies 构造一条 METRIC 证据，保证
    假设生成有指标事实可用。
    """
    if any(e.type == EvidenceType.METRIC for e in evs):
        return evs
    if not scenario.raw_anomalies:
        return evs
    from app.schema.models import TimeRange

    # 用异常序列构造指标证据（供打分，不重复写报告 evidence_list）
    payload = {
        "anomalies": scenario.raw_anomalies,
        "tech_signal_clean": False,
    }
    earliest = min((a.anomaly_start for a in scenario.raw_anomalies if a.anomaly_start), default=None)
    latest = max((a.anomaly_start for a in scenario.raw_anomalies if a.anomaly_start), default=None)
    ev = Evidence(
        evidence_id="ev-metric-synth",
        type=EvidenceType.METRIC,
        source="scenario_router",
        summary=f"场景路由关联的 {len(scenario.raw_anomalies)} 个异常指标",
        time_range=TimeRange(start=earliest, end=latest) if earliest and latest else None,
        payload=payload,
    )
    return [*evs, ev]


def generate_hypotheses(
    *,
    evidence: list[Evidence],
    scenario: ScenarioResult,
    graph: TraceGraph | None = None,
    event_start: datetime | None = None,
    llm: LLMClient | None = None,
) -> HypothesisScoringResult:
    """生成并打分 Top-3 候选根因（PRD §6.2 步骤 6）。

    参数：
      evidence: 已采集的全部证据（日志/指标/trace 等，含失败占位 Evidence）
      scenario: 场景路由结果（含业务上下文与 raw_anomalies）
      graph: 可选 trace 图（trace 假设依赖）
      event_start: 事件起点（时间先验锚点；不传用场景最早异常时间）
      llm: 可选 LLMClient（生产 DeepSeek / 测试 FakeLLM）。None = 纯规则模式。

    返回：HypothesisScoringResult（Top-3 候选，rank=1 最可能）。
    """
    if not evidence and not scenario.raw_anomalies:
        return HypothesisScoringResult(candidates=[], basis="无任何证据，无法生成假设")

    # 补全指标证据（场景路由的 raw_anomalies → 合成 METRIC 证据）
    evs = _find_or_create_metric_evidence(evidence, scenario)

    views = _parse_evidence(evs, scenario, graph)

    # 生成假设：trace > metric > log（优先级即确定性从高到低）
    hyps = _build_trace_hypotheses(views)
    hyps += _build_metric_hypotheses(views, scenario)
    hyps += _build_log_hypotheses(views)
    if not hyps:
        return HypothesisScoringResult(candidates=[], basis="证据不足以生成任何假设")

    # 事件起点锚点
    if event_start is None:
        earliest = min((a.anomaly_start for a in scenario.raw_anomalies if a.anomaly_start), default=None)
        if earliest is not None:
            event_start = earliest
        else:
            event_start = datetime.now(timezone.utc)

    # 确定性打分
    for h in hyps:
        _score_hypothesis(h, event_start=event_start, views=views, scenario=scenario)

    # 去重（同 svc 同 kind 合并，保留证据并集）
    hyps = _dedup_hypotheses(hyps)

    # 按规则分排序
    hyps.sort(key=lambda h: h["score"], reverse=True)

    # 可选：LLM 排序（置信度门槛 + 失败兜底）
    used_llm = False
    if llm is not None and len(hyps) > 1:
        try:
            system, user = _llm_rank_prompt(scenario, hyps)
            fallback = lambda: {"top": [], "confidence": 0.0}
            result = ask_json(llm, system, user, _LLM_RANK_SCHEMA, fallback=fallback, temperature=0.0)
            if result.ok and result.data is not None:
                top = [int(x) for x in result.data.get("top", []) if isinstance(x, int) or (isinstance(x, (str, float)) and str(x).isdigit())]
                conf = float(result.data.get("confidence", 0.0))
                if conf >= _LLM_MIN_CONFIDENCE and top:
                    hyps = _apply_llm_rank(hyps, top, conf)
                    hyps = _recompute_scores_after_rank(hyps)
                    used_llm = True
        except Exception:
            used_llm = False  # LLM 排序失败不炸掉打分，走规则结果

    # 裁剪 Top-3
    top3 = hyps[:3]

    # 序号重排（rank=1..n）
    for i, h in enumerate(top3, 1):
        h["rank"] = i

    reconstruction = graph.reconstruction_confidence if graph else ReconstructionConfidence.WEAK
    candidates = [_to_candidate(h, h["rank"], reconstruction=reconstruction) for h in top3]

    basis = (
        f"生成 {len(hyps)} 条假设，"
        f"{'LLM 排序（置信度通过）' if used_llm else '纯规则兜底'}"
    )

    return HypothesisScoringResult(candidates=candidates, basis=basis, used_llm=used_llm)


def _dedup_hypotheses(hyps: list[dict]) -> list[dict]:
    """按 (svc, kind) 合并重复假设，证据取并集。"""
    merged: dict[tuple, dict] = {}
    for h in hyps:
        key = (h.get("svc"), h.get("kind"))
        if key in merged:
            target = merged[key]
            target["evidence"] = list(dict.fromkeys([*target["evidence"], *h["evidence"]]))
            # 保留规则分高者
            if h.get("score", 0.0) > target.get("score", 0.0):
                target["hypothesis"] = h["hypothesis"]
                target["priority"] = h["priority"]
                target["score"] = h["score"]
        else:
            merged[key] = dict(h)
    return list(merged.values())
