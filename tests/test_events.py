"""Agent 事件发射单元测试(mock LLM,无网络):事件序列 / 错误路径 / 取消。"""

import json
import threading

from rcagent.config import load_config
from rcagent.core.agent import JobDesc, RCAgent
from rcagent.core.events import AgentEvents
from rcagent.env.local import LocalEnvironment
from rcagent.llm.client import LLMClient

CFG = load_config()
JOB = JobDesc(job_id="demo_es_conn_timeout", anomaly="job failed",
              detect_time="2024-01-01 12:00:00")


def build_agent(script, events=None):
    llm = LLMClient(CFG.llm, mock_script=script)
    return RCAgent.build(CFG, llm, LocalEnvironment(), events=events)


def record_sink(events_list):
    def sink(etype, payload):
        events_list.append((etype, payload))
    return sink


class TestEventSequence:
    def test_happy_path_event_sequence(self):
        """正常轨迹的事件序列: run_started → ... → finalize_result → run_finished。"""
        events = []
        ev = AgentEvents(sink=record_sink(events))

        def script(messages, params):
            n = len([m for m in messages if m["role"] == "assistant"])
            if n == 0:
                return '{"function": "runtime_log", "kwargs": {"job_id": "demo_es_conn_timeout"}}'
            return json.dumps({"function": "finalize", "kwargs": {
                "root_cause": "c", "solution": "s", "evidence": "e",
                "responsibility": "platform"}})

        agent = build_agent(script, ev)
        traj = agent.run(JOB)
        assert traj.passed

        types = [t for t, _ in events]
        # 事件顺序: 每步生成→解析→工具→观察→完成
        assert types[0] == "run_started"
        assert "llm_generating" in types and "llm_generated" in types
        assert "parse_ok" in types
        assert "tool_started" in types and "tool_finished" in types
        assert "observation_injected" in types
        assert "step_completed" in types
        assert "finalize_result" in types
        assert types[-1] == "run_finished"
        # run_finished 载荷
        final = events[-1][1]
        assert final["status"] == "passed"
        assert final["steps"] == 2
        assert "cost" in final

    def test_run_started_payload(self):
        events = []
        ev = AgentEvents(sink=record_sink(events))
        agent = build_agent(lambda m, p: '{"function": "finalize", "kwargs": {}}', ev)
        agent.run(JOB, decode_mode="greedy", max_steps=3)
        started = events[0][1]
        assert started["job_id"] == JOB.job_id
        assert started["variant"] == "full"
        assert started["max_steps"] == 3

    def test_default_none_noop(self):
        """默认 None 时行为与无事件完全一致(回归保障)。"""
        events = []
        ev = AgentEvents(sink=record_sink(events))
        agent = build_agent(lambda m, p: '{"function": "finalize", "kwargs": {}}', ev)
        agent.run(JOB)
        assert events  # 有 sink 时发射
        # 无 sink 的默认实例
        agent2 = build_agent(lambda m, p: '{"function": "finalize", "kwargs": {}}')
        agent2.run(JOB)  # 不抛异常即可


class TestErrorPaths:
    def test_unknown_tool_event(self):
        events = []
        ev = AgentEvents(sink=record_sink(events))

        def script(messages, params):
            n = len([m for m in messages if m["role"] == "assistant"])
            if n == 0:
                return '{"function": "no_such_tool", "kwargs": {}}'
            return '{"function": "finalize", "kwargs": {"root_cause": "c", "solution": "s", "evidence": "e", "responsibility": "platform"}}'

        agent = build_agent(script, ev)
        agent.run(JOB, max_steps=5)
        kinds = [(p.get("kind"), p.get("is_error_feedback"))
                 for t, p in events if t == "tool_validation_error" or t == "observation_injected"]
        assert ("unknown_tool", None) in kinds
        assert any(p.get("is_error_feedback") for t, p in events
                   if t == "observation_injected")

    def test_early_finalize_error_detected(self):
        events = []
        ev = AgentEvents(sink=record_sink(events))
        # 第一步就 finalize → early_finalize 错误检测
        agent = build_agent(
            lambda m, p: '{"function": "finalize", "kwargs": {"root_cause": "c", "solution": "s", "evidence": "e", "responsibility": "platform"}}',
            ev)
        agent.run(JOB, max_steps=3)
        detected = [(p.get("kind"), p.get("tool")) for t, p in events
                    if t == "error_detected"]
        assert ("early_finalize", "finalize") in detected


class TestCancellation:
    def test_cancel_between_steps(self):
        events = []
        cancel = threading.Event()

        def sink(etype, payload):
            events.append((etype, payload))
            if etype == "step_completed" and payload.get("step") == 1:
                cancel.set()  # 第 1 步完成后取消 → 第 2 步顶部检查生效

        ev = AgentEvents(sink=sink, cancel=cancel)

        def script(messages, params):
            return '{"function": "runtime_log", "kwargs": {"job_id": "demo_es_conn_timeout"}}'

        agent = build_agent(script, ev)
        traj = agent.run(JOB, max_steps=5)
        assert not traj.passed
        assert len(traj.records) == 1  # 第 2 步被取消,未执行
        assert events[-1][1]["status"] == "cancelled"
