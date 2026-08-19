"""JsonRegen(Algorithm 2)单元测试:破坏性 JSON 修复。"""

import pytest

from rcagent.core.jsonregen import (
    parse_json_or_none,
    protect_json_objects,
    sanitize_prompt,
)
from rcagent.llm.client import LLMClient
from rcagent.config import load_config


class TestParseJsonOrNone:
    def test_clean_json(self):
        out = parse_json_or_none('{"function": "runtime_log", "kwargs": {"job_id": "j1"}}')
        assert out == {"function": "runtime_log", "kwargs": {"job_id": "j1"}}

    def test_json_wrapped_in_prose(self):
        out = parse_json_or_none(
            'Thought: let me check the log.\nFunction: {"function": "runtime_log", "kwargs": {"job_id": "j1"}}\n'
        )
        assert out["function"] == "runtime_log"

    def test_trailing_comma(self):
        out = parse_json_or_none('{"function": "finalize", "kwargs": {"a": 1,}}')
        assert out["kwargs"] == {"a": 1}

    def test_unescaped_newline_in_string(self):
        out = parse_json_or_none('{"function": "x", "kwargs": {"msg": "line1\nline2"}}')
        assert out["kwargs"]["msg"] == "line1\nline2"

    def test_single_quote_json(self):
        # LLM 受净化影响把字符串引号输出为单引号
        out = parse_json_or_none("{'function': 'runtime_log', 'kwargs': {'job_id': 'j1'}}")
        assert out == {"function": "runtime_log", "kwargs": {"job_id": "j1"}}

    def test_no_json(self):
        assert parse_json_or_none("just a thought, no action") is None

    def test_truncated_json(self):
        assert parse_json_or_none('{"function": "runtime_log", "kwargs": {"job_id": "j1"') is None

    def test_multiline_json_with_single_quoted_values(self):
        """回归: 多行 JSON 结构 + 字符串值内单引号(LLM 常见输出形态)。"""
        out = parse_json_or_none(
            '{\n  "summary": "checks \'SINK_CONN_ERROR\' events",\n'
            '  "suggested_classes": ["FlinkLifecycleMapper"]\n}')
        assert out is not None
        assert out["summary"] == "checks 'SINK_CONN_ERROR' events"
        assert out["suggested_classes"] == ["FlinkLifecycleMapper"]

    def test_multiline_structure_newlines_preserved(self):
        """回归: 结构层换行不被误伤(此前正则把 { 后换行替换为字面 \\n)。"""
        out = parse_json_or_none('{\n  "a": "b"\n}')
        assert out == {"a": "b"}


class TestPromptSanitize:
    def test_protect_json_objects(self):
        text = 'history: {"function": "runtime_log"} and plain "quoted" text'
        protected, blocks = protect_json_objects(text)
        assert blocks == ['{"function": "runtime_log"}']
        # 真实 JSON 被占位符替换;其余文本(含引号)原样保留(引号替换是 sanitize 的职责)
        assert '"quoted"' in protected
        assert "runtime_log" not in protected
        sanitized = sanitize_prompt(text)
        assert '{"function": "runtime_log"}' in sanitized
        assert "'quoted'" in sanitized

    def test_sanitize_replaces_only_non_json(self):
        prompt = 'Previous action: {"function": "runtime_log"}\nNow call runtime "log" please'
        out = sanitize_prompt(prompt)
        # 真实 JSON 对象保持双引号
        assert '{"function": "runtime_log"}' in out
        # 非 JSON 文本的引号被替换为单引号
        assert "'log'" in out

    def test_clean_json_after_sanitize(self):
        # 历史 JSON + 新指令文本,净化后新生成指令中的方括号/花括号被替换
        prompt = 'History: {"function": "runtime_log"}\nUse [log] tool now, format {"function": ...}'
        out = sanitize_prompt(prompt)
        assert out.count('{"function": "runtime_log"}') == 1
        assert "<:log]" in out  # [log] -> <:log](替换表仅含左括号,对齐论文 digraph)


class TestJsonRegenFull:
    def test_regen_with_clean_llm(self):
        cfg = load_config()
        llm = LLMClient(cfg.llm, mock_script=lambda m, p: '{"function": "finalize"}')
        from rcagent.core.jsonregen import json_regen

        out = json_regen(llm, "do it")
        assert out == {"function": "finalize"}

    def test_regen_recovers_via_yaml_roundtrip(self):
        """LLM 先给出坏 JSON,经 YAML->JSON 恢复后返回正确结构。"""
        cfg = load_config()
        responses = iter([
            '{"function": "finalize", "kwargs": {"a": "broken',  # 截断,不可解析
            'function: finalize\nkwargs:\n  a: "ok"',              # YAML 提取
            '{"function": "finalize", "kwargs": {"a": "ok"}}',     # 恢复
        ])
        llm = LLMClient(cfg.llm, mock_script=lambda m, p: next(responses))
        from rcagent.core.jsonregen import json_regen

        out = json_regen(llm, "do it", retries=3)
        assert out == {"function": "finalize", "kwargs": {"a": "ok"}}

    def test_regen_gives_up(self):
        cfg = load_config()
        llm = LLMClient(cfg.llm, mock_script=lambda m, p: "not json at all")
        from rcagent.core.jsonregen import json_regen

        assert json_regen(llm, "do it", retries=2) is None
