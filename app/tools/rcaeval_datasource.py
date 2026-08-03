"""RCAEval 开放数据集适配器（真实数据源实现，PRD §5.2）。

把 RCAEval benchmark（RE1 系列）的真实指标数据转成项目 `MetricQuery` 协议，
让 Agent 编排层不感知底层差异——mock 换真实数据源只改配置（RCA_DATA_SOURCE）。

数据格式（RCAEval RE1-OB）：
  - 目录：`{service}_{fault}/{instance}/`（如 `productcatalogservice_cpu/1`）
  - `data.csv`：宽表时序，`time` 列 = epoch 秒，其余列 = `{service}_{metric}`
    （cpu/mem/load/latency/error 等，Prometheus 采集）
  - `inject_time.txt`：故障注入时刻（epoch 秒）——**这就是标注**：
    case 名 `{service}_{fault}` = 根因服务 + 故障类型

用法：
    from app.tools.rcaeval_datasource import RcaEvalMetricSource
    src = RcaEvalMetricSource("E:/QIUZHAO/rca-data/RE1-OB")
    incident = src.incident_for("productcatalogservice_cpu", 1)
    series = src.query_metric("productcatalogservice_cpu", tr)

本模块只读数据（不写任何东西），失败（case 不存在/数据损坏）抛
`RcaEvalDataError`，调用方（工作流节点）已做降级。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.schema.models import (
    AlertInfo,
    IncidentEvent,
    IncidentSource,
    MetricPoint,
    MetricSeries,
    Severity,
    TimeRange,
)
from app.tools.base import MetricQuery


class RcaEvalDataError(Exception):
    """RCAEval 数据读取失败（case 不存在/损坏）。"""


def _epoch_to_utc(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


@dataclass
class RcaEvalCase:
    """一个 RE1 case 的元信息（从目录名解析）。"""

    service: str  # 根因服务（如 productcatalogservice）
    fault: str  # 故障类型（cpu/mem/delay/loss/disk）
    instance: int  # 实例号（1-5）
    inject_time: datetime  # 故障注入时刻（epoch）

    @property
    def ground_truth(self) -> str:
        """标注的根因指标名（`{service}_{fault}`，RCAEval 的 ground truth 形态）。"""
        return f"{self.service}_{self.fault}"


class RcaEvalMetricSource(MetricQuery):
    """把 RCAEval RE1 数据集包装成 MetricQuery 协议实现。

    加载策略：构造时扫描目录索引全部 case（轻量，只读目录+inject_time），
    指标序列按需从 CSV 惰性读取（避免 375 cases 全量驻留内存）。
    """

    def __init__(self, data_root: str):
        self.root = Path(data_root)
        if not self.root.exists():
            raise RcaEvalDataError(f"数据目录不存在: {self.root}")
        self.cases: dict[str, RcaEvalCase] = {}  # key = f"{service}_{fault}/{instance}"
        self._index_cases()

    # ---------------------------------------------------------------- 索引

    def _index_cases(self) -> None:
        # case 目录可能在嵌套层级（zip 解压常多套一层，如 RE1-OB/RE1-OB/）。
        # 递归找形如 `{service}_{fault}/{instance}/inject_time.txt` 的叶子。
        for inst_dir in self.root.rglob("inject_time.txt"):
            case_name = inst_dir.parent.name  # 实例号
            try:
                inst = int(case_name)
            except ValueError:
                continue
            fault_dir = inst_dir.parent.parent
            name = fault_dir.name  # `{service}_{fault}`
            parts = name.rsplit("_", 1)
            if len(parts) != 2:
                continue
            service, fault = parts
            key = f"{name}/{inst}"
            self.cases[key] = RcaEvalCase(
                service=service, fault=fault, instance=inst,
                inject_time=self._read_inject_time(inst_dir.parent),
            )

    def _read_inject_time(self, inst_dir: Path) -> datetime:
        f = inst_dir / "inject_time.txt"
        if not f.exists():
            return datetime.min.replace(tzinfo=timezone.utc)
        try:
            return _epoch_to_utc(float(f.read_text().strip()))
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)

    # ---------------------------------------------------------------- 查询

    def list_cases(self) -> list[str]:
        """列出全部可用 case key（`{service}_{fault}/{instance}`）。"""
        return sorted(self.cases.keys())

    def case(self, key: str) -> RcaEvalCase:
        if key not in self.cases:
            raise RcaEvalDataError(f"case 不存在: {key}（可用: {len(self.cases)} 个）")
        return self.cases[key]

    def load_case_csv(self, key: str) -> list[dict]:
        """加载一个 case 的指标 CSV（返回行 dict 列表）。

        RE1 用 `data.csv`，RE2 用 `metrics.csv`——按存在性探测。用 rglob
        定位（数据可能嵌套），不硬拼路径。
        """
        case = self.case(key)
        target_dir = self.root / f"{case.service}_{case.fault}" / str(case.instance)
        csv_file = None
        for name in ("data.csv", "metrics.csv", "simple_metrics.csv"):
            cand = target_dir / name
            if cand.exists():
                csv_file = cand
                break
        if csv_file is None:
            # 嵌套层级兜底：递归找 {service}_{fault}/{instance}/任一指标 CSV
            for name in ("data.csv", "metrics.csv", "simple_metrics.csv"):
                matches = list(self.root.rglob(f"{case.service}_{case.fault}/{case.instance}/{name}"))
                if matches:
                    csv_file = matches[0]
                    break
            if csv_file is None:
                raise RcaEvalDataError(f"case {key} 缺指标 CSV（找过 {target_dir} 与递归）")
        with open(csv_file, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def metric_series(self, key: str, metric: str) -> MetricSeries:
        """取一个 case 的某个指标序列（惰性加载）。"""
        rows = self.load_case_csv(key)
        pts: list[MetricPoint] = []
        for r in rows:
            try:
                ts = _epoch_to_utc(float(r["time"]))
                val = float(r[metric])
            except (KeyError, ValueError):
                continue  # 空值/脏数据跳过（RE2 metrics.csv 有缺失值）
            pts.append(MetricPoint(ts=ts, value=val))
        return MetricSeries(metric=metric, labels={}, points=pts)

    def query_metric(
        self,
        metric: str,
        time_range: TimeRange,
        *,
        labels: dict[str, str] | None = None,
        step_seconds: int = 60,
    ) -> MetricSeries:
        """协议实现：需要 case 上下文时，从 labels['case'] 定位 case。"""
        key = (labels or {}).get("case")
        if key is None:
            return MetricSeries(metric=metric, labels=labels or {}, points=[])
        full = self.metric_series(key, metric)
        pts = [p for p in full.points if time_range.start <= p.ts <= time_range.end]
        return MetricSeries(metric=metric, labels=labels or {}, points=pts)

    # ---------------------------------------------------------------- 事件构造

    def incident_for(self, key: str) -> IncidentEvent:
        """把一个 case 构造为 IncidentEvent（告警自动触发，RCA-001）。

        事件文本用 case 标注（根因服务 + 故障类型），时间窗 = 注入前后各 30 分钟。
        """
        case = self.case(key)
        window = TimeRange(
            start=case.inject_time - timedelta(minutes=30),
            end=case.inject_time + timedelta(minutes=30),
        )
        return IncidentEvent(
            incident_id=f"RE1-{key.replace('/', '-')}",
            source=IncidentSource.ALERT_WEBHOOK,
            triggered_at=case.inject_time,
            alert=AlertInfo(
                title=f"{case.service} 疑似 {case.fault} 故障",
                severity=Severity.WARNING,
                labels={"service": case.service, "metric": case.ground_truth},
                starts_at=case.inject_time,
            ),
        )

    def anomaly_series(self, key: str) -> list[MetricSeries]:
        """取一个 case 的全部指标序列（供异常检测）。"""
        rows = self.load_case_csv(key)
        cols = [c for c in rows[0] if c != "time"]
        series_list: list[MetricSeries] = []
        for col in cols:
            pts: list[MetricPoint] = []
            for r in rows:
                try:
                    ts = _epoch_to_utc(float(r["time"]))
                    val = float(r[col])
                except (KeyError, ValueError):
                    continue  # 空值/脏数据跳过
                pts.append(MetricPoint(ts=ts, value=val))
            if pts:
                series_list.append(MetricSeries(metric=col, labels={}, points=pts))
        return series_list
