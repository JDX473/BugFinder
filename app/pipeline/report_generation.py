"""报告生成（PRD §6.2 步骤 7、§8.3 RCAReport 组装 + §8.4 校验降级）。

职责：把假设打分结果 + 全部证据 + 审计信息组装成一份完整的 `RCAReport`。

架构原则（与全项目一致）：**确定性优先，LLM 次之**。
  - 本模块**不调 LLM**（PRD §6.2 步骤 7 明确"不调 LLM 二次推理，仅拼装与措辞"）。
    LLM 的贡献已经在步骤 6（假设打分）结束；报告生成是纯确定性拼装 + 校验。
  - 证据收集（trace/metric/log/scenario）由编排层完成，本模块只负责：
      1. 强制证据引用校验（PRD §8.4：候选必有 supporting_evidence、引用必须存在）
      2. 时间线组装（时间先验 + 显著性标注）
      3. 修复建议生成（只读建议，由场景/根因映射）
      4. 审计写入（全轨迹，RCA-060）
      5. 元信息（状态/耗时/token 成本）

校验降级（PRD §8.4："字段校验失败 → 标记校验失败降级，不整份丢弃"）：
  用 `ReportValidator` 显式校验候选，把"整份丢弃"降级为"标注 partial + 降级说明"。
  这样保证：LLM 或下游产生的不完整候选不会炸掉整份报告，而是如实标注。

硬约束（PRD §12.1）：本模块不产生任何写操作，全部输出为只读报告文本。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.pipeline.hypothesis_scoring import HypothesisScoringResult
from app.pipeline.scenario_router import ScenarioResult
from app.schema.models import (
    AuditEntry,
    BusinessContext,
    Evidence,
    EvidenceType,
    RCAReport,
    RemediationPriority,
    RemediationSuggestion,
    ReportMeta,
    ReportStatus,
    RootCauseCandidate,
    ScenarioType,
    TimelineEvent,
    TimelineSignificance,
    TraceGraph,
)


class ReportValidationError(Exception):
    """报告候选校验失败（严重到无法组装）。"""


@dataclass
class ReportValidator:
    """报告候选校验器（PRD §8.4 校验规则）。

    用显式校验替代"整份丢弃"：校验失败的候选被降级/裁剪，报告如实标注 partial。
    """

    max_candidates: int = 3
    required_evidence: bool = True  # 候选必须有支持证据（PRD §8.4）
    require_valid_refs: bool = True  # 引用必须存在于 evidence_list（PRD §8.4）

    def validate(
        self,
        candidates: list[RootCauseCandidate],
        evidence_list: list[Evidence],
    ) -> tuple[list[RootCauseCandidate], list[str]]:
        """校验候选列表，返回 (可用候选, 降级说明)。

        降级策略：
          - 候选数超过上限 → 裁剪（保留 rank 最小的）
          - 缺 supporting_evidence → 整条降级丢弃（PRD：候选必须有证据）
          - 引用不存在的 evidence_id → 丢弃该候选
        所有降级都记录进 violations，报告 meta 标 partial 并附说明。
        """
        if not candidates:
            return [], ["无候选"]

        known = {e.evidence_id for e in evidence_list}
        violations: list[str] = []
        usable: list[RootCauseCandidate] = []
        for c in sorted(candidates, key=lambda c: c.rank):
            if self.required_evidence and not c.supporting_evidence:
                violations.append(f"候选 rank={c.rank} 缺 supporting_evidence，降级丢弃")
                continue
            if self.require_valid_refs:
                bad = [eid for eid in [*c.supporting_evidence, *c.refuting_evidence] if eid not in known]
                if bad:
                    violations.append(f"候选 rank={c.rank} 引用未知证据 {bad}，降级丢弃")
                    continue
            usable.append(c)
            if len(usable) >= self.max_candidates:
                if len(candidates) > len(usable):
                    violations.append(f"候选超过 {self.max_candidates} 个，裁剪保留前 {self.max_candidates} 个")
                break
        return usable, violations


def _build_timeline(
    evidence_list: list[Evidence],
    scenario: ScenarioResult,
    graph: TraceGraph | None,
    event_start: datetime,
) -> list[TimelineEvent]:
    """组装时间线（PRD §8.3 timeline + §8.4 时间先验）。

    只收编**离散事件**，不收"采集窗证据"（如整窗的指标检测摘要——那是证据清单，
    不是时间线事件）：
      - 场景路由的最早异常指标 → CAUSE（最可能的原因侧信号）
      - trace 首个错误跳 → CAUSE
      - 事件触发（告警/手动接入）→ SYMPTOM
    按时间排序输出。
    """
    events: list[TimelineEvent] = []

    # 场景证据（最早异常指标 = 最可能的原因侧信号）
    if scenario.earliest_anomaly is not None:
        at = scenario.earliest_anomaly.anomaly_start
        if at is not None:
            events.append(
                TimelineEvent(
                    at=at,
                    event=f"指标 {scenario.earliest_anomaly.metric} 异常开始（{scenario.earliest_anomaly.shape.value}）",
                    significance=TimelineSignificance.CAUSE,
                )
            )

    # trace 慢/错跳（原因侧候选，只取首个错误跳避免时间线过密）
    if graph is not None:
        for h in graph.hops:
            if h.has_error:
                events.append(
                    TimelineEvent(
                        at=h.start_time,
                        event=f"调用链 {h.source_service}->{h.target_service} 携带错误（{h.error_summary or '未知'}）",
                        significance=TimelineSignificance.CAUSE,
                    )
                )
                break

    # 事件起点（症状）
    events.append(
        TimelineEvent(at=event_start, event="事件触发（告警/手动接入）", significance=TimelineSignificance.SYMPTOM)
    )

    events.sort(key=lambda x: x.at)
    return events


def _build_remediation(
    candidates: list[RootCauseCandidate],
    scenario: ScenarioType,
    business_context: BusinessContext,
) -> list[RemediationSuggestion]:
    """生成只读修复建议（PRD §8.3 remediation_suggestions）。

    修复建议由场景与根因候选映射，全部为只读建议（重启/回滚/扩容等写操作
    不在本期范围，PRD §12.1）。场景级建议不依赖候选——business_logic 等
    场景即使无候选也给出业务剧本的处置方向。
    """
    base: list[RemediationSuggestion] = []
    # 场景级建议
    scenario_suggestions = {
        ScenarioType.RESOURCE_SATURATION: "检查资源瓶颈（CPU/内存/连接/磁盘）并扩容或优化，关注异常指标的负载源",
        ScenarioType.LATENCY_SPIKE: "检查下游依赖响应耗时，定位超时调用；确认是否需要调整超时配置或扩容",
        ScenarioType.ERROR_RATE_SPIKE: "检查错误日志与下游调用失败，优先定位传播错误的最深服务",
        ScenarioType.AVAILABILITY_DROP: "检查实例/服务可用性（探活、重启、负载均衡摘除异常节点）",
        ScenarioType.BUSINESS_LOGIC: f"检查业务规则/配置开关/数据状态（业务实体：{business_context.entity or '未知'}，症状：{business_context.symptom or '未知'}）",
        ScenarioType.OTHER: "无法确定场景，建议人工介入排查（参考完整审计轨迹与证据）",
    }
    if scenario in scenario_suggestions:
        base.append(
            RemediationSuggestion(
                priority=RemediationPriority.P1,
                action=scenario_suggestions[scenario],
                rationale=f"场景 {scenario.value} 的通用处置方向",
            )
        )

    # 根因级建议：rank1 最可能根因的处置
    if candidates:
        top = candidates[0]
        if "慢" in top.hypothesis or "超时" in top.hypothesis:
            base.append(
                RemediationSuggestion(
                    priority=RemediationPriority.P1,
                    action="针对慢/超时调用链，确认下游服务健康与依赖配置；必要时扩容或降级",
                    rationale=f"rank1 假设：{top.hypothesis}",
                )
        )
    return base


def _build_audit_trail(
    *,
    event_id: str,
    scenario: ScenarioResult,
    hypotheses: HypothesisScoringResult,
    at: datetime,
) -> list[AuditEntry]:
    """写入审计轨迹（RCA-060 全轨迹审计）。"""
    entries: list[AuditEntry] = []
    entries.append(
        AuditEntry(
            step="2_scenario",
            tool="scenario_router",
            query=f"incident={event_id}",
            hits=0,
            at=at,
            llm_summary=scenario.basis,
        )
    )
    entries.append(
        AuditEntry(
            step="6_hypothesis",
            tool="hypothesis_scoring",
            query=f"incident={event_id}",
            hits=len(hypotheses.candidates),
            at=at,
            llm_summary=hypotheses.basis,
        )
    )
    return entries


def _build_meta(
    *,
    status: ReportStatus,
    violations: list[str],
    token_cost: int,
    duration_sec: int,
) -> ReportMeta:
    """组装报告元信息；校验失败时降级标注。"""
    meta = ReportMeta(
        status=status,
        total_token_cost=token_cost,
        duration_sec=duration_sec,
    )
    if violations:
        meta.status = ReportStatus.PARTIAL  # 校验降级（PRD §8.4：不整份丢弃，如实标注）
        meta.human_feedback = {"validation_violations": violations}
    return meta


def generate_report(
    *,
    report_id: str,
    incident_id: str,
    event_start: datetime,
    scenario: ScenarioResult,
    hypotheses: HypothesisScoringResult,
    evidence_list: list[Evidence],
    graph: TraceGraph | None = None,
    token_cost: int = 0,
    duration_sec: int = 0,
    validator: ReportValidator | None = None,
) -> RCAReport:
    """组装一份完整的 RCAReport（PRD §8.3，纯确定性拼装）。

    参数：
      report_id: 报告 ID
      incident_id: 事件 ID
      event_start: 事件起点（时间先验锚点 + 时间线首事件）
      scenario: 场景路由结果
      hypotheses: 假设打分结果（Top-3 候选）
      evidence_list: 已采集的全部证据
      graph: 可选 trace 图（时间线/链路证据用）
      token_cost / duration_sec: 元信息（编排层统计）
      validator: 候选校验器（不传用默认）

    返回：RCAReport（校验失败时 meta 标 partial + 降级说明）。
    """
    if validator is None:
        validator = ReportValidator()

    # ---- 1. 候选校验（降级裁剪）----
    usable, violations = validator.validate(hypotheses.candidates, evidence_list)
    # 重新编号 rank（裁剪后 rank 连续）
    for i, c in enumerate(usable, 1):
        c.rank = i

    # ---- 2. 时间线 ----
    timeline = _build_timeline(evidence_list, scenario, graph, event_start)

    # ---- 3. 修复建议 ----
    remediation = _build_remediation(usable, scenario.scenario, scenario.business_context)

    # ---- 4. 审计轨迹 ----
    now = datetime.now(timezone.utc)
    audit = _build_audit_trail(
        event_id=incident_id,
        scenario=scenario,
        hypotheses=hypotheses,
        at=now,
    )

    # ---- 5. 元信息（降级标注）----
    meta = _build_meta(
        status=ReportStatus.COMPLETED,
        violations=violations,
        token_cost=token_cost,
        duration_sec=duration_sec,
    )

    return RCAReport(
        report_id=report_id,
        incident_id=incident_id,
        created_at=now,
        scenario=scenario.scenario,
        business_context=scenario.business_context,
        evidence_list=evidence_list,
        root_cause_candidates=usable,
        timeline=timeline,
        remediation_suggestions=remediation,
        audit_trail=audit,
        meta=meta,
    )
