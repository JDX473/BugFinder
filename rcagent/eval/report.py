"""评估报表:生成论文 Table I~V 风格的结果表。

结构: 方法(method) × 指标(metric) × 字段(field: root_cause/solution/evidence)
每个单元格 mean±std;Win Rate 列为 LLM 评估。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .metrics import mean_std


@dataclass
class FieldMetrics:
    """单个字段(如 root_cause)在单指标上的批量结果。"""

    metric: str
    scores: list[float] = field(default_factory=list)

    @property
    def mean_std_str(self) -> str:
        m, s = mean_std(self.scores)
        if len(self.scores) >= 2:  # 多次运行显示 std(论文 ±)
            return f"{m:.2f}±{s:.2f}"
        return f"{m:.2f}"


@dataclass
class MethodEval:
    """一个方法(rcagent/react/消融变体)在数据集上的全部指标。"""

    name: str
    fields: dict[str, list[FieldMetrics]] = field(default_factory=dict)
    win_rate: float | None = None
    pass_rate: float | None = None
    invalid_rate: float | None = None
    avg_steps: float | None = None

    def add_scores(self, field: str, metric: str, scores: list[float]) -> None:
        self.fields.setdefault(field, []).append(FieldMetrics(metric=metric, scores=scores))


def render_table(methods: list[MethodEval], metrics: list[str]) -> str:
    """渲染 markdown 表格(行=方法,列=各字段×指标)。"""
    header = ["Method"]
    for f in ["root_cause", "solution", "evidence"]:
        if any(f in m.fields for m in methods):
            header += [f"{f[:12]}: {mt}" for mt in metrics]
    rows = [header, ["---"] * len(header)]
    for m in methods:
        row = [m.name]
        for f in ["root_cause", "solution", "evidence"]:
            if f not in m.fields:
                continue
            by_metric = {fm.metric: fm for fm in m.fields[f]}
            for mt in metrics:
                fm = by_metric.get(mt)
                row.append(fm.mean_std_str if fm else "-")
        rows.append(row)
    width = [max(len(r[i]) for r in rows) for i in range(len(header))]
    lines = []
    for r in rows:
        lines.append("| " + " | ".join(c.ljust(w) for c, w in zip(r, width)) + " |")
    return "\n".join(lines)


def render_trajectory_stats(methods: list[MethodEval]) -> str:
    """论文 Table IV 风格: Pass Rate / 轨迹长度 / Invalid Rate。"""
    lines = ["| Method | Pass Rate | Trajectory Length | Invalid Rate |",
             "| --- | --- | --- | --- |"]
    for m in methods:
        lines.append(
            f"| {m.name} | {m.pass_rate:.2f} | {m.avg_steps:.2f} | {m.invalid_rate:.2f} |"
            if m.pass_rate is not None else f"| {m.name} | - | - | - |")
    return "\n".join(lines)
