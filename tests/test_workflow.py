"""workflow.py 的测试：端到端 7 步、HITL 中断/恢复、失败降级、预算收敛、手动触发。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.graph.workflow import RCAWorkflow, RCAWorkflowError, make_checkpointer
from app.pipeline.event_normalizer import normalize_alert_payload
from app.schema.models import IncidentSource, ManualInput, IncidentEvent

UTC = timezone.utc


def _alert_payload(**overrides) -> dict:
    payload = {
        "title": "checkout error_rate 异常",
        "severity": "critical",
        "service": "checkout",
        "timestamp": "2026-08-02T21:00:00Z",
        "trace_id": "tr-mock-0001",
    }
    payload.update(overrides)
    return payload


def _incident(**overrides) -> IncidentEvent:
    return normalize_alert_payload(_alert_payload(**overrides))


# ---------------------------------------------------------------- 端到端

class TestEndToEnd:
    def test_full_investigation(self):
        wf = RCAWorkflow()
        out = wf.invoke(_incident())
        report = out["report"]
        assert report.scenario.value == "error_rate_spike"
        assert len(report.root_cause_candidates) >= 1
        assert report.meta.status.value == "completed"
        assert len(report.evidence_list) >= 3  # trace + log + metric（+ scenario 待接）
        assert report.evidence_list[0].evidence_id

    def test_manual_trigger(self):
        wf = RCAWorkflow()
        out = wf.invoke_manual(service="checkout", free_text="用户反馈支付失败", trace_id="tr-mock-0001")
        assert out["report"].scenario.value == "error_rate_spike"
        assert len(out["report"].root_cause_candidates) >= 1

    def test_business_scenario_via_manual(self):
        wf = RCAWorkflow()
        out = wf.invoke_manual(service="car-door", free_text="用户反馈车门打不开")
        report = out["report"]
        assert report.scenario.value == "business_logic"
        assert report.business_context.entity == "车门"
        assert report.business_context.symptom == "打不开"

    def test_meta_duration_filled(self):
        wf = RCAWorkflow()
        out = wf.invoke(_incident())
        assert out["meta"]["duration_sec"] >= 0
        assert out["report"].meta.duration_sec == out["meta"]["duration_sec"]

    def test_step_index_reaches_7(self):
        wf = RCAWorkflow()
        out = wf.invoke(_incident())
        assert out["step_index"] == 7


# ---------------------------------------------------------------- 失败降级

class TestFailureDegradation:
    def test_no_trace_id_degrades_gracefully(self):
        """无 traceId → trace 占位证据，链路继续到报告。"""
        wf = RCAWorkflow()
        payload = _alert_payload(trace_id=None)
        incident = normalize_alert_payload(payload)
        out = wf.invoke(incident)
        assert out["report"] is not None
        # trace 证据是占位（error=True）
        trace_evs = [e for e in out["report"].evidence_list if e.evidence_id == "ev-trace"]
        assert trace_evs and trace_evs[0].error

    def test_unknown_service_logs_no_crash(self):
        """未知服务 → 日志/指标为空，占位证据，不崩。"""
        wf = RCAWorkflow()
        incident = normalize_alert_payload(_alert_payload(service="no-such-svc"))
        out = wf.invoke(incident)
        assert out["report"] is not None
        assert out["report"].meta.status.value in ("completed", "partial")

    def test_empty_incident_text_no_crash(self):
        """title 非空但剥离后为空（如纯标点）→ incident_text 为空不崩。"""
        wf = RCAWorkflow()
        incident = normalize_alert_payload({"title": "。。。", "timestamp": "2026-08-02T21:00:00Z"})
        out = wf.invoke(incident)
        assert out["report"] is not None


# ---------------------------------------------------------------- HITL

class TestHitl:
    def test_interrupt_and_resume(self):
        wf = RCAWorkflow(hitl=True, checkpointer=make_checkpointer())
        tid = "hitl-1"
        out = wf.invoke(_incident(), thread_id=tid)
        # 首次停在中断点：无 report，is_interrupted True
        assert wf.is_interrupted(tid)
        assert out.get("report") is None
        # 恢复：投递人工答复
        out2 = wf.invoke(_incident(), thread_id=tid, resume_value="确认继续")
        assert not wf.is_interrupted(tid)
        assert out2["report"] is not None
        assert out2["report"].scenario.value == "error_rate_spike"

    def test_no_hitl_runs_through(self):
        wf = RCAWorkflow()
        tid = "nohitl-1"
        out = wf.invoke(_incident(), thread_id=tid)
        assert out.get("report") is not None
        assert not wf.is_interrupted(tid)

    def test_get_state_intermediate(self):
        """中间态可见（RCA-042）：中断后能看到已收集的字段。"""
        wf = RCAWorkflow(hitl=True)
        tid = "state-1"
        wf.invoke(_incident(), thread_id=tid)
        st = wf.get_state(tid)
        assert "incident" in st
        assert st.get("incident_text") == "checkout error_rate 异常"


# ---------------------------------------------------------------- 评审修复回归

class TestReviewFixes:
    def test_resume_value_consumed(self):
        """评审 #6：HITL 恢复时人工答复被消费（写进 hitl_resume_value）。"""
        wf = RCAWorkflow(hitl=True, checkpointer=make_checkpointer())
        tid = "resume-val"
        wf.invoke(_incident(), thread_id=tid)
        assert wf.is_interrupted(tid)
        # 恢复时投递补充输入 dict
        out = wf.invoke(_incident(), thread_id=tid, resume_value={"service": "checkout", "trace_id": "tr-mock-0001"})
        assert not wf.is_interrupted(tid)
        assert out["report"] is not None
        assert out["hitl_resume_value"] == {"service": "checkout", "trace_id": "tr-mock-0001"}

    def test_resume_without_thread_id_raises(self):
        """评审 #7：resume 未传 thread_id 必须抛错（防止静默空跑）。"""
        wf = RCAWorkflow(hitl=True)
        with pytest.raises(RCAWorkflowError):
            wf.invoke(_incident(), resume_value="ok")

    def test_resume_value_supplemented_incident(self):
        """评审 #6：人工补充的 service/trace_id 并入 incident（后续步骤消费）。"""
        wf = RCAWorkflow(hitl=True, checkpointer=make_checkpointer())
        tid = "supplement"
        wf.invoke(_incident(), thread_id=tid)
        out = wf.invoke(_incident(), thread_id=tid, resume_value={"trace_id": "tr-mock-0001"})
        assert out["incident"].manual_input.trace_id == "tr-mock-0001"

    def test_budget_convergence_marks_exceeded(self):
        """评审 #19：预算收敛路径标记 budget_exceeded=True。"""
        wf = RCAWorkflow(time_budget_sec=0)
        out = wf.invoke(_incident())
        assert out["meta"]["budget_exceeded"] is True

    def test_metric_source_broken_degrades(self):
        """评审 #13：metric_source 无 series 属性（或抛异常）不炸工作流。"""
        class BrokenMetricSource:
            series = None  # hasattr 为 True 但访问抛 AttributeError

        wf = RCAWorkflow(metric_source=BrokenMetricSource())
        out = wf.invoke(_incident())
        assert out["report"] is not None  # 场景降级为 other，链路继续

    def test_trace_source_general_exception_degrades(self):
        """评审 #14：日志数据源抛通用异常 → 占位证据，不炸全图。"""
        class BoomLogSource:
            def query_logs(self, *args, **kwargs):
                raise RuntimeError("ES 连接失败")

        wf = RCAWorkflow(log_source=BoomLogSource())
        out = wf.invoke(_incident())
        assert out["report"] is not None
        trace_evs = [e for e in out["report"].evidence_list if e.evidence_id == "ev-trace"]
        assert trace_evs and trace_evs[0].error

    def test_shared_mock_sources(self):
        """评审 #5：多个实例共享同一份 mock 数据源（不重复构建）。"""
        wf1 = RCAWorkflow()
        wf2 = RCAWorkflow()
        assert wf1.log_source is wf2.log_source
        assert wf1.metric_source is wf2.metric_source

    def test_is_interrupted_unknown_thread_false(self):
        """评审 #10：未创建线程 is_interrupted 返回 False（不误判）。"""
        wf = RCAWorkflow(hitl=True)
        assert wf.is_interrupted("no-such-thread") is False

    def test_budget_check_before_scenario(self):
        """评审 #1：超预算时跳过场景路由（首个 LLM 调用点）。"""
        wf = RCAWorkflow(time_budget_sec=0)
        out = wf.invoke(_incident())
        # 场景未判定（跳过），但报告仍产出
        assert out["report"] is not None


# ---------------------------------------------------------------- LLM 决策循环

class _FakeReactLLM:
    """模拟 LLM 决策循环：先调工具，再 final_answer。"""

    def __init__(self, tool_calls: int = 1, conclude: bool = True):
        self.tool_calls = tool_calls
        self.conclude = conclude
        self.calls: list[str] = []

    def complete(self, messages: list[dict[str, str]], temperature: float = 0.0) -> str:
        user = messages[-1]["content"] if messages else ""
        # 每次调用后换动作：先 tool，最后 final_answer
        call_no = len(self.calls)
        self.calls.append(user)
        if call_no < self.tool_calls:
            return '{"tool_name": "query_logs", "args": {"keyword": "ERROR"}, "confidence": 0.9}'
        conclusion = '{"conclude": true, "hypothesis": "payment 超时导致错误传播"}' if self.conclude else '{"conclude": false, "reason": "需要更多证据"}'
        return f'{{"final_answer": {conclusion}, "confidence": 0.9}}'


class TestAgentDecisionLoop:
    def test_agent_node_fires_when_llm_injected(self):
        """llm 注入时，agent 节点触发，ev-agent 进证据。"""
        llm = _FakeReactLLM(tool_calls=1)
        wf = RCAWorkflow(llm=llm)
        out = wf.invoke(_incident())
        ev_ids = [e.evidence_id for e in out["report"].evidence_list]
        assert "ev-agent" in ev_ids
        # LLM 实际调了工具（steps 里有 tool:query_logs）
        agent_ev = next(e for e in out["report"].evidence_list if e.evidence_id == "ev-agent")
        steps = agent_ev.payload["steps"]
        assert any(s["action"].startswith("tool:") for s in steps)

    def test_agent_node_skipped_when_no_llm(self):
        """llm=None 时 agent 不触发（确定性路径不变）。"""
        wf = RCAWorkflow()
        out = wf.invoke(_incident())
        ev_ids = [e.evidence_id for e in out["report"].evidence_list]
        assert "ev-agent" not in ev_ids

    def test_agent_failure_falls_back(self):
        """agent 内 LLM 失败（坏 JSON）→ 确定性兜底，不阻塞报告。"""
        class ThrowingLLM:
            def complete(self, messages, temperature=0.0):
                raise TimeoutError("模拟超时")

        wf = RCAWorkflow(llm=ThrowingLLM())
        out = wf.invoke(_incident())
        assert out["report"] is not None
        # ev-agent 是兜底结论（conclude=True）
        agent_ev = next(e for e in out["report"].evidence_list if e.evidence_id == "ev-agent")
        assert agent_ev.payload["conclusion"].get("conclude") is True

    def test_agent_evidence_has_conclusion(self):
        """agent 结论（final_answer）写进 ev-agent 的 conclusion。"""
        llm = _FakeReactLLM(tool_calls=0, conclude=True)
        wf = RCAWorkflow(llm=llm)
        out = wf.invoke(_incident())
        agent_ev = next(e for e in out["report"].evidence_list if e.evidence_id == "ev-agent")
        assert agent_ev.payload["conclusion"]["conclude"] is True
        assert "payment" in agent_ev.payload["conclusion"]["hypothesis"]


# ---------------------------------------------------------------- 预算

class TestBudget:
    def test_time_budget_exceeded_converges(self):
        """时间预算极低 → 预算路由直接收敛到报告（跳过日志/指标/假设富集）。"""
        wf = RCAWorkflow(time_budget_sec=0)  # 任何耗时都超预算
        out = wf.invoke(_incident())
        assert out["report"] is not None  # 仍能出报告
        # 跳过富集：无日志/指标证据（只有 trace + 场景）
        etypes = {e.type.value for e in out["report"].evidence_list}
        assert "metric" not in etypes  # 收敛路径跳过指标富集

    def test_budget_not_exceeded_runs_full(self):
        wf = RCAWorkflow(time_budget_sec=600)
        out = wf.invoke(_incident())
        etypes = {e.type.value for e in out["report"].evidence_list}
        assert "metric" in etypes
        assert "trace" in etypes
