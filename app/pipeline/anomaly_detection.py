"""确定性指标异常检测（PRD §5.2 核心）。

职责：对一段指标时序，确定性地判断——
  - 是否异常（相对基线）
  - 什么形态（突增 / 突降 / 持续走高 / 锯齿 / 无异常）
  - 何时开始异常（突变起始时间）
  - 异常幅度

输出结构化的 `MetricAnomaly` 摘要，而不是原始时间序列。
LLM 只读这个摘要，不做数值判断（LLM 对纯数字的理解不可靠且费 token）。

方法：
  - **3σ / Z-score**：以窗口内均值±标准差为界，检测突增/突降（突变检测）。
  - **MAD**（中位数绝对偏差）：比 σ 更抗离群点，作为稳健基线，用于"持续走高"趋势。
  - **趋势**：比较前后两半均值，检测渐变型异常（MAD/3σ 抓不到的那种）。

边界：
  - 需要至少一个"正常基线窗口"（比较窗口）才能判定异常；只有孤立的几个点无法判定。
  - 本模块是确定性工具，不调用 LLM。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.schema.models import MetricPoint, MetricSeries


class AnomalyShape(StrEnum):
    SPIKE_UP = "spike_up"  # 突增
    SPIKE_DOWN = "spike_down"  # 突降
    RISE = "rise"  # 持续走高（渐变）
    FALL = "fall"  # 持续走低
    JITTER = "jitter"  # 锯齿/剧烈抖动
    NORMAL = "normal"


@dataclass
class MetricAnomaly:
    """一个指标的结构化异常摘要（喂给 LLM / 写入 Evidence）。"""

    metric: str
    shape: AnomalyShape
    baseline_mean: float  # 基线窗口均值
    anomaly_start: object | None  # 异常起始时间（datetime），无异常时 None
    current_mean: float  # 检测到异常后窗口的均值
    ratio: float  # current / baseline（>1 增，<1 减）
    detail: str = ""  # 供报告使用的自然语言说明
    is_anomaly: bool = False

    def to_summary(self) -> str:
        """转成一行摘要，供 LLM 精读 / 写入 Evidence.summary。"""
        if not self.is_anomaly:
            return f"指标 {self.metric} 无异常（基线均值 {self.baseline_mean:.4g}）"
        start = self.anomaly_start.strftime("%Y-%m-%dT%H:%M:%SZ") if self.anomaly_start else "?"
        return (
            f"指标 {self.metric} 在 {start} 出现{_shape_cn(self.shape)}，"
            f"从基线均值 {self.baseline_mean:.4g} 变为 {self.current_mean:.4g}"
            f"（{self.ratio:.2f} 倍）"
        )


def _shape_cn(shape: AnomalyShape) -> str:
    return {
        AnomalyShape.SPIKE_UP: "突增",
        AnomalyShape.SPIKE_DOWN: "突降",
        AnomalyShape.RISE: "持续走高",
        AnomalyShape.FALL: "持续走低",
        AnomalyShape.JITTER: "剧烈抖动",
        AnomalyShape.NORMAL: "无异常",
    }[shape]


def _mad(values: list[float]) -> float:
    """中位数绝对偏差。比 σ 抗离群点。"""
    if not values:
        return 0.0
    med = sorted(values)[len(values) // 2]
    return sorted(abs(v - med) for v in values)[len(values) // 2]


def detect_anomaly(
    series: MetricSeries,
    *,
    split_ratio: float = 0.5,
    z_threshold: float = 3.0,
    mad_threshold: float = 4.0,
    min_points: int = 8,
    min_ratio: float = 1.2,
) -> MetricAnomaly:
    """检测一个指标序列的异常。

    - split_ratio：按时间分成基线窗口（前）与检测窗口（后）的比例
    - z_threshold：突增/突降的 Z-score 阈值（3σ 对应 z>=3）
    - mad_threshold：持续走高的稳健阈值（用 MAD 归一化的 Z）
    - min_points：少于该点数的序列无法判定（返回 NORMAL）
    - min_ratio：检测窗口均值 / 基线均值 的幅度门槛（默认 1.2，即偏离 <20% 视为正常）
    """
    points = series.points
    if len(points) < min_points:
        return MetricAnomaly(
            metric=series.metric,
            shape=AnomalyShape.NORMAL,
            baseline_mean=_mean([p.value for p in points]),
            anomaly_start=None,
            current_mean=_mean([p.value for p in points]),
            ratio=1.0,
            detail=f"点数不足（{len(points)} < {min_points}），无法判定",
            is_anomaly=False,
        )

    n = int(len(points) * split_ratio)
    base_pts, detect_pts = points[:n], points[n:]

    base_vals = [p.value for p in base_pts]
    detect_vals = [p.value for p in detect_pts]

    base_mean = _mean(base_vals)
    base_std = _std(base_vals)
    detect_mean = _mean(detect_vals)
    ratio = detect_mean / base_mean if base_mean else 0.0

    # 幅度门槛：偏离不够大一律视为正常（防微扰）
    if ratio >= 1.0:
        meets_magnitude = ratio >= min_ratio
    else:
        meets_magnitude = ratio <= 1.0 / min_ratio

    if not meets_magnitude:
        return MetricAnomaly(
            metric=series.metric,
            shape=AnomalyShape.NORMAL,
            baseline_mean=base_mean,
            anomaly_start=None,
            current_mean=detect_mean,
            ratio=ratio,
            detail=f"偏离幅度不足（{ratio:.3f}，门槛 {min_ratio}），视为正常",
            is_anomaly=False,
        )

    if base_std == 0:
        # 基线完全平稳：任何达到幅度门槛的偏离都是突变
        if detect_mean != base_mean:
            shape = AnomalyShape.SPIKE_UP if detect_mean > base_mean else AnomalyShape.SPIKE_DOWN
            start = _first_deviant(points, n, base_mean, shape)
            return _make_anomaly(series, shape, base_mean, start, detect_pts, ratio)
        return MetricAnomaly(
            metric=series.metric,
            shape=AnomalyShape.NORMAL,
            baseline_mean=base_mean,
            anomaly_start=None,
            current_mean=detect_mean,
            ratio=1.0,
            detail="基线平稳且检测窗口无偏离",
            is_anomaly=False,
        )

    # 核心判定：区分"突变(spike)"与"渐变(rise)"
    #   z_first = 检测窗口第一个点相对基线的偏离（突变 → 起点就跳变）
    #   z_mean  = 检测窗口整体均值相对基线的偏离（渐变 → 整体爬升）
    z_first = (detect_pts[0].value - base_mean) / base_std
    z_mean = (detect_mean - base_mean) / base_std

    if abs(z_mean) >= z_threshold:
        up = z_mean > 0
        # 起点即偏离 → 突变；起点正常但整体偏离 → 渐变
        if abs(z_first) >= z_threshold * 0.7:
            shape = AnomalyShape.SPIKE_UP if up else AnomalyShape.SPIKE_DOWN
        else:
            shape = AnomalyShape.RISE if up else AnomalyShape.FALL
        start = _find_change_point(points, n, base_mean, base_std, z_threshold, direction=shape)
        return _make_anomaly(series, shape, base_mean, start, detect_pts, ratio)

    # MAD 兜底：z_mean 未达阈值但稳健 Z 超（小幅度渐变），归为渐变
    mad_val = _mad(base_vals)
    if mad_val > 0:
        mad_z = (detect_mean - base_mean) / mad_val
        if abs(mad_z) >= mad_threshold:
            shape = AnomalyShape.RISE if mad_z > 0 else AnomalyShape.FALL
            start = _find_change_point(points, n, base_mean, mad_val, mad_threshold, direction=shape)
            return _make_anomaly(series, shape, base_mean, start, detect_pts, ratio)

    # 锯齿/抖动：检测窗口内相对基线的变异过大
    detect_std = _std(detect_vals)
    if detect_std > base_std * 3 and detect_std > 0.01:
        return MetricAnomaly(
            metric=series.metric,
            shape=AnomalyShape.JITTER,
            baseline_mean=base_mean,
            anomaly_start=points[n].ts,
            current_mean=detect_mean,
            ratio=ratio,
            detail=f"检测窗口方差过大（std {detect_std:.4g} vs 基线 {base_std:.4g}）",
            is_anomaly=True,
        )

    return MetricAnomaly(
        metric=series.metric,
        shape=AnomalyShape.NORMAL,
        baseline_mean=base_mean,
        anomaly_start=None,
        current_mean=detect_mean,
        ratio=ratio,
        detail="未超过阈值",
        is_anomaly=False,
    )


def _make_anomaly(series, shape, base_mean, start, detect_pts, ratio) -> MetricAnomaly:
    return MetricAnomaly(
        metric=series.metric,
        shape=shape,
        baseline_mean=base_mean,
        anomaly_start=start,
        current_mean=_mean([p.value for p in detect_pts]),
        ratio=ratio,
        detail=f"{shape.value} 检测（见 summary）",
        is_anomaly=True,
    )


def _first_deviant(points: list[MetricPoint], split_idx: int, base_mean: float, direction: AnomalyShape):
    """找检测窗口内第一个严格偏离基线方向的值的时间点。"""
    for p in points[split_idx:]:
        if direction in (AnomalyShape.SPIKE_UP, AnomalyShape.RISE) and p.value > base_mean:
            return p.ts
        if direction in (AnomalyShape.SPIKE_DOWN, AnomalyShape.FALL) and p.value < base_mean:
            return p.ts
    return points[split_idx].ts if points else None


def _find_change_point(
    points: list[MetricPoint],
    split_idx: int,
    base_mean: float,
    base_scale: float,
    threshold: float,
    direction: AnomalyShape,
) -> object:
    """在检测窗口内找第一个超过阈值的点的时间，作为异常起始时间。"""
    for p in points[split_idx:]:
        z = (p.value - base_mean) / base_scale if base_scale else 0.0
        if direction in (AnomalyShape.SPIKE_UP, AnomalyShape.RISE) and z >= threshold:
            return p.ts
        if direction in (AnomalyShape.SPIKE_DOWN, AnomalyShape.FALL) and z <= -threshold:
            return p.ts
    return points[split_idx].ts if points else None


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = _mean(values)
    return (sum((v - m) ** 2 for v in values) / (len(values) - 1)) ** 0.5


def detect_anomalies(series_list: list[MetricSeries], **kwargs) -> list[MetricAnomaly]:
    """批量检测，返回异常列表（可按幅度排序）。"""
    results = [detect_anomaly(s, **kwargs) for s in series_list]
    anomalies = [r for r in results if r.is_anomaly]
    anomalies.sort(key=lambda a: abs(a.ratio), reverse=True)
    return anomalies
