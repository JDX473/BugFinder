"""LLM 调用层:DeepSeek/OpenAI 兼容 API + mock 模式。

统一封装 chat 调用与成本计量。provider 为 mock 时使用可编程脚本响应,
用于无 API key 环境下验证 agent 循环逻辑。
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass
class Generation:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""
    extra: dict = field(default_factory=dict)  # penalty 迭代次数等


class LLMError(Exception):
    pass


class LLMClient:
    """chat 补全客户端。provider: deepseek | openai | mock。

    显式传入 mock_script 时强制 mock 模式(测试/脚本化验证用),
    即使 config 配置了真实 provider。
    """

    def __init__(self, cfg, mock_script: Callable[[list[dict], dict], str] | None = None):
        self._cfg = cfg
        self.provider = cfg.provider
        self.model = cfg.model
        self.mock_script = mock_script
        self._force_mock = mock_script is not None
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self._client = None
        if self.provider in ("deepseek", "openai") and not self._force_mock:
            import openai  # 延迟导入, mock 模式不需要

            api_key = os.environ.get(cfg.api_key_env or "OPENAI_API_KEY")
            if not api_key:
                raise LLMError(
                    f"provider={self.provider} 需要环境变量 {cfg.api_key_env or 'OPENAI_API_KEY'}"
                )
            kwargs = {"api_key": api_key, "timeout": cfg.get("timeout", 120)}
            if cfg.get("base_url"):
                kwargs["base_url"] = cfg.base_url
            self._client = openai.OpenAI(**kwargs)

    def chat(
        self,
        messages: list[dict],
        *,
        temperature: float = 0.0,
        top_p: float | None = None,
        frequency_penalty: float = 0.0,
        presence_penalty: float = 0.0,
        stream_cb: Callable[[str], None] | None = None,
    ) -> Generation:
        """单次 chat 调用;失败自动重试(指数退避)。

        stream_cb 给出时使用流式返回(逐块回调增量文本,Web 可视化用);
        mock 模式不流式(一次性返回)。
        """
        if self.provider == "mock" or self._force_mock:
            assert self.mock_script is not None
            text = self.mock_script(messages, dict(temperature=temperature))
            return Generation(text=text, model="mock")

        assert self._client is not None
        last_err: Exception | None = None
        for attempt in range(self._cfg.get("max_retries", 3) + 1):
            try:
                if stream_cb is not None:
                    stream = self._client.chat.completions.create(
                        model=self.model,
                        messages=messages,
                        temperature=temperature,
                        top_p=top_p,
                        frequency_penalty=frequency_penalty,
                        presence_penalty=presence_penalty,
                        stream=True,
                    )
                    parts: list[str] = []
                    p_tok = c_tok = 0
                    for chunk in stream:
                        delta = chunk.choices[0].delta.content if chunk.choices else None
                        if delta:
                            parts.append(delta)
                            stream_cb(delta)
                        if chunk.usage:
                            p_tok = chunk.usage.prompt_tokens or 0
                            c_tok = chunk.usage.completion_tokens or 0
                    text = "".join(parts)
                    self.total_prompt_tokens += p_tok
                    self.total_completion_tokens += c_tok
                    return Generation(text=text, prompt_tokens=p_tok,
                                      completion_tokens=c_tok, model=self.model)
                resp = self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=temperature,
                    top_p=top_p,
                    frequency_penalty=frequency_penalty,
                    presence_penalty=presence_penalty,
                )
                usage = resp.usage
                p_tok = usage.prompt_tokens if usage else 0
                c_tok = usage.completion_tokens if usage else 0
                self.total_prompt_tokens += p_tok
                self.total_completion_tokens += c_tok
                return Generation(
                    text=resp.choices[0].message.content or "",
                    prompt_tokens=p_tok,
                    completion_tokens=c_tok,
                    model=self.model,
                )
            except Exception as e:  # noqa: BLE001 — 网络/限流错误统一重试
                last_err = e
                if attempt < self._cfg.get("max_retries", 3):
                    time.sleep(2**attempt)
        raise LLMError(f"LLM 调用失败: {last_err}")

    def cost_estimate(self) -> dict:
        """累计 token 与估算费用(按配置单价)。"""
        price_in = self._cfg.get("price_per_1m_input", 0) / 1e6
        price_out = self._cfg.get("price_per_1m_output", 0) / 1e6
        cost = self.total_prompt_tokens * price_in + self.total_completion_tokens * price_out
        return {
            "prompt_tokens": self.total_prompt_tokens,
            "completion_tokens": self.total_completion_tokens,
            "estimated_cost_usd": round(cost, 4),
        }


def json_echo_mock(script: list[dict]) -> Callable[[list[dict], dict], str]:
    """把脚本动作序列转成 mock LLM:按消息轮次依次返回脚本中的 JSON action。

    script: [{"function": ..., "kwargs": ...}, ...],最后一个动作自动被包装为
    finalize(四项结果)以模拟正常退出;若脚本中已含 finalize 则原样使用。
    附加规则:当消息中出现 "Error" 反馈时,插入一次"反思性空动作"再继续,
    模拟错误处理后的行为变化(用于测试错误路径)。
    """
    from ..core.jsonregen import SENSITIVE_TO_CLEAN

    clean = SENSITIVE_TO_CLEAN
    step = {"i": 0}

    def _mock(messages, params):
        history = [m for m in messages if m["role"] == "assistant"]
        # 出现错误反馈时,先让 controller"反思"一轮(输出纯 thought,无动作)
        last_user = messages[-1]["content"] if messages else ""
        if "Error" in last_user and last_user.startswith("System"):
            return "Thought: 收到错误反馈,我需要调整策略。"
        action = script[min(step["i"], len(script) - 1)]
        step["i"] += 1
        action_str = json.dumps({"thought": "mock thought", **action}, ensure_ascii=False)
        return action_str

    return _mock
