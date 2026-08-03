"""ask_json.py 的测试：解析提取、schema 校验、重试、确定性兜底。"""

from __future__ import annotations

import json

import pytest

from app.llm.ask_json import _extract_json, ask_json

# 一个简单的测试 schema
SCHEMA = {
    "type": "object",
    "properties": {
        "scenario": {"type": "string", "enum": ["latency_spike", "error_rate_spike", "other"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["scenario", "confidence"],
    "additionalProperties": False,
}


class FakeLLM:
    """可编程返回预设输出序列的假模型。"""

    def __init__(self, outputs: list[str]):
        self.outputs = list(outputs)
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages: list[dict[str, str]], temperature: float = 0.0) -> str:
        self.calls.append(messages)
        if not self.outputs:
            return ""
        return self.outputs.pop(0)


def fallback() -> dict:
    return {"scenario": "other", "confidence": 0.1}


# ---------------------------------------------------------------- _extract_json

class TestExtractJson:
    def test_plain_json(self):
        assert _extract_json('{"a": 1}') == {"a": 1}

    def test_markdown_fence(self):
        assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_markdown_without_lang(self):
        # 无语言标注的围栏 + 单引号（非法 JSON）→ 提取失败应抛
        with pytest.raises(json.JSONDecodeError):
            _extract_json("```\n{'a': 1}\n```")

    def test_embedded_in_prose(self):
        assert _extract_json('结果是 {"a": 1} 完') == {"a": 1}

    def test_garbage_raises(self):
        with pytest.raises(json.JSONDecodeError):
            _extract_json("完全没有 JSON")


# ---------------------------------------------------------------- ask_json 主流程

class TestAskJson:
    def test_valid_json_no_retry(self):
        llm = FakeLLM(['{"scenario": "error_rate_spike", "confidence": 0.85}'])
        res = ask_json(llm, "system", "user", SCHEMA, fallback=fallback)
        assert res.ok is True
        assert res.retries == 0
        assert res.used_fallback is False
        assert res.data["scenario"] == "error_rate_spike"
        assert len(llm.calls) == 1

    def test_retry_recovers(self):
        # 第一次坏 JSON，第二次合法
        llm = FakeLLM(["这不是 JSON", '{"scenario": "latency_spike", "confidence": 0.6}'])
        res = ask_json(llm, "system", "user", SCHEMA, fallback=fallback)
        assert res.ok is True
        assert res.retries == 1
        assert res.data["scenario"] == "latency_spike"

    def test_schema_violation_retries(self):
        # 缺 required 字段 → 校验失败 → 重试
        llm = FakeLLM(['{"scenario": "latency_spike"}', '{"scenario": "other", "confidence": 0.9}'])
        res = ask_json(llm, "system", "user", SCHEMA, fallback=fallback)
        assert res.ok is True
        assert res.retries == 1

    def test_enum_violation_rejected(self):
        llm = FakeLLM(['{"scenario": "bogus", "confidence": 0.5}'])
        res = ask_json(llm, "system", "user", SCHEMA, fallback=fallback, max_retries=0)
        assert res.ok is False
        assert res.used_fallback is True
        assert res.data == fallback()

    def test_exhausted_retries_fallback(self):
        # 连续坏输出，重试耗尽走兜底
        llm = FakeLLM(["坏", "还是坏", "继续坏", "不修了"])
        res = ask_json(llm, "system", "user", SCHEMA, fallback=fallback, max_retries=3)
        assert res.ok is False
        assert res.used_fallback is True
        assert res.data == fallback()
        assert len(llm.calls) == 4  # 初试 + 3 次重试

    def test_fallback_failure(self):
        def bad_fallback():
            raise RuntimeError("fallback 崩了")

        llm = FakeLLM(["坏"])
        res = ask_json(llm, "system", "user", SCHEMA, fallback=bad_fallback, max_retries=0)
        assert res.ok is False
        assert res.data is None
        assert "fallback 失败" in (res.error or "")

    def test_error_included_in_retry_feedback(self):
        # 验证重试时把上次错误回喂给了模型
        llm = FakeLLM(["坏", '{"scenario": "other", "confidence": 0.5}'])
        res = ask_json(llm, "system", "user", SCHEMA, fallback=fallback)
        assert res.ok is True
        # 第二次调用的 messages 应包含反馈
        feedback = llm.calls[1][-1]["content"]
        assert "未通过校验" in feedback

    def test_markdown_output_accepted(self):
        llm = FakeLLM(['```json\n{"scenario": "other", "confidence": 0.2}\n```'])
        res = ask_json(llm, "system", "user", SCHEMA, fallback=fallback)
        assert res.ok is True
        assert res.data["scenario"] == "other"
