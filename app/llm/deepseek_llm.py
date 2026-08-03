"""DeepSeek 生产 LLM 客户端（走 OpenAI 兼容接口）。

依赖：`pip install "openai>=1.40"`。未安装/未配置 key 时不导入本模块，
系统运行在 mock 模式（LLM 未启用时 ask_json 走 fallback）。
"""

from __future__ import annotations

from config.settings import settings


def create_deepseek_client():
    """根据配置构建 DeepSeekLLM 客户端；未配置 key 返回 None。"""
    if not settings.llm.enabled:
        return None
    try:
        from openai import OpenAI  # 延迟导入：可选依赖

        return DeepSeekLLM(
            api_key=settings.llm.api_key,
            base_url=settings.llm.base_url,
            model=settings.llm.model,
        )
    except ImportError:
        return None


class DeepSeekLLM:
    """DeepSeek OpenAI 兼容客户端，实现 LLMClient 协议。"""

    def __init__(self, api_key: str, base_url: str, model: str):
        self._api_key = api_key
        self._base_url = base_url
        self._model = model

    def complete(self, messages: list[dict[str, str]], temperature: float = 0.0) -> str:
        from openai import OpenAI

        client = OpenAI(api_key=self._api_key, base_url=self._base_url)
        resp = client.chat.completions.create(
            model=self._model,
            messages=messages,
            temperature=temperature,
        )
        content = resp.choices[0].message.content or ""
        return content
