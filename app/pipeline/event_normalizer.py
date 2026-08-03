"""事件接收 / 归一化（RCA-003/004，Agent 的"门卫"）。

职责：把告警平台的脏载荷（webhook payload / 自由文本）归一化为统一的
`IncidentEvent`，并生成去重键、按时间窗去重。

脏载荷的常见问题（本模块解决）：
  - 字段名不统一（ts / time / timestamp，message / msg / text）
  - 时间可能缺时区、可能是毫秒时间戳
  - severity 可能是中文 / 大写 / 数字
  - 没有 traceId（需从 free_text / annotations 里提取）
  - 没有明确的时间窗（需从告警时间推断）

本模块是纯确定性代码，不调用 LLM（LLM 抽取放在第 1 步事件解析，这里先做
结构层面的归一化，保证事件在进入工作流之前就是干净可消费的）。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

from app.schema.models import AlertInfo, IncidentEvent, IncidentSource, ManualInput, Severity

# traceId 常见形态：32 位 hex / 16 位 hex / UUID
_TRACE_ID_RE = re.compile(r"\b(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{16}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\b")


class EventNormalizeError(Exception):
    """无法归一化的告警载荷（缺必要字段等）。"""


def _parse_time(value) -> datetime:
    """把多种时间格式解析为 UTC datetime。

    支持：ISO8601 字符串（含/不含时区）、毫秒/秒级 epoch、datetime 对象。
    naive 时间视为 UTC（由 schema 的 UTCDateTime 保证）。
    value 为空时抛 ValueError。
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        # epoch：>1e12 视为毫秒
        return datetime.fromtimestamp(value / 1000 if value > 1e12 else value, tz=timezone.utc)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            raise ValueError("空时间")
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            pass
        try:
            return datetime.fromtimestamp(float(s), tz=timezone.utc)
        except ValueError:
            pass
    raise ValueError(f"无法解析时间: {value!r}")


def _pick(*candidates) -> str:
    """从多个候选 key 里取第一个非空值。"""
    for c in candidates:
        if c is not None and str(c).strip():
            return str(c).strip()
    return ""


def _normalize_severity(value) -> Severity:
    """把各种 severity 表示归一化为枚举。"""
    if isinstance(value, Severity):
        return value
    s = str(value or "").strip().lower()
    mapping = {
        "critical": Severity.CRITICAL, "严重": Severity.CRITICAL, "p0": Severity.CRITICAL,
        "warning": Severity.WARNING, "warn": Severity.WARNING, "警告": Severity.WARNING, "p1": Severity.WARNING,
        "info": Severity.INFO, "信息": Severity.INFO, "notice": Severity.INFO, "p2": Severity.INFO,
    }
    if s in mapping:
        return mapping[s]
    return Severity.WARNING  # 未知 severity 默认 warning


def _extract_trace_id(payload: dict, text: str) -> str | None:
    """从 payload 字段 / 自由文本里提取 traceId。"""
    for key in ("trace_id", "traceId", "trace-id", "traceid"):
        v = _pick(payload.get(key, ""))
        if v:
            return v
    m = _TRACE_ID_RE.search(text)
    return m.group(0) if m else None


def _build_time_window(payload: dict, triggered_at: datetime, fallback_minutes: int = 30) -> dict:
    """从 payload 推断时间窗；缺省用告警时间前后 fallback_minutes。"""
    start = end = None
    for key in ("starts_at", "start_time", "start", "window_start"):
        if payload.get(key):
            try:
                start = _parse_time(payload[key])
                break
            except ValueError:
                continue
    for key in ("ends_at", "end_time", "end", "window_end"):
        if payload.get(key):
            try:
                end = _parse_time(payload[key])
                break
            except ValueError:
                continue
    if start is None:
        start = triggered_at - timedelta(minutes=fallback_minutes)
    if end is None:
        end = triggered_at + timedelta(minutes=fallback_minutes)
    return {"start": start, "end": end}


def _normalize_labels(payload: dict, text: str) -> dict[str, str]:
    """抽取 labels：service / host / metric（从 payload 或自由文本）。"""
    labels: dict[str, str] = {}
    for key, label_name in (("service", "service"), ("service_name", "service"), ("app", "service")):
        v = _pick(payload.get(key, ""))
        if v:
            labels["service"] = v
            break
    for key in ("host", "instance", "hostname"):
        v = _pick(payload.get(key, ""))
        if v:
            labels["host"] = v
            break
    for key in ("metric", "alertname", "rule_name"):
        v = _pick(payload.get(key, ""))
        if v:
            labels["metric"] = v
            break
    # 从自由文本兜底抽取 service / host
    if "service" not in labels:
        for svc in re.findall(r"服务[:：]?\s*(\w+)", text):
            labels["service"] = svc
            break
    return labels


def normalize_alert_payload(payload: dict) -> IncidentEvent:
    """把一个 webhook 载荷归一化为 IncidentEvent。

    参数 payload：告警平台推来的原始 dict。
    返回：归一化后的 IncidentEvent（含手动触发所缺的时间窗回填）。
    """
    title = _pick(payload.get("title"), payload.get("message"), payload.get("msg"), payload.get("text"))
    if not title:
        raise EventNormalizeError("告警载荷缺少 title/message")

    try:
        triggered_at = _parse_time(
            _pick(payload.get("timestamp"), payload.get("time"), payload.get("ts"), payload.get("triggered_at"))
        )
    except ValueError:
        # 没有时间字段时回退到当前时刻（告警总有一个到达时刻）
        triggered_at = datetime.now(timezone.utc)

    title = _pick(payload.get("title"), payload.get("message"), payload.get("msg"), payload.get("text"))
    if not title:
        raise EventNormalizeError("告警载荷缺少 title/message")

    # 汇总成自由文本（供 traceId 提取与标签兜底）
    combined_text = " ".join(
        str(payload.get(k, "")) for k in ("title", "message", "msg", "text", "annotations", "summary")
    )

    trace_id = _extract_trace_id(payload, combined_text)
    labels = _normalize_labels(payload, combined_text)
    if trace_id:
        labels["trace_id"] = trace_id

    severity = _normalize_severity(_pick(payload.get("severity"), payload.get("level")))
    time_window = _build_time_window(payload, triggered_at)

    return IncidentEvent(
        incident_id=payload.get("incident_id") or f"INC-{triggered_at.strftime('%Y%m%d%H%M%S')}-{abs(hash(title)) % 10000}",
        source=IncidentSource.ALERT_WEBHOOK,
        triggered_at=triggered_at,
        alert=AlertInfo(
            title=title,
            severity=severity,
            labels=labels,
            starts_at=time_window["start"],
            annotations=_pick(payload.get("annotations"), payload.get("summary")) or None,
        ),
        manual_input=ManualInput(
            trace_id=trace_id,
            service=labels.get("service"),
            time_window=None,
        ),
    )


def dedup_key(event: IncidentEvent) -> str:
    """生成事件的去重键（RCA-003 事件归一化与去重）。

    规则：同一 service + metric + 同一 30 分钟窗口内算重复。
    不含 incident_id（它是唯一 ID，不去重）。
    """
    service = event.alert.labels.get("service", "") if event.alert else ""
    metric = event.alert.labels.get("metric", "") if event.alert else ""
    window = event.triggered_at.strftime("%Y%m%d%H")  # 小时级窗口
    raw = f"{service}|{metric}|{window}".encode()
    return hashlib.md5(raw).hexdigest()


class AlertDedupStore:
    """时间窗去重器：记录已见事件，判定新事件是否重复（RCA-003）。

    骨架阶段为内存实现；接真环境后换 Redis（TTL 与并发由外部存储保证）。
    """

    def __init__(self, ttl_minutes: int = 120):
        self.ttl = timedelta(minutes=ttl_minutes)
        self._seen: dict[str, datetime] = {}

    def is_duplicate(self, event: IncidentEvent) -> bool:
        """是否与已见事件重复。重复返回 True，不重复则记录并返回 False。"""
        key = dedup_key(event)
        now = event.triggered_at
        # 清理过期
        self._seen = {k: v for k, v in self._seen.items() if now - v < self.ttl}
        if key in self._seen:
            return True
        self._seen[key] = now
        return False
