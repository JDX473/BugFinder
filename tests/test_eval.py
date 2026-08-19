"""评估体系(§IV-C)单元测试: 指标 / 归一化 / LLM 评估器 / 报表。"""

import json

from rcagent.config import load_config
from rcagent.eval.judge import compute_win_rate, judge_score, judge_win
from rcagent.eval.metrics import (
    UNCLEAR,
    MetricUnavailable,
    compute,
    emb_score,
    meteor,
    norm_scores,
)
from rcagent.eval.report import MethodEval, render_table, render_trajectory_stats
from rcagent.llm.client import LLMClient
from rcagent.llm.embedding import Embedder

CFG = load_config()
EMB = Embedder(dict(provider="mock"))  # 测试统一用 mock embedding(框架逻辑验证)


class TestMetrics:
    def test_embscore_perfect_match(self):
        scores = emb_score(["es connection timeout"], ["es connection timeout"], EMB)
        assert scores[0] > 0.99

    def test_embscore_different_texts_lower(self):
        a = emb_score(["elasticsearch connect timed out"], ["elasticsearch connect timed out"], EMB)
        b = emb_score(["elasticsearch connect timed out"], ["user deleted the job"], EMB)
        assert b[0] < a[0]

    def test_meteor_perfect_match(self):
        scores = meteor(["the cat sat on the mat"], ["the cat sat on the mat"])
        assert scores[0] > 0.99

    def test_meteor_unrelated_low(self):
        scores = meteor(["the cat sat on the mat"], ["zzz qqq www eee rrr"])
        assert scores[0] < 0.2

    def test_norm_score_formula(self):
        # (s - b) / (1 - b)
        out = norm_scores([0.8], [0.5])
        assert abs(out[0] - (0.8 - 0.5) / (1 - 0.5)) < 1e-9

    def test_compute_unknown_metric(self):
        import pytest

        with pytest.raises(ValueError):
            compute("nope", ["a"], ["b"], EMB)

    def test_unclear_baseline(self):
        assert UNCLEAR == "Unclear"


class TestJudge:
    def test_score_extraction(self):
        llm = LLMClient(CFG.llm, mock_script=lambda m, p: "Score: 8")
        assert judge_score(llm, "correctness", "p", "r") == 8.0

    def test_score_clamped(self):
        llm = LLMClient(CFG.llm, mock_script=lambda m, p: "Score: 15")
        assert judge_score(llm, "helpfulness", "p", "r") == 10.0

    def test_win_verdict(self):
        llm = LLMClient(CFG.llm, mock_script=lambda m, p: "BETTER")
        assert judge_win(llm, "a", "b", "r") == "BETTER"

    def test_win_rate(self):
        calls = {"i": 0}

        def script(m, p):
            v = ["BETTER", "WORSE", "EQUAL"][calls["i"] % 3]
            calls["i"] += 1
            return v

        llm = LLMClient(CFG.llm, mock_script=script)
        rate, verdicts = compute_win_rate(llm, ["a1", "a2", "a3"],
                                          ["b1", "b2", "b3"], ["r1", "r2", "r3"])
        assert rate == 1 / 3
        assert verdicts == ["BETTER", "WORSE", "EQUAL"]


class TestReport:
    def test_table_renders(self):
        me = MethodEval(name="rcagent")
        me.add_scores("root_cause", "METEOR", [1.0, 0.5])
        me.pass_rate, me.invalid_rate, me.avg_steps = 0.9, 0.05, 6.0
        t = render_table([me], ["METEOR"])
        assert "rcagent" in t and "METEOR" in t and "±" in t
        s = render_trajectory_stats([me])
        assert "Pass Rate" in s and "0.90" in s
