"""Log Expert Agent(Algorithm 1)单元测试: 幻觉过滤 / 分块分析 / 总结。"""

import json

from rcagent.config import load_config
from rcagent.experts.knowledge import build_demo_kb
from rcagent.experts.log_agent import LogExpertAgent
from rcagent.llm.client import LLMClient
from rcagent.llm.embedding import Embedder

CFG = load_config()
EMB = Embedder(dict(provider="mock"))  # 测试统一用 mock embedding(框架逻辑验证)
KB = build_demo_kb(EMB)

SAMPLE_CHUNK = "\n".join([
    "2024-01-01 09:02:52 ERROR org.apache.flink.connector.elasticsearch - "
    "SocketTimeoutException: Connect timed out [Elasticsearch:9200]",
    "2024-01-01 09:02:53 ERROR org.apache.flink.connector.elasticsearch - "
    "Caused by: java.net.SocketTimeoutException: Read timed out",
    "2024-01-01 09:02:54 INFO org.apache.flink.runtime - retrying 2",
])


def make_agent(script):
    llm = LLMClient(CFG.llm, mock_script=script)
    return LogExpertAgent(llm, EMB, KB, CFG)


class TestEvidenceFilter:
    def test_verbatim_evidence_accepted(self):
        agent = make_agent(lambda m, p: "{}")
        line = SAMPLE_CHUNK.splitlines()[0]
        assert agent._evidence_ok(line, SAMPLE_CHUNK)

    def test_fabricated_evidence_rejected(self):
        agent = make_agent(lambda m, p: "{}")
        assert not agent._evidence_ok(
            "The user misconfigured the password on the dashboard", SAMPLE_CHUNK)

    def test_evidence_from_other_log_rejected(self):
        agent = make_agent(lambda m, p: "{}")
        other = ("2024-01-01 09:00:00 ERROR org.apache.flink.runtime.checkpoint - "
                 "Checkpoint expired")
        # 编辑距离大 -> 拒绝
        assert not agent._evidence_ok(other, SAMPLE_CHUNK)


class TestChunkAnalysis:
    def _chunk_copying_mock(self, messages, params):
        """mock 从 prompt 的 Target log chunk 中复制第一行作为证据。"""
        user = messages[-1]["content"]
        chunk = user.split("Target log chunk:")[1].split('Respond with a JSON')[0]
        line = chunk.strip().splitlines()[0]
        return json.dumps({
            "interpretation": "Elasticsearch connection timed out",
            "evidence": line,
        })

    def test_chunk_analysis_accepted(self):
        agent = make_agent(self._chunk_copying_mock)
        analysis = agent._analyze_chunk(SAMPLE_CHUNK)
        assert analysis is not None
        assert "Elasticsearch" in analysis.interpretation
        assert "SocketTimeoutException" in analysis.evidence

    def test_unparsable_chunk_dropped(self):
        agent = make_agent(lambda m, p: "not json")
        assert agent._analyze_chunk(SAMPLE_CHUNK) is None

    def test_hallucination_filtered_end_to_end(self):
        def lying_mock(messages, params):
            return json.dumps({
                "interpretation": "user deleted the deployment",
                "evidence": "The user deleted the deployment from the console",
            })
        agent = make_agent(lying_mock)
        out = agent.run(SAMPLE_CHUNK)
        assert "no reliable analysis" in out  # 全部被幻觉过滤丢弃


class TestFullRun:
    def test_run_with_valid_analysis(self):
        def mock(messages, params):
            user = messages[-1]["content"]
            if "Target log chunk" in user:
                chunk = user.split("Target log chunk:")[1].split("Respond with a JSON")[0]
                line = chunk.strip().splitlines()[0]
                return json.dumps({"interpretation": "es timeout", "evidence": line})
            return json.dumps({"interpretation": "merged: es timeout",
                               "evidence": "SocketTimeoutException line"})
        agent = make_agent(mock)
        out = agent.run(SAMPLE_CHUNK)
        assert "interpretation:" in out and "evidence:" in out
        assert "merged" in out  # 总结阶段被调用

    def test_empty_log(self):
        agent = make_agent(lambda m, p: "{}")
        out = agent.run("")
        assert "insufficient" in out

    def test_kb_retrieves_related_example(self):
        hits = KB.search(SAMPLE_CHUNK, top_k=1)
        assert hits and "SocketTimeoutException" in hits[0][0]
