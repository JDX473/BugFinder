"""Agent 主循环端到端测试(mock LLM):正常路径 / 错误路径 / 步数上限。"""

import json

from rcagent.config import load_config
from rcagent.core.agent import JobDesc, RCAgent
from rcagent.core.trajectory import STATUS_FAILED, STATUS_PASSED
from rcagent.env.local import LocalEnvironment
from rcagent.llm.client import LLMClient

JOB = JobDesc(job_id="demo_es_conn_timeout", anomaly="job failed",
              detect_time="2024-01-01 12:00:00")


def build_agent(script):
    cfg = load_config()
    llm = LLMClient(cfg.llm, mock_script=script)
    return cfg, RCAgent.build(cfg, llm, LocalEnvironment())


def snapshot_of(obs_text: str) -> str:
    import re

    m = re.search(r"\[snapshot: (\d{10})\]", obs_text)
    assert m, f"no snapshot in observation: {obs_text[:200]}"
    return m.group(1)


class TestHappyPath:
    def test_full_trajectory(self):
        script = [
            {"function": "runtime_log", "kwargs": {"job_id": "demo_es_conn_timeout"}},
            {"function": "log_agent", "kwargs": {"snapshot": "USE_PREV"}},
            {"function": "finalize", "kwargs": {
                "root_cause": "es timeout", "solution": "check cluster",
                "evidence": "SocketTimeoutException", "responsibility": "platform"}},
        ]
        cfg, agent = build_agent(_scripted(script))
        traj = agent.run(JOB)
        assert traj.status == STATUS_PASSED
        assert traj.result["responsibility"] == "platform"
        assert len(traj.records) == 3
        assert traj.invalid_actions == 0
        # 第2步确认用真实 snapshot key 调用 log_agent
        assert script[1]["kwargs"]["snapshot"].isdigit()

    def test_snapshot_used_as_expert_arg(self):
        """log_agent 收到的是上一步 observation 的真实快照键。"""
        captured = {}

        def scripted(messages, params):
            i = len([m for m in messages if m["role"] == "assistant"])
            if i == 0:
                return '{"function": "runtime_log", "kwargs": {"job_id": "demo_es_conn_timeout"}}'
            if i == 1:
                obs = next(m["content"] for m in reversed(messages) if m["role"] == "user"
                           and m["content"].startswith("Observation"))
                captured["snapshot"] = snapshot_of(obs)
                return '{"function": "log_agent", "kwargs": {"snapshot": "' + captured["snapshot"] + '"}}'
            return json.dumps({"function": "finalize", "kwargs": {
                "root_cause": "c", "solution": "s", "evidence": "e", "responsibility": "user"}})

        cfg, agent = build_agent(scripted)
        traj = agent.run(JOB)
        assert traj.passed
        # 快照键可被快照库解析(完整日志确实被保存)
        assert agent.store.get(captured["snapshot"]) is not None
        assert "SocketTimeoutException" in agent.store.get(captured["snapshot"])


class TestErrorPath:
    def test_unknown_tool_gets_feedback(self):
        def scripted(messages, params):
            n_assistant = len([m for m in messages if m["role"] == "assistant"])
            if n_assistant == 0:
                return '{"function": "no_such_tool", "kwargs": {}}'
            if n_assistant == 1:
                return '{"function": "runtime_log", "kwargs": {"job_id": "demo_es_conn_timeout"}}'
            return '{"function": "finalize", "kwargs": {"root_cause": "c", "solution": "s", "evidence": "e", "responsibility": "platform"}}'

        cfg, agent = build_agent(scripted)
        traj = agent.run(JOB)
        assert traj.passed  # 错误后仍能恢复并完成
        assert traj.invalid_actions == 1
        # 注入给 controller 的反馈包含错误说明
        assert "does not exist" in traj.records[0].observation_head

    def test_duplicate_call_feedback(self):
        def scripted(messages, params):
            n_assistant = len([m for m in messages if m["role"] == "assistant"])
            if n_assistant == 0:
                return '{"function": "runtime_log", "kwargs": {"job_id": "demo_es_conn_timeout"}}'
            if n_assistant == 1:
                return '{"function": "runtime_log", "kwargs": {"job_id": "demo_es_conn_timeout"}}'
            return '{"function": "finalize", "kwargs": {"root_cause": "c", "solution": "s", "evidence": "e", "responsibility": "platform"}}'

        cfg, agent = build_agent(scripted)
        traj = agent.run(JOB)
        assert traj.passed
        assert traj.invalid_actions == 1
        assert "already invoked" in traj.records[1].error

    def test_max_steps_exhausted_fails(self):
        def scripted(messages, params):
            return '{"function": "no_such_tool", "kwargs": {}}'

        cfg, agent = build_agent(scripted)
        traj = agent.run(JOB, max_steps=3)
        assert traj.status == STATUS_FAILED
        assert traj.result is None
        assert traj.invalid_actions == 3


def _scripted(actions: list[dict]):
    """按动作列表依次返回(支持 'USE_PREV' 占位: 用上一步观察的 snapshot)。"""
    idx = {"i": 0}

    def script(messages, params):
        i = idx["i"]
        idx["i"] += 1
        act = actions[min(i, len(actions) - 1)]
        if i < len(actions) and act["kwargs"].get("snapshot") == "USE_PREV":
            obs = next(m["content"] for m in reversed(messages) if m["role"] == "user"
                       and m["content"].startswith("Observation"))
            act["kwargs"]["snapshot"] = snapshot_of(obs)
        return json.dumps({"thought": "thinking", **act}, ensure_ascii=False)

    return script
