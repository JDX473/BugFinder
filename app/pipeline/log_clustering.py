"""日志聚类 / 降噪（PRD §5.1 降采样聚合、§6.2 步骤 4 日志分析）。

职责：把一段故障窗口的日志压成「异常模板簇」摘要，喂给 LLM 精读，
而不是把原文大海直接塞进上下文。控制 token 成本与幻觉的关键一步。

三段式管线（确定性，不调 LLM）：
  1. 规则预过滤：level 过滤 + 噪音模板黑名单（heartbeat / 健康检查 / 已知噪音）。
     噪音日志不是"异常簇"，进不了分析上下文，只进统计字段。
  2. Drain 简化版模板聚类：按 token 把同构日志归并成模板簇
     （变量值归一为占位符，稳定段保留为模板），簇 = 模板 + 计数 + 代表样本。
  3. 簇摘要：每个簇产出代表样本、异常类型、时间范围、错误占比，
     LLM 只读这个摘要。

输出：`ClusterResult`（含噪音统计 + 簇列表），供下游写作 Evidence(LOG) 的 summary。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from app.schema.models import LogRecord

# 噪音模板黑名单：命中的日志行视为"已知噪音"，不进异常簇。
# 按子串匹配（大小写不敏感），覆盖常见 heartbeat / 健康检查 / 框架噪音。
_DEFAULT_NOISE_MARKERS: tuple[str, ...] = (
    "heartbeat",
    "health check",
    "health_check",
    "keepalive",
    "ping",
    "lease renewal",
    "metrics flushed",
    "scheduled task started",
    "worker poll loop",
    "connection pool acquired",
    "cache refreshed",
)

# level 过滤白名单：低于该门槛的日志不进异常簇（只统计、不分析）。
# 按严重程度升序。默认 "warn" = 只保留 warn / error / fatal 进分析。
_DEFAULT_MIN_LEVEL = "warn"
_LEVEL_RANK = {"debug": 0, "trace": 0, "info": 1, "warn": 2, "warning": 2, "error": 3, "fatal": 4, "critical": 4}

# 数字形态变量：独立 token 或紧跟在 0x/：/（ 后面。
_NUM_RE = re.compile(r"0x[0-9a-fA-F]+|\d[\d.,:]*")
# 常见"键=值"形态变量（id 类），整体归一，保留键名便于阅读。
_KV_RE = re.compile(r"\b(id|uuid|trace[_-]?id|request[_-]?id|ip|port|pid|code|error[_-]?code|exception|type|cost|took|duration|elapsed|time|ms|s|node|thread|req[_-]?id)\b\s*[=:]\s*[^,\s]+", re.IGNORECASE)
# 常见 IP：端口形态变量。
_IP_PORT_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d{1,5})?\b")

_DEFAULT_MAX_REPRESENTATIVES = 3  # 每个簇保留的代表样本条数
_DEFAULT_MAX_CLUSTERS = 50  # 上限簇数：超出的低频簇合并进 "other" 簇


class LogClusteringError(Exception):
    """聚类入口参数非法（空噪音黑名单等）。"""


@dataclass
class LogCluster:
    """一个异常日志簇（模板聚类结果）。"""

    template: str  # 归一化模板（变量已占位），作为簇的稳定身份
    count: int  # 簇内日志条数
    level: str  # 簇内最高日志级别
    services: list[str] = field(default_factory=list)  # 出现的服务（去重，保持出现序）
    first_timestamp: object | None = None  # 簇内最早时间
    last_timestamp: object | None = None  # 簇内最晚时间
    representatives: list[str] = field(default_factory=list)  # 原始日志样本（截断）
    exception_type: str | None = None  # 簇内最常见异常类型（来自 exception 字段 / 模板）
    error_ratio: float = 0.0  # 簇内 level>=error 的占比

    def to_summary(self, max_representatives: int = 2) -> str:
        """转成供 LLM 精读的摘要文本。"""
        head = f"[{self.level}] 模板: {self.template}"
        lines = [head, f"计数 {self.count} 条 | 服务 {','.join(self.services)}"]
        if self.first_timestamp is not None:
            lines.append(f"时间 {self.first_timestamp:%H:%M:%S} ~ {self.last_timestamp:%H:%M:%S}")
        if self.exception_type:
            lines.append(f"异常类型 {self.exception_type}")
        if self.error_ratio > 0:
            lines.append(f"错误占比 {self.error_ratio:.0%}")
        samples = self.representatives[:max_representatives]
        if samples:
            lines.append("代表样本: " + " | ".join(samples))
        return "\n".join(lines)


@dataclass
class ClusterResult:
    """日志聚类整体结果。"""

    total_logs: int  # 输入日志总数
    noise_count: int  # 被规则过滤掉的噪音条数
    clustered_count: int  # 进入聚类的日志条数
    clusters: list[LogCluster] = field(default_factory=list)  # 按 count 降序

    def to_summary(self, max_clusters: int = 10) -> str:
        """整体摘要：噪音统计 + 各簇摘要（供 Evidence.summary / LLM 精读）。"""
        lines = [f"日志 {self.total_logs} 条，噪音过滤 {self.noise_count} 条，有效 {self.clustered_count} 条，聚成 {len(self.clusters)} 个模板簇"]
        for c in self.clusters[:max_clusters]:
            lines.append(c.to_summary())
        if len(self.clusters) > max_clusters:
            lines.append(f"… 其余 {len(self.clusters) - max_clusters} 个簇省略")
        return "\n".join(lines)


class LogNoiseFilter:
    """规则预过滤器：level 门槛 + 噪音模板黑名单。"""

    def __init__(self, noise_markers: tuple[str, ...] = _DEFAULT_NOISE_MARKERS, min_level: str = _DEFAULT_MIN_LEVEL):
        if not noise_markers:
            raise LogClusteringError("噪音黑名单不能为空")
        self.markers = tuple(m.lower() for m in noise_markers)
        self.min_rank = _LEVEL_RANK.get(min_level.lower(), _LEVEL_RANK[_DEFAULT_MIN_LEVEL])

    def _is_level_passed(self, record: LogRecord) -> bool:
        return _LEVEL_RANK.get(record.level.lower(), 1) >= self.min_rank

    def _is_noise_by_marker(self, record: LogRecord) -> bool:
        text = record.message.lower()
        return any(m in text for m in self.markers)

    def is_noise(self, record: LogRecord) -> bool:
        """是否噪音：命中黑名单（不管级别）或级别低于门槛。"""
        return self._is_noise_by_marker(record) or not self._is_level_passed(record)


def normalize_template(message: str) -> str:
    """把一条原始日志消息归一化为模板（变量值 → 占位符）。

    归一化顺序（长优先，先具体后宽泛）：
      1. key=value / key:value 形态变量 → {key}
      2. IP[:port] → {ip}（必须在数字之前，否则被通用数字吃掉）
      3. 数字 token（含 0x 十六进制、千分位/小数） → {num}
    之后把消息压缩为单个空格序列。
    """
    text = message.strip()
    text = _KV_RE.sub(r"{\1}", text)  # 保留键名：id=123 → {id}
    text = _IP_PORT_RE.sub("{ip}", text)  # IP 先于通用数字归一
    text = _NUM_RE.sub("{num}", text)
    return re.sub(r"\s+", " ", text)


def _extract_exception_type(record: LogRecord) -> str | None:
    """提取异常类型：优先 exception 字段，其次从消息末尾的异常类名。

    注意 exception 可能是全限定类名（java.util.concurrent.TimeoutException），
    此时取最后一个 `.` 后的短类名。
    """
    if record.exception:
        name = re.search(r"[A-Za-z_$][\w.$]*$", record.exception.strip())
        if name:
            return name.group(0).rsplit(".", 1)[-1]
    # 消息里的异常类型：`ExceptionType: message` 或 `ExceptionTypeException`
    m = re.search(r"\b[A-Z][\w$]*(?:Exception|Error)\b", record.message)
    return m.group(0) if m else None


def _truncate(message: str, max_chars: int = 200) -> str:
    return message if len(message) <= max_chars else message[: max_chars - 1] + "…"


def cluster_logs(
    records: list[LogRecord],
    *,
    noise_filter: LogNoiseFilter | None = None,
    max_representatives: int = _DEFAULT_MAX_REPRESENTATIVES,
    max_clusters: int = _DEFAULT_MAX_CLUSTERS,
    max_sample_chars: int = 200,
) -> ClusterResult:
    """把一段日志聚成模板簇（PRD §5.1 降采样聚合）。

    参数：
      records: 日志行（已按时间/服务/级别过滤过的候选）
      noise_filter: 规则预过滤器（不传用默认：min_level=warn + 默认黑名单）
      max_representatives: 每簇保留的代表样本上限
      max_clusters: 簇数上限，超出的低频簇合并进 "other" 簇
      max_sample_chars: 代表样本截断长度

    返回：ClusterResult（噪音统计 + 簇列表，按 count 降序）。
    """
    if noise_filter is None:
        noise_filter = LogNoiseFilter()

    noise_count = 0
    kept: list[LogRecord] = []
    for rec in records:
        if noise_filter.is_noise(rec):
            noise_count += 1
        else:
            kept.append(rec)

    buckets: dict[str, list[LogRecord]] = {}
    for rec in kept:
        key = normalize_template(rec.message)
        buckets.setdefault(key, []).append(rec)

    clusters: list[LogCluster] = []
    for template, recs in buckets.items():
        clusters.append(_build_cluster(template, recs, max_representatives, max_sample_chars))
    clusters.sort(key=lambda c: c.count, reverse=True)

    # 簇数上限：低频簇合并为 other（保留模板可读性）
    if len(clusters) > max_clusters:
        head, tail = clusters[: max_clusters - 1], clusters[max_clusters - 1 :]
        other = _merge_clusters("other（低频簇聚合）", tail)
        if other is not None:
            head.append(other)
        clusters = head

    return ClusterResult(
        total_logs=len(records),
        noise_count=noise_count,
        clustered_count=len(kept),
        clusters=clusters,
    )


def _build_cluster(template: str, recs: list[LogRecord], max_representatives: int, max_sample_chars: int) -> LogCluster:
    levels = sorted((r.level.lower() for r in recs), key=lambda lv: _LEVEL_RANK.get(lv, 1), reverse=True)
    services: list[str] = []
    for r in recs:
        if r.service not in services:
            services.append(r.service)
    timestamps = [r.timestamp for r in recs]
    errors = sum(1 for r in recs if _LEVEL_RANK.get(r.level.lower(), 1) >= _LEVEL_RANK["error"])
    ex_types = [t for r in recs if (t := _extract_exception_type(r)) is not None]
    most_common_ex = max(set(ex_types), key=ex_types.count) if ex_types else None
    samples = [_truncate(r.message, max_sample_chars) for r in recs[:max_representatives]]

    return LogCluster(
        template=template,
        count=len(recs),
        level=levels[0] if levels else "info",
        services=services,
        first_timestamp=min(timestamps) if timestamps else None,
        last_timestamp=max(timestamps) if timestamps else None,
        representatives=samples,
        exception_type=most_common_ex,
        error_ratio=errors / len(recs) if recs else 0.0,
    )


def _merge_clusters(template: str, clusters: list[LogCluster]) -> LogCluster | None:
    """把多个低频簇合并成一个聚合簇（保留总体统计，代表样本取各簇首个）。"""
    if not clusters:
        return None
    total = sum(c.count for c in clusters)
    levels = sorted((c.level for c in clusters), key=lambda lv: _LEVEL_RANK.get(lv, 1), reverse=True)
    services: list[str] = []
    for c in clusters:
        for s in c.services:
            if s not in services:
                services.append(s)
    first_ts = min((c.first_timestamp for c in clusters if c.first_timestamp is not None), default=None)
    last_ts = max((c.last_timestamp for c in clusters if c.last_timestamp is not None), default=None)
    reps = [r for c in clusters for r in c.representatives[:1]][:_DEFAULT_MAX_REPRESENTATIVES]
    errors = sum(round(c.count * c.error_ratio) for c in clusters)
    ex_types = [t for c in clusters if c.exception_type for t in [c.exception_type]]
    most_common_ex = max(set(ex_types), key=ex_types.count) if ex_types else None

    return LogCluster(
        template=template,
        count=total,
        level=levels[0] if levels else "info",
        services=services,
        first_timestamp=first_ts,
        last_timestamp=last_ts,
        representatives=reps,
        exception_type=most_common_ex,
        error_ratio=errors / total if total else 0.0,
    )
