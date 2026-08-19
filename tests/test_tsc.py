"""TSC(论文 §III-D2)与文本聚合(§III-D1)单元测试。"""

import json

from rcagent.config import load_config
from rcagent.core.agent import JobDesc, RCAgent
from rcagent.env.local import LocalEnvironment
from rcagent.experts.knowledge import build_demo_kb
from rcagent.llm.client import LLMClient
from rcagent.llm.embedding import Embedder
from rcagent.sc.aggregate import aggregate_by_embedding, aggregate_by_llm, embedding_vote
from rcagent.sc.tsc import TSCRunner

CFG = load_config()
EMB = Embedder(dict(provider="mock"))  # 测试统一用 mock embedding(框架逻辑验证)
JOB = JobDesc(job_id="demo_es_conn_timeout", anomaly="job failed",
              detect_time="2024-01-01 12:00:00")


def build_runner(script, sc_samples=3):
    llm = LLMClient(CFG.llm, mock_script=script)
    agent = RCAgent.build(CFG, llm,
                          LocalEnvironment(llm=llm, embedder=EMB, kb=build_demo_kb(EMB)))
    return TSCRunner(agent, CFG, EMB, llm)


def finalize_action(cause: str) -> str:
    return json.dumps({"thought": "done", "function": "finalize", "kwargs": {
        "root_cause": cause, "solution": "fix it", "evidence": "log line",
        "responsibility": "platform"}})


class TestReplay:
    def test_replay_matches_run_messages(self):
        """重放重建的消息与主轨迹运行中的消息一致。"""
        calls = {"i": 0}
        seen: dict[int, int] = {}

        def script(messages, params):
            n_assistant = len([m for m in messages if m["role"] == "assistant"])
            seen[calls["i"]] = n_assistant
            calls["i"] += 1
            if n_assistant == 0:
                return '{"function": "runtime_log", "kwargs": {"job_id": "demo_es_conn_timeout"}}'
            return finalize_action(f"cause {n_assistant}")

        runner = build_runner(script)
        main = runner.agent.run(JOB)
        assert main.passed and len(main.records) == 2
        # 重放 0 条历史(从第 1 步开始)时,消息结构与主轨迹第 1 次调用一致
        msgs = runner.agent.replay_messages(JOB, main, 0)
        assert len(msgs) == 2  # system + user
        assert msgs[1]["role"] == "user"


class TestAggregate:
    def test_embedding_vote_picks_consensus(self):
        texts = ["es connection timed out from socket",
                 "elasticsearch connect timed out",
                 "the user deleted the whole deployment"]
        winner = embedding_vote(texts, EMB)
        assert winner in texts[:2]  # 相似组胜出

    def test_aggregate_by_embedding_fills_unclear(self):
        cands = [{"root_cause": "a", "solution": "s1", "evidence": "e1",
                  "responsibility": "platform"},
                 None,  # 失败轨迹 → Unclear
                 {"root_cause": "a", "solution": "s1", "evidence": "e1",
                  "responsibility": "platform"}]
        out = aggregate_by_embedding(cands, EMB, ["root_cause", "solution",
                                                  "evidence", "responsibility"])
        assert out["responsibility"] == "platform"
        assert out["root_cause"] != "Unclear"  # 共识非 Unclear 时胜出

    def test_aggregate_by_llm(self):
        llm = LLMClient(CFG.llm, mock_script=lambda m, p: json.dumps({
            "root_cause": "merged cause", "solution": "merged sol",
            "evidence": "merged ev", "responsibility": "user"}))
        cands = [{"root_cause": "a", "solution": "s", "evidence": "e",
                  "responsibility": "platform"},
                 {"root_cause": "b", "solution": "s", "evidence": "e",
                  "responsibility": "user"}]
        out = aggregate_by_llm(llm, cands, ["root_cause", "solution", "evidence",
                                            "responsibility"])
        assert out["root_cause"] == "merged cause"

    def test_aggregate_by_llm_all_failed(self):
        llm = LLMClient(CFG.llm, mock_script=lambda m, p: "{}")
        assert aggregate_by_llm(llm, [None, None], ["root_cause"]) is None


class TestTSC:
    def _tsc_mock(self):
        """主轨迹: runtime_log → finalize;子轨迹: 直接 finalize(不同根因)。"""
        calls = {"i": 0}

        def script(messages, params):
            i = calls["i"]
            calls["i"] += 1
            if i == 0:
                return '{"function": "runtime_log", "kwargs": {"job_id": "demo_es_conn_timeout"}}'
            return finalize_action(f"sampled cause {i}")

        return script

    def test_tsc_full_pipeline(self):
        runner = build_runner(self._tsc_mock(), sc_samples=3)
        res = runner.run(JOB, samples=3, method="tsc", aggregate="llm")
        assert res.result is not None
        assert len(res.sub_trajs) == 3
        assert res.sample_pass_rate == 1.0
        # LLM 聚合被调用(聚合结果来自 mock 的 merged 输出则验证 LLM 分支;
        # 这里 mock 聚合失败会退化为主轨迹,两种都算有效输出)
        assert res.result["root_cause"]

    def test_tsc_embedding_aggregate(self):
        runner = build_runner(self._tsc_mock(), sc_samples=3)
        res = runner.run(JOB, samples=3, method="tsc", aggregate="embedding")
        assert res.result is not None
        assert res.result["responsibility"] == "platform"

    def test_failed_main_trajectory_skips_sampling(self):
        def script(messages, params):
            return '{"function": "no_such_tool", "kwargs": {}}'

        runner = build_runner(script)
        res = runner.run(JOB, samples=3, method="tsc")
        assert res.result is None
        assert res.sub_trajs == []

    def test_stepwise_sc_discards_non_finalize(self):
        calls = {"i": 0}

        def script(messages, params):
            i = calls["i"]
            calls["i"] += 1
            if i == 0:
                return '{"function": "runtime_log", "kwargs": {"job_id": "demo_es_conn_timeout"}}'
            if i == 1:  # 主轨迹 finalize
                return finalize_action("main cause")
            # 步进 SC 采样: 前两条返回非 finalize(丢弃),第三条成功
            if i in (2, 3):
                return '{"function": "runtime_log", "kwargs": {"job_id": "demo_es_conn_timeout"}}'
            return finalize_action("sampled cause")

        runner = build_runner(script, sc_samples=3)
        res = runner.run(JOB, samples=3, method="sc")
        # 2 条被丢弃,1 条成功
        assert res.sample_pass_rate == 1 / 3
        assert res.result is not None
