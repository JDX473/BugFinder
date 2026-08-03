"""场景路由（PRD §6.2 步骤 2、§5.2 指标接入）。

职责：把事件判定为 6 类场景之一（5 类技术形态 + `business_logic`），
决定后续走哪套 SOP 剧本。**确定性优先，LLM 兜底**。

判定优先级（高 → 低）：
  1. **指标证据**（主信号，最可靠、可确定性判定）：基于 MAD/3σ 检测器的
     `MetricAnomaly` 输出，指标名 → 场景映射；多指标同时异常取**最早异常**者
     为主场景（时间先验：更早异常的更可能是因），次要信号留给假设打分。
  2. **业务证据**：技术信号干净（无异常指标）时，若事件文本命中业务白名单，
     判定为 `business_logic`（技术健康但功能不对，如"车门打不开"）。
  3. **LLM 兜底**：以上都无法确定时，让 LLM 从 6 个场景枚举里选（走 ask_json
     强约束，只允许枚举值），并给置信度。
  4. **other 兜底**：LLM 也判不出（低置信/解析失败）→ `other`（通用剧本，
     强制人工介入的出口）。

本模块纯逻辑，不直接依赖具体 LLM 实现——LLMClient 可注入（测试用 FakeLLM）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.llm.ask_json import ask_json
from app.llm.protocol import LLMClient
from app.pipeline.anomaly_detection import MetricAnomaly
from app.schema.models import BusinessContext, ScenarioType

# 指标名 → 场景映射。匹配方式：指标名按非字母数字分隔符（./_-）切分后与关键词
# **精确词**比对（大小写不敏感），避免裸子串误匹配（如 "rt" 命中 "cart_abandonment_rate"）。
# 场景优先级 = 元组顺序（延迟 > 错误率 > 资源 > 可用性），同词命中时前面的场景优先。
_METRIC_TO_SCENARIO: tuple[tuple[tuple[str, ...], ScenarioType], ...] = (
    (("latency", "p99", "response", "rt", "duration"), ScenarioType.LATENCY_SPIKE),
    (("error", "errors", "errorrate", "failure", "exception", "5xx"), ScenarioType.ERROR_RATE_SPIKE),
    (("cpu", "memory", "mem", "disk", "io", "gc", "connection", "thread", "load"), ScenarioType.RESOURCE_SATURATION),
    (("availability", "success", "sla", "uptime"), ScenarioType.AVAILABILITY_DROP),
)

# 业务守卫"资源指标"用**独立**的精确词集（不依赖 _METRIC_TO_SCENARIO 的映射词，
# 避免 load/io/rt 等歧义词误当资源指标——"观测到健康资源指标"必须证明资源层确实被观测）。
_RESOURCE_KEYWORDS: tuple[str, ...] = ("cpu", "memory", "mem", "disk", "io", "gc", "connection", "thread", "load")

# 指标名按这些分隔符切分成词
_METRIC_SEPARATORS = re.compile(r"[^a-z0-9]+")

# 业务白名单：技术信号干净时，告警文本命中这些**业务症状短语** → business_logic。
# 元组顺序即优先级，前面的先匹配。条目 = (业务症状/动作关键词, 业务实体, 归一化症状词)。
# 注意：只用症状短语，不用裸实体词（如 "车门"）——实体词在技术告警里出现很常见
# （服务名/发布公告），会误判 business_logic。
_BUSINESS_WHITELIST: tuple[tuple[str, str, str], ...] = (
    ("车门打不开", "车门", "打不开"),
    ("无法开门", "车门", "无法开门"),
    ("开不了门", "车门", "打不开"),
    ("支付失败", "支付", "支付失败"),
    ("订单卡住", "订单", "卡住"),
    ("收不到验证码", "验证码", "收不到验证码"),
    ("不能登录", "账户", "不能登录"),
    ("登录不上", "账户", "不能登录"),
)


class ScenarioRoutingError(Exception):
    """路由入口参数非法（空业务白名单等）。"""


@dataclass
class ScenarioResult:
    """场景路由结果（进 incident_context，后续工作流按它分叉）。"""

    scenario: ScenarioType  # 判定的主场景
    confidence: float = 0.0  # 0~1：路由置信度
    basis: str = ""  # 判定依据（指标/业务/llm/other 及摘要），审计与 debug 用
    source: str = "none"  # 判定来源：metric / business / llm / other
    business_context: BusinessContext = field(default_factory=BusinessContext)  # 业务上下文
    earliest_anomaly: MetricAnomaly | None = None  # 最早异常指标（主场景依据）
    raw_anomalies: list[MetricAnomaly] = field(default_factory=list)  # 全部异常指标（供假设打分）

    def to_summary(self) -> str:
        """一行摘要，供 incident_context / Evidence.summary。"""
        bc = ""
        if self.business_context.is_present:
            bc = f"，业务上下文 {self.business_context.entity}/{self.business_context.symptom}"
        return (
            f"场景 {self.scenario.value}（来源 {self.source}，置信度 {self.confidence:.2f}，"
            f"依据 {self.basis}）{bc}"
        )


# ---------------------------------------------------------------- 业务白名单

class BusinessWhitelist:
    """业务白名单：判定"技术信号干净 + 命中业务症状 → business_logic"。

    条目带业务实体与归一化症状，供 BusinessContext 抽取。
    """

    def __init__(self, entries: tuple[tuple[str, str, str], ...] = _BUSINESS_WHITELIST):
        if not entries:
            raise ScenarioRoutingError("业务白名单不能为空")
        self.entries = entries

    def match(self, text: str) -> tuple[str, str] | None:
        """在文本中查找首个命中条目。返回 (业务实体, 归一化症状)；未命中 None。"""
        for keyword, entity, symptom in self.entries:
            if keyword in text:
                return entity, symptom
        return None


# ---------------------------------------------------------------- 指标 → 场景

def metric_to_scenario(metric: str) -> ScenarioType | None:
    """按指标名判定场景；无法映射返回 None。

    匹配方式：指标名按非字母数字分隔符切分成词，与关键词做**精确词**比对，
    避免裸子串误匹配（如 "rt" 不该命中 "cart_abandonment_rate"）。
    """
    lowered = metric.lower()
    words = {w for w in _METRIC_SEPARATORS.split(lowered) if w}
    for keywords, scenario in _METRIC_TO_SCENARIO:
        if words & set(keywords):
            return scenario
    return None


def _is_resource_metric(metric: str) -> bool:
    """资源类指标（CPU/内存/磁盘/IO/连接/GC…）——判断技术信号是否健康。

    用独立的精确词集 `_RESOURCE_KEYWORDS`，避免把含歧义子串（load/io/rt）的
    业务指标误当资源指标。业务守卫"观测到健康资源指标"必须证明资源层被观测。
    """
    lowered = metric.lower()
    words = {w for w in _METRIC_SEPARATORS.split(lowered) if w}
    return bool(words & set(_RESOURCE_KEYWORDS))


def _pick_earliest(anomalies: list[MetricAnomaly]):
    """选最早异常：优先异常起始时间；起始时间相同时按幅度(|ratio| 大者先)。

    返回 (最早异常, 按时间排序的异常列表)。
    """
    if not anomalies:
        return None, []
    ordered = sorted(
        anomalies,
        key=lambda a: (
            a.anomaly_start if a.anomaly_start is not None else _MAX_TIME,  # None 视为最晚
            -abs(a.ratio),  # 起始时间相同时，幅度大者优先（时间先验外的最强信号）
        ),
    )
    return ordered[0], ordered


# 时间排序用的"无限大"哨兵（None 起始时间的异常视为最晚，稳定平局）
_MAX_TIME = datetime.max.replace(tzinfo=timezone.utc)


def _has_sufficient_data(a: MetricAnomaly) -> bool:
    """是否观测到足够数据可判定（非"点数不足/空序列无法判定"）。

    detect_anomaly 对点数不足的序列返回 is_anomaly=False 且 detail 含"点数不足"——
    这类序列是"无数据可判定"，不是"观测到健康"，不能用来证明技术信号干净。
    """
    return "点数不足" not in a.detail


def _tech_signal_clean(anomalies: list[MetricAnomaly]) -> bool:
    """技术信号是否干净：无异常指标，且"有观测到健康的资源指标"。

    注意：`anomalies` 必须是**完整**的 detect_anomaly 结果（含 is_anomaly=False
    的正常指标），不能是 detect_anomalies() 过滤后的异常子集——否则"全部正常"
    会退化成空列表，无法证明资源指标被观测过。本模块以"能证明资源指标健康"
    作为走 business_logic 的必要前提，防止指标证据缺失时误判。
    """
    if not anomalies:
        return False
    if any(a.is_anomaly for a in anomalies):
        return False
    return any(
        not a.is_anomaly and _has_sufficient_data(a) and _is_resource_metric(a.metric)
        for a in anomalies
    )


# ---------------------------------------------------------------- LLM 兜底

# LLM 判定被当作权威路由决策的置信度门槛：低于此值视为"判不出"，降级 other。
# 与 docstring"低置信→other"的承诺对齐（模型"随口选"不该进特定场景 SOP）。
_LLM_MIN_CONFIDENCE = 0.5

_LLM_SCENARIO_SCHEMA = {
    "type": "object",
    "properties": {
        "scenario": {
            "type": "string",
            "enum": [s.value for s in ScenarioType],
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "business_entity": {"type": "string"},
        "business_symptom": {"type": "string"},
    },
    "required": ["scenario", "confidence"],
    # 不用 additionalProperties:False——DeepSeek 常在 JSON 里附带 reason/说明等
    # 多余字段，严格拒绝会把整次 LLM 判定降级到 other。多余键忽略即可。
}


def _llm_scenario_prompt(incident_text: str, anomalies_summary: str) -> tuple[str, str]:
    system = (
        "你是故障场景分类器。根据告警描述与指标证据，判断这次故障属于哪个场景。"
        "只允许从给定枚举里选一个。技术指标全正常但告警描述的是某个业务功能不正常"
        "（如车门打不开、支付失败）时，应选 business_logic。"
    )
    user = (
        f"告警/事件描述：{incident_text}\n"
        f"指标证据：{anomalies_summary or '无'}\n\n"
        "输出 JSON：scenario（枚举值）、confidence（0~1）、"
        "business_entity（业务实体，如'车门'）、business_symptom（业务症状，如'打不开'）。"
        "若与业务无关，business_entity/business_symptom 填空字符串。"
    )
    return system, user


# ---------------------------------------------------------------- 路由主入口

def route_scenario(
    *,
    incident_text: str,
    anomalies: list[MetricAnomaly],
    llm: LLMClient | None = None,
    business_whitelist: BusinessWhitelist | None = None,
) -> ScenarioResult:
    """判定事件场景（确定性优先，LLM 兜底）。

    参数：
      incident_text: 事件描述（告警 title/正文，或手动输入的 free_text），可为空/None
      anomalies: 该事件时间窗内全部指标的 MetricAnomaly（来自异常检测器，
                 可能为空 = 无指标证据）
      llm: 可选 LLMClient（生产 DeepSeek / 测试 FakeLLM）。None = 禁用 LLM 兜底
           （纯规则场景，确定性测试用）。
      business_whitelist: 业务白名单（不传用默认）

    返回：ScenarioResult（含场景、置信度、依据、业务上下文）。
    """
    if incident_text is None:
        incident_text = ""  # 语义来源 free_text/annotations 可为 None（#9 防御）
    if business_whitelist is None:
        business_whitelist = BusinessWhitelist()

    # ---- 优先级 1：指标证据 ----
    abnormal = [a for a in anomalies if a.is_anomaly]
    if abnormal:
        earliest, ordered = _pick_earliest(abnormal)
        # 按时间序遍历异常，取**第一个可映射**的指标作主场景：
        # 最早异常不可映射时（如网络类自定义指标），仍可路由到同窗内的可映射异常，
        # 不把整个指标分支丢弃。
        driver = next((a for a in ordered if metric_to_scenario(a.metric) is not None), None)
        if driver is not None:
            scenario = metric_to_scenario(driver.metric)
            basis = f"指标 {driver.metric} 异常（{driver.shape.value}，起始 {_fmt_ts(driver.anomaly_start)}）"
            return ScenarioResult(
                scenario=scenario,
                confidence=0.9,
                basis=basis,
                source="metric",
                business_context=_business_from_text(incident_text, business_whitelist),
                earliest_anomaly=earliest,  # 记录时间最早者（供假设打分）
                raw_anomalies=abnormal,
            )

    # ---- 优先级 2：业务证据（技术信号干净 + 命中业务白名单）----
    if _tech_signal_clean(anomalies):
        match = business_whitelist.match(incident_text)
        if match is not None:
            entity, symptom = match
            return ScenarioResult(
                scenario=ScenarioType.BUSINESS_LOGIC,
                confidence=0.85,
                basis=f"技术信号干净（资源指标正常），业务白名单命中 '{match[1]}'",
                source="business",
                business_context=BusinessContext(
                    entity=entity, symptom=symptom, action="", confidence=1.0, source="rule"
                ),
                raw_anomalies=anomalies,
            )

    # ---- 优先级 3：LLM 兜底 ----
    llm_error: str | None = None
    if llm is not None:
        anomalies_summary = "\n".join(
            f"- {a.metric}: {a.shape.value} (ratio {a.ratio:.2f})" for a in anomalies
        )
        system, user = _llm_scenario_prompt(incident_text, anomalies_summary)
        fallback = lambda: {"scenario": ScenarioType.OTHER.value, "confidence": 0.1}
        try:
            result = ask_json(
                llm, system, user, _LLM_SCENARIO_SCHEMA, fallback=fallback, temperature=0.0
            )
        except Exception as e:  # LLM 兜底自身失败不该炸掉整个路由
            result = None
            llm_error = f"LLM 兜底异常: {e}"
        if result is not None and result.data is not None:
            scenario = _safe_scenario(result.data.get("scenario"))
            entity = str(result.data.get("business_entity", "") or "")
            symptom = str(result.data.get("business_symptom", "") or "")
            llm_conf = float(result.data.get("confidence", 0.5))
            if result.ok and llm_conf >= _LLM_MIN_CONFIDENCE:
                # LLM 判定置信度门槛：低置信（<0.5）视为"判不出"，不当作权威路由决策
                # （docstring 承诺"低置信→other"）。LLM 返回 OTHER 时钳制置信度为
                # 0.1，与确定性 other 出口语义一致（other = 低置信兜底出口）。
                if scenario == ScenarioType.OTHER:
                    llm_conf = 0.1
                return ScenarioResult(
                    scenario=scenario,
                    confidence=llm_conf,
                    basis="LLM 判定",
                    source="llm",
                    business_context=BusinessContext(
                        entity=entity, symptom=symptom, action="",
                        confidence=llm_conf,
                        source="llm",
                    ),
                    raw_anomalies=anomalies,
                )
            if not result.ok:
                llm_error = f"LLM 结构化解析失败，走确定性兜底（{result.error or '未知'}）"
            else:
                llm_error = f"LLM 判定置信度过低（{llm_conf:.2f} < {_LLM_MIN_CONFIDENCE}），降级 other"

    # ---- 优先级 4：other 兜底 ----
    if llm_error is None:
        llm_error = "无指标证据、无业务命中、无 LLM"
    return ScenarioResult(
        scenario=ScenarioType.OTHER,
        confidence=0.1,
        basis=llm_error,
        source="other",
        business_context=_business_from_text(incident_text, business_whitelist),
        raw_anomalies=anomalies,
    )


def _safe_scenario(value: object) -> ScenarioType:
    """把 LLM 返回的场景值安全转成枚举；非法值 → OTHER。"""
    try:
        return ScenarioType(str(value))
    except ValueError:
        return ScenarioType.OTHER


def _business_from_text(text: str, whitelist: BusinessWhitelist) -> BusinessContext:
    """从文本做规则级业务上下文抽取（不依赖技术信号干净与否）。"""
    match = whitelist.match(text)
    if match is None:
        return BusinessContext()
    entity, symptom = match
    return BusinessContext(entity=entity, symptom=symptom, action="", confidence=1.0, source="rule")


def _fmt_ts(ts) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ") if ts else "?"
