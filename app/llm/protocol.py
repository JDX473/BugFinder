"""LLM 访问协议。所有组件通过 `LLMClient` 协议与模型交互，便于 mock 测试。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class LLMClient(Protocol):
    """模型客户端最小接口。

    生产实现：DeepSeekOpenAILLM（走 OpenAI 兼容接口）。
    测试实现：FakeLLM（可编程返回坏 JSON / 缺字段 JSON / 纯文本 / 合法 JSON）。
    """

    def complete(self, messages: list[dict[str, str]], temperature: float = 0.0) -> str:
        """给定消息列表，返回模型生成的文本。"""
        ...
