"""消融变体测试(论文 §V-B 的 RQ2 基础): react 基线 / 单组件消融开关。"""

import json

from rcagent.config import load_config
from rcagent.core.agent import JobDesc, RCAgent
from rcagent.env.local import LocalEnvironment
from rcagent.llm.client import LLMClient

CFG = load_config()
JOB = JobDesc(job_id="demo_es_conn_timeout", anomaly="job failed",
              detect_time="2024-01-01 12:00:00")


def build_variant(variant):
    llm = LLMClient(CFG.llm, mock_script=lambda m, p: '{"function": "finalize", "kwargs": '
                    '{"root_cause": "c", "solution": "s", "evidence": "e", '
                    '"responsibility": "platform"}}')
    return RCAgent.build(CFG, llm, LocalEnvironment(), variant=variant)


class TestVariantConfig:
    def test_react_disables_everything(self):
        agent = build_variant("react")
        assert "log_agent" not in agent.registry.names()  # 无专家工具
        assert not agent.use_regen                          # 无 JsonRegen
        assert agent.obs_mode == "no_obsk"                  # 无 OBSK
        assert not agent.detector.enabled                   # 无错误处理

    def test_full_has_everything(self):
        agent = build_variant("full")
        assert "log_agent" in agent.registry.names()
        assert agent.use_regen
        assert agent.obs_mode == "full"
        assert agent.detector.enabled

    def test_no_experts(self):
        agent = build_variant("no_experts")
        assert "log_agent" not in agent.registry.names()
        assert agent.use_regen and agent.obs_mode == "full"  # 其余机制保留

    def test_no_jsonregen(self):
        assert not build_variant("no_jsonregen").use_regen

    def test_obs_modes(self):
        assert build_variant("no_obsk").obs_mode == "no_obsk"
        assert build_variant("no_obs_head").obs_mode == "no_obs_head"

    def test_unknown_variant_rejected(self):
        import pytest

        with pytest.raises(ValueError):
            build_variant("nope")


class TestVariantBehavior:
    def test_react_trajectory_runs(self):
        """react 变体可端到端运行(无 OBSK: 观察直接截断)。"""
        calls = {"i": 0}

        def script(messages, params):
            i = calls["i"]
            calls["i"] += 1
            if i == 0:
                return '{"function": "runtime_log", "kwargs": {"job_id": "demo_es_conn_timeout"}}'
            return json.dumps({"function": "finalize", "kwargs": {
                "root_cause": "c", "solution": "s", "evidence": "e",
                "responsibility": "platform"}})

        llm = LLMClient(CFG.llm, mock_script=script)
        agent = RCAgent.build(CFG, llm, LocalEnvironment(), variant="react")
        traj = agent.run(JOB)
        assert traj.passed
        # no_obsk 观察中无 snapshot 标记,只有直接截断标记
        assert "[snapshot:" not in traj.records[0].observation_head
        assert "[truncated]" in traj.records[0].observation_head

    def test_no_obs_head_gives_snapshot_only(self):
        calls = {"i": 0}

        def script(messages, params):
            i = calls["i"]
            calls["i"] += 1
            if i == 0:
                return '{"function": "runtime_log", "kwargs": {"job_id": "demo_es_conn_timeout"}}'
            return json.dumps({"function": "finalize", "kwargs": {
                "root_cause": "c", "solution": "s", "evidence": "e",
                "responsibility": "platform"}})

        llm = LLMClient(CFG.llm, mock_script=script)
        agent = RCAgent.build(CFG, llm, LocalEnvironment(), variant="no_obs_head")
        traj = agent.run(JOB)
        assert traj.passed
        obs = traj.records[0].observation_head
        assert "[snapshot:" in obs and len(obs) < 100  # 只有快照键,无 head

    def test_react_duplicate_calls_not_blocked(self):
        """react 变体关闭错误处理: 重复调用不会被拦截(Invalid Rate 上升来源)。"""
        def script(messages, params):
            return '{"function": "runtime_log", "kwargs": {"job_id": "demo_es_conn_timeout"}}'

        llm = LLMClient(CFG.llm, mock_script=script)
        agent = RCAgent.build(CFG, llm, LocalEnvironment(), variant="react")
        traj = agent.run(JOB, max_steps=3)
        assert traj.invalid_actions == 0  # 无错误处理,重复调用不计数
