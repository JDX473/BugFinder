"""LangGraph 工作流共享状态（PRD §6.1 编排层的"背板"）。

`WorkflowState` 是 TypedDict，langgraph 用它做节点间的状态传递与 checkpoint。
所有字段显式声明合并语义（reducer）：

  - `evidence` 用 `operator.add`（列表累加——每次节点往共享状态追加证据）
  - 其余字段用 `replace`（整体覆盖，如 `incident`/`scenario`/`graph`/`report`）

State 里直接存 Pydantic / dataclass 对象（`IncidentEvent`/`ScenarioResult`/
`TraceGraph`/`RCAReport`）——langgraph MemorySaver 对 dataclass 走 msgpack
序列化（已验证可 roundtrip），对象引用能随 checkpoint 恢复。同时也存关键
派生值（如 `incident_text`），避免节点反复解包对象。
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from app.schema.models import RCAReport, IncidentEvent


def replace_reducer(_prev: Any, new: Any) -> Any:
    """整体覆盖 reducer：新值直接替换旧值（不走合并）。"""
    return new


class WorkflowState(TypedDict, total=False):
    """一次事件调查的完整共享状态（贯穿 7 步）。

    字段分组：
      - 输入：incident / incident_text / event_start / services
      - 各步产物：scenario / graph / evidence（累加）/ hypotheses / report
      - 控制：step_index（当前执行到第几步）/ meta（预算与降级信息）
      - 人工介入：hitl_interrupts（中断位置记录）
    """

    # ---- 输入（事件进入时设置，不随节点变化）----
    incident: IncidentEvent  # 归一化后的事件（PRD §8.1）
    incident_text: str  # 事件描述（告警 title/正文，供场景路由/LLM 兜底）
    event_start: Any  # 事件起点（时间先验锚点）
    services: list[str]  # 关联服务（按日志聚合，供指标过滤）

    # ---- 各步产物 ----
    scenario: Any  # 场景路由结果（ScenarioResult，步骤 2）
    graph: Any  # trace 重建图（TraceGraph，步骤 3）
    evidence: Annotated[list, operator.add]  # 已采集的全部证据（步骤 3~5 累加）
    hypotheses: Any  # 假设打分结果（HypothesisScoringResult，步骤 6）
    report: RCAReport  # 最终报告（步骤 7）

    # ---- 控制与审计 ----
    step_index: Annotated[int, replace_reducer]  # 当前执行到第几步（0 起，用于断点/恢复）
    meta: Annotated[dict, replace_reducer]  # 元信息：token_cost / duration_sec / budget 快照 / violations
    hitl_interrupts: Annotated[list, operator.add]  # HITL 中断位置（供恢复时跳过）
    hitl_resume_value: Annotated[Any, replace_reducer]  # HITL 恢复时人工答复（interrupt 返回值）
