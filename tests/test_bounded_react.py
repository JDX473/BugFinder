"""bounded_react.py 的测试：有界循环、工具调用、final_answer、LLM 失败兜底、证据压制。"""

from __future__ import annotations

import pytest

from app.graph.bounded_react import ReActResult, run_bounded_react


class FakeLLM:
    def __init__(self, outputs: list[str]):
        self.outputs = list(outputs)
        self.calls = 0

    def complete(self, messages: list[dict[str, str]], temperature: float = 0.0) -> str:
        self.calls += 1
        return self.outputs.pop(0) if self.outputs else ""


class QueryTool:
    name = "query_logs"
    description = "查询日志"
    args_schema = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}

    def __init__(self):
        self.runs = 0

    def run(self, args: dict) -> str:
        self.runs += 1
        return f"found logs for {args.get('q', '?')}"


# ---------------------------------------------------------------- 确定性优先

def test_no_llm_uses_fallback():
    """无 LLM → 直接跑确定性 fallback，不调 LLM。"""
    r = run_bounded_react(task="t", tools=[], llm=None, fallback=lambda: {"cause": "deterministic"})
    assert not r.used_llm
    assert r.conclusion == {"cause": "deterministic"}
    assert r.steps == []


def test_no_llm_no_fallback():
    r = run_bounded_react(task="t", tools=[], llm=None)
    assert r.conclusion == {"status": "no_llm"}


# ---------------------------------------------------------------- 有界循环

def test_tool_then_final_answer():
    """LLM 先调工具再 final_answer → 采用 LLM 结论。"""
    tool = QueryTool()
    llm = FakeLLM(
        [
            '{"tool_name": "query_logs", "args": {"q": "ERROR"}, "confidence": 0.9}',
            '{"final_answer": {"cause": "payment timeout"}, "confidence": 0.9}',
        ]
    )
    r = run_bounded_react(task="找根因", tools=[tool], llm=llm, fallback=lambda: {"cause": "fb"})
    assert r.used_llm
    assert r.conclusion == {"cause": "payment timeout"}
    assert tool.runs == 1
    assert len(r.steps) == 2
    assert llm.calls == 2


def test_max_iters_bounded():
    """循环有上限：LLM 一直调工具，到 max_iters 强制收敛。"""
    tool = QueryTool()
    llm = FakeLLM(['{"tool_name": "query_logs", "args": {"q": "x"}, "confidence": 0.9}'] * 10)
    r = run_bounded_react(task="t", tools=[tool], llm=llm, fallback=lambda: {"cause": "fb"}, max_iters=3)
    # 最多调 3 次工具，之后收敛到 fallback
    assert tool.runs == 3
    assert not r.used_llm
    assert r.conclusion == {"cause": "fb"}


# ---------------------------------------------------------------- 失败兜底

def test_bad_json_falls_back():
    """LLM 输出坏 JSON → 确定性兜底。"""
    llm = FakeLLM(["不是 JSON", "还是坏", "继续坏", "不修了"])
    r = run_bounded_react(task="t", tools=[], llm=llm, fallback=lambda: {"cause": "fb"})
    assert not r.used_llm
    assert r.conclusion == {"cause": "fb"}


def test_low_confidence_falls_back():
    """final_answer 低置信（<0.5）→ 不用 LLM 结论，走兜底。"""
    llm = FakeLLM(['{"final_answer": {"cause": "guess"}, "confidence": 0.2}'])
    r = run_bounded_react(task="t", tools=[], llm=llm, fallback=lambda: {"cause": "fb"})
    assert not r.used_llm
    assert r.conclusion == {"cause": "fb"}


def test_unknown_tool_falls_back():
    """LLM 调未知工具 → 非法动作，中断循环落兜底。"""
    llm = FakeLLM(['{"tool_name": "nonexistent", "args": {}, "confidence": 0.9}'])
    r = run_bounded_react(task="t", tools=[QueryTool()], llm=llm, fallback=lambda: {"cause": "fb"})
    assert not r.used_llm
    assert r.conclusion == {"cause": "fb"}


def test_tool_exception_does_not_crash():
    """工具执行抛异常 → 观察记为失败，循环继续。"""
    class BoomTool(QueryTool):
        def run(self, args):
            raise RuntimeError("boom")

    llm = FakeLLM(
        [
            '{"tool_name": "query_logs", "args": {"q": "x"}, "confidence": 0.9}',
            '{"final_answer": {"cause": "ok"}, "confidence": 0.9}',
        ]
    )
    r = run_bounded_react(task="t", tools=[BoomTool()], llm=llm, fallback=lambda: {"cause": "fb"})
    assert r.used_llm  # 工具失败后 LLM 仍能给出结论
    assert r.conclusion == {"cause": "ok"}
    assert "失败" in r.steps[0]["observation"] or "boom" in r.steps[0]["observation"]


class ThrowingLLM:
    def complete(self, messages, temperature=0.0):
        raise TimeoutError("模拟超时")


def test_llm_exception_falls_back():
    llm = ThrowingLLM()
    r = run_bounded_react(task="t", tools=[], llm=llm, fallback=lambda: {"cause": "fb"})
    assert not r.used_llm
    assert r.conclusion == {"cause": "fb"}


# ---------------------------------------------------------------- 证据压制

def test_to_evidence_packs_conclusion():
    """结论压成 Evidence：中间步骤进 payload，summary 是摘要。"""
    r = ReActResult(
        conclusion={"cause": "payment timeout"},
        used_llm=True,
        steps=[{"iter": 0, "action": "tool:query_logs"}],
        basis="ReAct（1 步）",
    )
    ev = r.to_evidence("ev-1", "metric", "react")
    assert ev.evidence_id == "ev-1"
    assert ev.type == "metric"
    assert "ReAct" in ev.summary
    assert ev.payload["conclusion"] == {"cause": "payment timeout"}
    assert ev.payload["used_llm"] is True
    assert len(ev.payload["steps"]) == 1


# ---------------------------------------------------------------- 评审修复回归

class TestReviewFixes:
    def test_observation_truncated(self):
        """评审 #4/#15：工具长观察被截断（不进下一轮 prompt）。"""
        class BigTool(QueryTool):
            def run(self, args):
                return "x" * 5000  # 超长观察

        llm = FakeLLM(
            [
                '{"tool_name": "query_logs", "args": {"q": "x"}, "confidence": 0.9}',
                '{"final_answer": {"cause": "ok"}, "confidence": 0.9}',
            ]
        )
        r = run_bounded_react(task="t", tools=[BigTool()], llm=llm, fallback=lambda: {"cause": "fb"})
        # 步骤记录里的 observation 已截断（≤500 + 省略号）
        assert r.used_llm
        obs = r.steps[0]["observation"]
        assert len(obs) < 600
        assert obs.endswith("…")

    def test_final_answer_prefers_over_tool(self):
        """评审 #26：LLM 同时带 tool_name + final_answer → 视为结束调查。"""
        llm = FakeLLM(['{"tool_name": "query_logs", "final_answer": {"cause": "done"}, "confidence": 0.9}'])
        tool = QueryTool()
        r = run_bounded_react(task="t", tools=[tool], llm=llm, fallback=lambda: {"cause": "fb"})
        assert r.used_llm
        assert r.conclusion == {"cause": "done"}
        assert tool.runs == 0  # 没被误当成工具调用
