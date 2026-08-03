"""LangGraph 7 步工作流节点（PRD §6.1 / §6.2 的确定性实现）。

每个节点封装一个已有的确定性模块，统一契约：
  - 输入：`WorkflowState`（只读已有字段）
  - 输出：dict（写入 state 的字段增量）

**失败降级原则（RCA-012）**：任一步失败不中断整体。失败节点写入"占位证据"
（Evidence.error=True，报告如实标注该信号缺失/失败），不抛异常中断图。

**依赖注入（LangGraph 约定）**：节点函数签名必须是 `(state) -> dict`
（或 `(state, config)`），不能直接挂 llm/log_source 参数（langgraph 会把
第二个位置参数当 config 注入）。因此节点用 **`make_*_node` 工厂**创建，
依赖（llm / log_source / metric_source）经闭包捕获。
"""

from __future__ import annotations

import time as _time
from datetime import datetime, timedelta, timezone
from functools import partial

from app.pipeline.anomaly_detection import detect_anomaly
from app.pipeline.event_normalizer import normalize_alert_payload
from app.pipeline.hypothesis_scoring import generate_hypotheses
from app.pipeline.log_clustering import cluster_logs
from app.pipeline.report_generation import generate_report
from app.pipeline.scenario_router import ScenarioResult, route_scenario
from app.schema.models import (
    Evidence,
    EvidenceType,
    IncidentEvent,
    IncidentSource,
    LogRecord,
    MetricSeries,
    ScenarioType,
    TimeRange,
)
from app.tools.trace_reconstruction import TraceReconstructionError, rebuild_trace


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _error_evidence(
    eid: str, step: str, tool: str, detail: str, *, etype: EvidenceType = EvidenceType.LOG
) -> Evidence:
    """失败占位证据（RCA-012）：报告如实标注该信号缺失/失败。"""
    return Evidence(
        evidence_id=eid,
        type=etype,
        source=tool,
        summary=f"[{step}] 失败：{detail}",
        error=True,
    )


def _incident_text(incident: IncidentEvent) -> str:
    """从事件提取"事件描述"（场景路由/LLM 兜底用）。"""
    if incident.alert is not None:
        return incident.alert.title or ""
    if incident.manual_input is not None and incident.manual_input.free_text:
        return incident.manual_input.free_text
    return ""


def _event_start(incident: IncidentEvent) -> datetime:
    """事件起点：告警触发时间（或手动输入的时间窗起点）。"""
    if incident.alert is not None and incident.alert.starts_at is not None:
        return incident.alert.starts_at
    if incident.manual_input is not None and incident.manual_input.time_window is not None:
        return incident.manual_input.time_window.start
    return incident.triggered_at


def _extract_trace_id(incident: IncidentEvent) -> str | None:
    if incident.manual_input is not None and incident.manual_input.trace_id:
        return incident.manual_input.trace_id
    if incident.alert is not None:
        return incident.alert.labels.get("trace_id")
    return None


def _incident_window(event_start: datetime, minutes: int = 30) -> TimeRange:
    """事件时间窗：事件起点 ± minutes（与 mock 指标序列对齐）。"""
    return TimeRange(start=event_start - timedelta(minutes=minutes), end=event_start + timedelta(minutes=minutes))


# ---------------------------------------------------------------- 步骤 1：事件解析

def node_1_parse(state: dict) -> dict:
    """事件解析：从 IncidentEvent 提取工作流派生字段（RCA-004）。

    `incident` 由编排层在进入图前用 `event_normalizer.normalize_alert_payload`
    归一化（脏告警 → 标准 IncidentEvent）。本节点不重复归一化，只提取
    场景路由 / 时间线 / 服务过滤需要的字段。
    """
    incident: IncidentEvent = state.get("incident")
    if incident is None:
        return {"incident_text": "", "event_start": _now_utc(), "services": [], "step_index": 1}

    text = _incident_text(incident)
    start = _event_start(incident)
    services: list[str] = []
    if incident.alert is not None:
        svc = incident.alert.labels.get("service")
        if svc:
            services = [svc]
    elif incident.manual_input is not None and incident.manual_input.service:
        services = [incident.manual_input.service]
    return {"incident_text": text, "event_start": start, "services": services, "step_index": 1}


# ---------------------------------------------------------------- 步骤 2：场景认知

def _fetch_metric_series(metric_source, services: list[str]) -> list[MetricSeries]:
    """从指标数据源取序列（按服务过滤；无 service 字段时取全部）。"""
    if metric_source is None or not hasattr(metric_source, "series"):
        return []
    all_series = list(metric_source.series.values())
    if not services:
        return all_series
    return [s for s in all_series if s.labels.get("service") in services]


def make_scenario_node(llm=None, metric_source=None):
    """场景认知节点工厂：判定 6 类场景（scenario_router）。

    指标检测**前置**（确定性）：取序列 → detect_anomaly → 完整结果（含正常）
    存进 state.metric_anomalies，供步骤 5 直接消费（不重复检测）。这符合
    PRD §5.2"确定性优先：先做异常检测再喂 LLM"。场景路由用这些异常判定场景。
    """

    def node(state: dict) -> dict:
        services = state.get("services", [])
        try:
            series = _fetch_metric_series(metric_source, services)
            all_results = [detect_anomaly(s) for s in series]
        except Exception as e:
            # 评审 #13：指标源异常（series=None / 网络）不炸整条工作流（RCA-012），
            # 降级为"无指标证据"场景路由（落 other 或业务/LLM 兜底）。
            all_results = []
        scenario = route_scenario(
            incident_text=state.get("incident_text", ""),
            anomalies=all_results,
            llm=llm,
        )
        return {
            "scenario": scenario,
            "metric_series": _safe_series(metric_source, services),
            "metric_anomalies": all_results,
            "step_index": 2,
        }

    return node


def _safe_series(metric_source, services: list[str]) -> list:
    """安全取指标序列；失败返回空列表（不炸）。"""
    try:
        return _fetch_metric_series(metric_source, services)
    except Exception:
        return []


# ---------------------------------------------------------------- 步骤 3：链路重建

def make_trace_node(log_source=None):
    """链路重建节点工厂：按 traceId 聚合日志重建调用链（PRD §5.3）。

    失败（无 traceId / 聚合不到日志）→ 占位 Evidence + graph=None，
    后续步骤缺 trace 证据但继续。
    """

    def node(state: dict) -> dict:
        incident: IncidentEvent = state.get("incident")
        evs: list[Evidence] = []
        trace_id = _extract_trace_id(incident) if incident else None
        if not trace_id:
            evs.append(
                _error_evidence("ev-trace", "3_trace", "trace_reconstruction", "无 traceId，跳过链路重建", etype=EvidenceType.TRACE)
            )
            return {"graph": None, "evidence": evs, "step_index": 3}
        try:
            graph = rebuild_trace(log_source, trace_id)
            evs.append(
                Evidence(
                    evidence_id="ev-trace",
                    type=EvidenceType.TRACE,
                    source="trace_reconstruction",
                    summary=f"trace {graph.trace_id} 重建 {len(graph.hops)} 跳（{graph.reconstruction_confidence.value}）",
                    payload={"trace_id": graph.trace_id, "hop_count": len(graph.hops)},
                )
            )
            return {"graph": graph, "evidence": evs, "step_index": 3}
        except Exception as e:
            # 评审 #14：数据源通用异常（ES 探活失败/网络断/权限）也降级为占位证据，
            # 不穿透炸掉整条工作流（RCA-012）。
            evs.append(_error_evidence("ev-trace", "3_trace", "trace_reconstruction", str(e), etype=EvidenceType.TRACE))
            return {"graph": None, "evidence": evs, "step_index": 3}

    return node


# ---------------------------------------------------------------- 步骤 4：日志分析

def _fetch_incident_logs(log_source, incident: IncidentEvent, event_start: datetime) -> list[LogRecord]:
    """取事件关联日志：优先按 traceId，其次按服务 + 时间窗。"""
    window = _incident_window(event_start)
    trace_id = _extract_trace_id(incident) if incident else None
    if trace_id:
        return log_source.query_logs(window, trace_id=trace_id)
    if incident is not None and incident.alert is not None:
        svc = incident.alert.labels.get("service")
        if svc:
            return log_source.query_logs(window, filter_expression=f"service:{svc}")
    return []


def make_logs_node(log_source=None, llm=None):
    """日志分析节点工厂：聚类降噪 + 异常簇摘要（PRD §6.2 步骤 4）。

    确定性优先：先跑 `cluster_logs`（规则聚类，不调 LLM）。
    注入 llm 时（评审 #16 接线）：若聚类结果含异常簇，用有界 ReAct 让 LLM
    判断"是否需要深挖该簇"（工具受限：只能查日志，max_iters=4），结论压成
    一条补充 Evidence。LLM 失败/未注入 → 保持纯确定性（只产出聚类摘要）。
    """

    def node(state: dict) -> dict:
        incident: IncidentEvent = state.get("incident")
        event_start = state.get("event_start")
        evs: list[Evidence] = []
        if log_source is None or incident is None or event_start is None:
            evs.append(_error_evidence("ev-log", "4_logs", "log_clustering", "无日志数据源，跳过日志分析"))
            return {"evidence": evs, "step_index": 4}
        try:
            records = _fetch_incident_logs(log_source, incident, event_start)
        except Exception as e:
            evs.append(_error_evidence("ev-log", "4_logs", "log_clustering", f"日志查询失败：{e}"))
            return {"evidence": evs, "step_index": 4}

        if not records:
            evs.append(_error_evidence("ev-log", "4_logs", "log_clustering", "窗口内无日志数据，跳过日志分析"))
            return {"evidence": evs, "step_index": 4}

        try:
            result = cluster_logs(records)
            abnormal = [c for c in result.clusters if c.level in ("error", "fatal", "critical") or c.error_ratio > 0]
            if abnormal:
                top = abnormal[0]
                evs.append(
                    Evidence(
                        evidence_id="ev-log",
                        type=EvidenceType.LOG,
                        source="log_clustering",
                        summary=f"日志异常簇：{top.template}（{top.count} 条，{','.join(top.services)}）",
                        snippet=top.representatives[0] if top.representatives else None,
                        payload={"cluster_template": top.template, "cluster_count": top.count},
                    )
                )
            else:
                evs.append(
                    Evidence(
                        evidence_id="ev-log",
                        type=EvidenceType.LOG,
                        source="log_clustering",
                        summary=f"日志聚类 {result.total_logs} 条，无异常簇（噪音 {result.noise_count} 条）",
                    )
                )
            # 注入 llm + 有异常簇时：有界 ReAct 决定是否深挖（评审 #16）
            if llm is not None and abnormal:
                evs.append(_react_log_dig(abnormal[0], records, log_source, llm))
        except Exception as e:
            evs.append(_error_evidence("ev-log", "4_logs", "log_clustering", str(e)))
        return {"evidence": evs, "step_index": 4}

    return node


def _react_log_dig(top_cluster, records: list[LogRecord], log_source, llm) -> Evidence:
    """有界 ReAct：让 LLM 决定异常簇是否值得深挖（评审 #16 接线）。

    工具受限：只能调用"查询日志"一个工具，max_iters=4，失败落确定性兜底。
    结论压成一条补充 Evidence（证据压制）。
    """
    from app.graph.bounded_react import ReActTool, run_bounded_react
    from app.schema.models import TimeRange

    window = _incident_window(records[0].timestamp if records else _now_utc())
    query_logs_tool = _QueryLogsTool(log_source, window)

    def fallback() -> dict:
        # 确定性兜底：默认不深挖（聚类摘要已足够）
        return {"deep_dive": False, "reason": "聚类摘要已包含异常信息"}

    result = run_bounded_react(
        task=f"异常日志簇 '{top_cluster.template}' 是否需要深挖原始日志找根因？",
        tools=[query_logs_tool],
        llm=llm,
        fallback=fallback,
        observation_prefix=f"异常簇：{top_cluster.to_summary()}",
    )
    return result.to_evidence("ev-log-react", "log", "bounded_react")


class _QueryLogsTool:
    """ReAct 可调用的受限日志查询工具。"""

    name = "query_logs"
    description = "按关键词查询故障窗口内的原始日志（最多 20 条）"
    args_schema = {
        "type": "object",
        "properties": {"keyword": {"type": "string"}, "level": {"type": "string"}},
        "required": ["keyword"],
    }

    def __init__(self, log_source, window: TimeRange):
        self._log_source = log_source
        self._window = window

    def run(self, args: dict) -> str:
        keyword = str(args.get("keyword", "") or "")
        level = str(args.get("level", "") or "")
        records = self._log_source.query_logs(
            self._window,
            filter_expression=f"level:{level}" if level else "",
            limit=20,
        )
        if keyword:
            records = [r for r in records if keyword.lower() in r.message.lower()]
        if not records:
            return "无匹配日志"
        return "\n".join(f"[{r.level}] {r.service}: {r.message[:120]}" for r in records[:20])


# ---------------------------------------------------------------- 步骤 5：指标验证

def make_metrics_node():
    """指标验证节点工厂：把步骤 2 检测的指标异常落成 Evidence（PRD §6.2 步骤 5）。

    指标检测已在步骤 2 前置完成（state.metric_anomalies 含正常序列），
    本节点只做"结论写入共享状态"，不重复检测——保证全链路一致性。
    """

    def node(state: dict) -> dict:
        all_results = state.get("metric_anomalies", [])
        evs: list[Evidence] = []
        if not all_results:
            evs.append(_error_evidence("ev-metric", "5_metrics", "anomaly_detection", "无指标序列（窗口/服务未配置）", etype=EvidenceType.METRIC))
            return {"evidence": evs, "step_index": 5}
        abnormal = [a for a in all_results if a.is_anomaly]
        evs.append(
            Evidence(
                evidence_id="ev-metric",
                type=EvidenceType.METRIC,
                source="anomaly_detection",
                summary=f"指标检测 {len(all_results)} 个序列，{len(abnormal)} 个异常",
                payload={"anomalies": abnormal, "tech_signal_clean": not abnormal},
            )
        )
        return {"evidence": evs, "step_index": 5}

    return node


# ---------------------------------------------------------------- 步骤 6：假设生成/打分

def make_hypotheses_node(llm=None):
    """假设生成/打分节点工厂：Top-3 候选根因（PRD §6.2 步骤 6）。"""

    def node(state: dict) -> dict:
        scenario = state.get("scenario")
        if scenario is None:
            scenario = ScenarioResult(
                scenario=ScenarioType.OTHER, confidence=0.1, source="other", basis="场景未判定（前序节点失败）"
            )
        result = generate_hypotheses(
            evidence=state.get("evidence", []),
            scenario=scenario,
            graph=state.get("graph"),
            event_start=state.get("event_start"),
            llm=llm,
        )
        return {"hypotheses": result, "step_index": 6}

    return node


# ---------------------------------------------------------------- 步骤 7：报告生成

def make_report_node():
    """报告生成节点工厂：组装 RCAReport（PRD §6.2 步骤 7，纯确定性）。"""

    def node(state: dict) -> dict:
        incident: IncidentEvent = state.get("incident")
        incident_id = incident.incident_id if incident is not None else "unknown"
        scenario = state.get("scenario")
        if scenario is None:
            scenario = ScenarioResult(scenario=ScenarioType.OTHER, confidence=0.1, source="other", basis="场景未判定")
        hypotheses = state.get("hypotheses")
        if hypotheses is None:
            # 预算收敛路径跳过 6_hypotheses → 空候选（报告如实标 partial）
            from app.pipeline.hypothesis_scoring import HypothesisScoringResult

            hypotheses = HypothesisScoringResult(candidates=[], basis="预算收敛，未执行假设打分")
        report = generate_report(
            report_id=f"R-{incident_id}",
            incident_id=incident_id,
            event_start=state.get("event_start", _now_utc()),
            scenario=scenario,
            hypotheses=hypotheses,
            evidence_list=state.get("evidence", []),
            graph=state.get("graph"),
            token_cost=state.get("meta", {}).get("token_cost", 0),
            duration_sec=state.get("meta", {}).get("duration_sec", 0),
        )
        # 评审 #19：预算收敛路径标记 budget_exceeded（预算路由不写状态，
        # 由报告节点在 meta 里如实标注，供上层判断是否走了收敛）。
        meta = dict(state.get("meta", {}))
        t0 = meta.get("t0")
        time_budget = meta.get("time_budget_sec")
        if t0 is not None and time_budget is not None and _time.time() - t0 > time_budget:
            meta["budget_exceeded"] = True
        return {"report": report, "step_index": 7, "meta": meta}

    return node
