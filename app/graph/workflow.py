"""LangGraph 7 步工作流（PRD §6.1 编排层）。

把 7 个确定性节点（见 `nodes.py`）串成状态机：
  START → 1_parse → 2_scenario → 3_trace → 4_logs → 5_metrics
        → 6_hypotheses → 7_report → END

**确定性优先，LLM 次之**：所有节点默认跑确定性实现（mock/规则）。注入 llm
后，步骤 2/6 走 LLM 兜底（ask_json 强约束），步骤 4/5 走有界 ReAct 委派
（见 `bounded_react.py`）。

编排层职责（PRD §6.1/§6.3 + RCA 需求）：
  - **状态传递**：WorkflowState 贯穿 7 步（checkpoint 可恢复）
  - **失败降级**（RCA-012）：节点失败写占位 Evidence，不中断图
  - **时间预算**（RCA-011）：超预算收敛到当前最佳（跳过富集步骤直接出报告）
  - **人工介入**（RCA-013）：HITL interrupt，中间态可中断/恢复
  - **token 预算**：`token_cost` 累计进 meta（真实计费需 LLM 返回 usage，
    ask_json 目前不返回，留 Phase 2；调用方可在节点间注入计数）
"""

from __future__ import annotations

import time as _time
from datetime import datetime, timezone
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from app.graph.nodes import (
    make_hypotheses_node,
    make_logs_node,
    make_metrics_node,
    make_report_node,
    make_scenario_node,
    make_trace_node,
    node_1_parse,
)
from app.graph.state import WorkflowState
from app.schema.models import IncidentEvent
from app.tools.base import LogQuery, MetricQuery


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# 模块级共享的 mock 数据源（评审 #5：每个 RCAWorkflow 实例都重建
# build_mock_logs/build_mock_metrics 是浪费；骨架阶段数据不变，惰性共享一份）。
_shared_mock_log: Any | None = None
_shared_mock_metric: Any | None = None


def _default_log_source():
    global _shared_mock_log
    if _shared_mock_log is None:
        from app.tools.mock_datasource import MockLogDatasource

        _shared_mock_log = MockLogDatasource()
    return _shared_mock_log


def _default_metric_source():
    global _shared_mock_metric
    if _shared_mock_metric is None:
        from app.tools.mock_datasource import MockMetricDatasource

        _shared_mock_metric = MockMetricDatasource()
    return _shared_mock_metric


def make_checkpointer() -> MemorySaver:
    """构造工作流默认 checkpointer。

    评审 #23：langgraph-checkpoint 4.1.1 的默认 serde `_allowed_msgpack_modules`
    为 `True`（允许所有类型），`with_msgpack_allowlist` 在此处是 no-op（短路返回）。
    因此默认 MemorySaver 即可正常序列化项目模型（Pydantic/dataclass），
    反序列化时的 "unregistered type" 警告是 langgraph 对未来硬化的提示，
    当前不阻塞功能，也不需额外配置。生产环境换 Postgres/Redis checkpointer。
    """
    return MemorySaver()


class RCAWorkflowError(Exception):
    """工作流装配/执行错误。"""


# 节点名常量（供测试/调试引用）
NODE_NAMES = {
    "parse": "1_parse",
    "scenario": "2_scenario",
    "trace": "3_trace",
    "logs": "4_logs",
    "metrics": "5_metrics",
    "hypotheses": "6_hypotheses",
    "report": "7_report",
}


class RCAWorkflow:
    """RCA 7 步调查工作流（LangGraph 状态机）。

    用法：
        wf = RCAWorkflow(llm=None)                  # 纯确定性（mock 数据源）
        wf = RCAWorkflow(llm=llm, checkpointer=...)  # 注入 LLM + 持久化

        out = wf.invoke(incident)                    # 一次完整调查
        report = out["report"]

        # HITL：hitl=True 时，步骤 1 后 interrupt 等人工确认
        wf = RCAWorkflow(hitl=True)
        out = wf.invoke(incident)                    # 停在中断点（无 report）
        wf.is_interrupted(thread_id)                 # → True
        out = wf.invoke(incident, resume_value="确认", thread_id=same)  # 恢复
    """

    def __init__(
        self,
        *,
        log_source: LogQuery | None = None,
        metric_source: MetricQuery | None = None,
        llm=None,
        checkpointer=None,
        token_budget: int = 200_000,
        time_budget_sec: int = 600,
        hitl: bool = False,
    ):
        if log_source is None:
            log_source = _default_log_source()
        if metric_source is None:
            metric_source = _default_metric_source()

        self.log_source = log_source
        self.metric_source = metric_source
        self.llm = llm
        self.token_budget = token_budget
        self.time_budget_sec = time_budget_sec
        self.hitl = hitl
        self.checkpointer = checkpointer or make_checkpointer()
        self._graph = self._build_graph()

    # ---------------------------------------------------------------- 图装配

    def _build_graph(self) -> Any:
        g = StateGraph(WorkflowState)

        # 节点（用工厂注入依赖）
        g.add_node(NODE_NAMES["parse"], node_1_parse)
        g.add_node(NODE_NAMES["scenario"], make_scenario_node(llm=self.llm, metric_source=self.metric_source))
        g.add_node(NODE_NAMES["trace"], make_trace_node(log_source=self.log_source))
        g.add_node(NODE_NAMES["logs"], make_logs_node(log_source=self.log_source, llm=self.llm))
        g.add_node(NODE_NAMES["metrics"], make_metrics_node())
        g.add_node(NODE_NAMES["hypotheses"], make_hypotheses_node(llm=self.llm))
        g.add_node(NODE_NAMES["report"], make_report_node())

        # HITL 检查节点（RCA-013 人工介入点：步骤 1 后等人工确认/补充）
        if self.hitl:
            from langgraph.types import interrupt

            def hitl_gate(state: dict) -> dict:
                # interrupt 的返回值 = 恢复时 Command(resume=value) 投递的人工答复。
                # 评审 #6：必须消费返回值（写进 state），否则人工补充对调查零影响。
                # 支持两种答复：{"service": "...", "trace_id": "..."} 补充输入，
                # 或纯文本确认（如 "确认继续"）。
                resume_value = interrupt(
                    {
                        "ask": "调查开始前人工确认（补充 traceId / 服务 / 时间窗，或直接确认继续）",
                        "incident": state.get("incident_text", ""),
                        "step_index": state.get("step_index", 1),
                    }
                )
                updates: dict = {
                    "hitl_interrupts": ["2_scenario"],
                    "hitl_resume_value": resume_value,
                    # 中断点：停在 1_parse 之后、2_scenario 之前 → 下一步 index=2
                    "step_index": 2,
                }
                # 人工补充的输入并入 incident（供后续步骤消费）
                if isinstance(resume_value, dict):
                    incident = state.get("incident")
                    if incident is not None and (
                        resume_value.get("trace_id") or resume_value.get("service")
                    ):
                        mi = incident.manual_input
                        if mi is not None:
                            updates["incident"] = incident.model_copy(
                                update={
                                    "manual_input": mi.model_copy(
                                        update={
                                            "trace_id": resume_value.get("trace_id") or mi.trace_id,
                                            "service": resume_value.get("service") or mi.service,
                                        }
                                    )
                                }
                            )
                return updates

            g.add_node("hitl_gate", hitl_gate)

        # 时间预算路由（RCA-011）：超过预算 → 跳过富集步骤直接出报告（收敛）。
        # 注意：这是 `add_conditional_edges` 的**路由函数**（只读判断返回分支名），
        # 不是节点——不能 add_node，否则返回值被当状态更新报错。
        # 评审 #1：预算检查点前置——2_scenario 是第一个 LLM 调用（ask_json），
        # 必须在它之前就判断是否收敛，而不是等到 3_trace 之后。
        def budget_route(state: dict) -> str:
            meta = state.get("meta", {})
            t0 = meta.get("t0")
            if t0 is None:
                return "report"  # 无起点信息时保守收敛
            if _time.time() - t0 > self.time_budget_sec:
                return "report"
            return "continue"

        # 顺序边
        g.add_edge(START, NODE_NAMES["parse"])
        # 预算守卫 1：1_parse → 场景路由之前检查（场景是首个 LLM 调用点，评审 #1）
        # continue → 进入调查（HITL 时先中断等人工，否则直接场景路由）
        # report → 超预算，直接收敛到报告
        next_after_budget = "hitl_gate" if self.hitl else NODE_NAMES["scenario"]
        g.add_conditional_edges(
            NODE_NAMES["parse"],
            budget_route,
            {"continue": next_after_budget, "report": NODE_NAMES["report"]},
        )
        if self.hitl:
            g.add_edge("hitl_gate", NODE_NAMES["scenario"])
        g.add_edge(NODE_NAMES["scenario"], NODE_NAMES["trace"])
        # 预算守卫 2：3_trace → 日志分析之前检查（聚类后可收敛）
        g.add_conditional_edges(
            NODE_NAMES["trace"],
            budget_route,
            {"continue": NODE_NAMES["logs"], "report": NODE_NAMES["report"]},
        )
        g.add_edge(NODE_NAMES["logs"], NODE_NAMES["metrics"])
        g.add_edge(NODE_NAMES["metrics"], NODE_NAMES["hypotheses"])
        g.add_edge(NODE_NAMES["hypotheses"], NODE_NAMES["report"])
        g.add_edge(NODE_NAMES["report"], END)

        return g.compile(checkpointer=self.checkpointer)

    # ---------------------------------------------------------------- 执行

    def invoke(
        self,
        incident: IncidentEvent,
        *,
        thread_id: str | None = None,
        resume_value: Any = None,
    ) -> dict:
        """执行一次调查（首次或恢复）。

        - incident: 归一化事件（用 event_normalizer 构造）
        - thread_id: checkpoint 线程 ID（恢复必须用同一 ID；None 自动生成）
        - resume_value: 恢复时投递的人工答复；None = 首次执行/无值恢复

        返回最终 state dict（含 report）。若被 HITL interrupt 中断，返回
        部分 state（无 report），可通过 `is_interrupted(thread_id)` 判断。
        """
        # 评审 #7：resume 必须显式 thread_id——自动生成的 tid 是恢复时刻的
        # 新值，找不到中断线程会静默空跑（重新从头执行，且不产出 report）。
        if resume_value is not None and thread_id is None:
            raise RCAWorkflowError("恢复调查必须显式传入 thread_id（恢复时自动生成的新 ID 找不到中断线程）")

        t_start = _time.time()
        # 评审 #8：thread_id 自动生成带随机后缀，避免同秒同 incident 两次
        # invoke 碰撞共用一个 checkpoint（重试/重复上报）。
        tid = thread_id or f"inc-{incident.incident_id}-{int(t_start * 1000)}-{id(incident) % 10000}"
        config = {"configurable": {"thread_id": tid}}

        initial: dict = {
            "incident": incident,
            "evidence": [],
            "step_index": 0,
            "meta": {
                "token_cost": 0,
                "duration_sec": 0,
                "token_budget": self.token_budget,
                "time_budget_sec": self.time_budget_sec,
                "t0": t_start,
                "budget_exceeded": False,
            },
            "hitl_interrupts": [],
        }

        try:
            if resume_value is None:
                # 首次执行（或该线程无 checkpoint）：注入初始 state 从头跑
                result = self._graph.invoke(initial, config=config)
            else:
                # HITL 恢复：投递人工答复（resume_value），从中断点继续
                from langgraph.types import Command

                # 评审 #11：恢复前取 checkpoint 里的原耗时，恢复后累计
                prev_meta = self.get_state(tid).get("meta", {}) if self._thread_exists(tid) else {}
                prev_duration = prev_meta.get("duration_sec", 0)
                result = self._graph.invoke(Command(resume=resume_value), config=config)
                # 恢复段耗时 = 累计到当前（不覆盖首跑耗时）
                result = self._finalize(result, t_start, config, base_duration=prev_duration)
                return result
        except RCAWorkflowError:
            raise
        except Exception as e:
            raise RCAWorkflowError(f"工作流执行失败: {e}") from e

        return self._finalize(result, t_start, config)

    def stream(
        self,
        incident: IncidentEvent,
        *,
        thread_id: str | None = None,
    ):
        """流式执行调查：yield 每步进度事件，最后 yield 最终报告。

        用于 Web 前端实时展示"正在做第几步"（SSE）。每步 yield：
          {"type": "step", "step": int, "name": str, "status": "done"|"skipped"}
        最后 yield：
          {"type": "report", "report": dict}

        HITL 中断时 yield {"type": "interrupt", "message": str} 后停止。
        """
        t_start = _time.time()
        tid = thread_id or f"inc-{incident.incident_id}-{int(t_start * 1000)}-{id(incident) % 10000}"
        config = {"configurable": {"thread_id": tid}}

        initial: dict = {
            "incident": incident,
            "evidence": [],
            "step_index": 0,
            "meta": {
                "token_cost": 0,
                "duration_sec": 0,
                "token_budget": self.token_budget,
                "time_budget_sec": self.time_budget_sec,
                "t0": t_start,
                "budget_exceeded": False,
            },
            "hitl_interrupts": [],
        }

        step_names = {
            "1_parse": "事件解析",
            "2_scenario": "场景判定",
            "3_trace": "链路重建",
            "4_logs": "日志分析",
            "5_metrics": "指标验证",
            "6_hypotheses": "假设打分",
            "7_report": "报告生成",
        }
        order = list(step_names.keys())

        try:
            for update in self._graph.stream(initial, config=config, stream_mode="updates"):
                # update = {node_name: node_output}（每步一个）
                for node_name in update:
                    # HITL 中断：interrupt 后 stream 停止，无更多 update
                    if node_name in step_names:
                        idx = order.index(node_name) + 1
                        yield {"type": "step", "step": idx, "name": node_name, "label": step_names[node_name]}
            # 中断检测
            st = self._graph.get_state(config)
            if st.next:
                yield {"type": "interrupt", "message": "等待人工确认（HITL）", "thread_id": tid}
                return

            result = self._finalize(dict(st.values), t_start, config)
            yield {"type": "report", "report": result["report"].model_dump(mode="json")}
        except Exception as e:
            yield {"type": "error", "message": str(e)}

    def _thread_exists(self, thread_id: str) -> bool:
        """该线程是否已有 checkpoint（非空）？"""
        try:
            st = self._graph.get_state({"configurable": {"thread_id": thread_id}})
            return bool(st.values)
        except Exception:
            return False

    def _finalize(self, result: dict, t_start: float, config: dict, base_duration: int = 0) -> dict:
        """收尾：回填 meta 耗时，返回含 report 的最终 state。

        base_duration：resume 时传首跑已累计的耗时，本次增量叠加（评审 #11）。
        """
        meta = dict(result.get("meta", {}))
        meta["duration_sec"] = base_duration + int(_time.time() - t_start)
        result["meta"] = meta
        if result.get("report") is not None:
            result["report"].meta.duration_sec = meta.get("duration_sec", 0)
            result["report"].meta.total_token_cost = meta.get("token_cost", 0)
        return result

    def is_interrupted(self, thread_id: str) -> bool:
        """该线程是否停在 HITL 中断点（未跑完）？

        评审 #10：对从未创建的线程也返回 False（无 checkpoint → 未中断），
        避免"恢复时校验"误判。
        """
        if not self._thread_exists(thread_id):
            return False
        st = self._graph.get_state({"configurable": {"thread_id": thread_id}})
        return bool(st.next)

    def get_state(self, thread_id: str) -> dict:
        """查看线程的当前 checkpoint 状态（中间态可见，RCA-042）。"""
        st = self._graph.get_state({"configurable": {"thread_id": thread_id}})
        return st.values

    def invoke_manual(
        self,
        *,
        service: str | None = None,
        free_text: str | None = None,
        trace_id: str | None = None,
        thread_id: str | None = None,
    ) -> dict:
        """手动触发调查（RCA-002）：构造 ManualInput 事件后走 invoke。"""
        from app.schema.models import IncidentSource, ManualInput

        incident = IncidentEvent(
            incident_id=f"INC-manual-{int(_time.time())}",
            source=IncidentSource.MANUAL,
            triggered_at=_now_utc(),
            manual_input=ManualInput(trace_id=trace_id, service=service, free_text=free_text),
        )
        return self.invoke(incident, thread_id=thread_id)
