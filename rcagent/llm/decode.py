"""解码策略管理(论文 §IV-A):greedy 默认 + 自适应重复惩罚 + SC 采样。

- greedy: temperature=0
- sampling: temperature=0.9, top_p=0.6(SC 采样,对齐 Vicuna 默认配置)
- adaptive penalty: 生成 token 数超过阈值(如 4096)时,重启生成并
  +0.5 frequency/presence penalty,可迭代(论文: applied iteratively)。
  OpenAI 兼容协议用 presence_penalty+frequency_penalty 近似 vLLM 的
  repetition_penalty(FR-09 降级策略)。
"""

from __future__ import annotations

from ..config import Config
from .client import LLMClient, Generation


def generate(
    client: LLMClient,
    cfg: Config,
    messages: list[dict],
    *,
    mode: str = "greedy",
    stream_cb: Callable[[str], None] | None = None,
) -> Generation:
    """按模式生成;greedy/sampling 之外自动应用自适应惩罚。

    stream_cb 给出时流式返回(逐块回调增量文本,Web 可视化用)。
    """
    if mode == "greedy":
        return _generate_with_penalty(client, cfg, messages, temperature=0.0,
                                      stream_cb=stream_cb)
    if mode == "sampling":
        decoding = cfg.get("decoding") or cfg
        s = decoding.get("sampling") or {}
        return _generate_with_penalty(
            client,
            cfg,
            messages,
            temperature=s.get("temperature", 0.9),
            top_p=s.get("top_p", 0.6),
            stream_cb=stream_cb,
        )
    raise ValueError(f"unknown decode mode: {mode}")


def _generate_with_penalty(
    client: LLMClient,
    cfg: Config,
    messages: list[dict],
    *,
    temperature: float,
    top_p: float | None = None,
    stream_cb: Callable[[str], None] | None = None,
) -> Generation:
    threshold = cfg.get("penalty_threshold_tokens", 4096)
    max_iter = cfg.get("max_penalty_iterations", 3)
    step = cfg.get("penalty_step", 0.5)

    freq = pres = 0.0
    gen = client.chat(
        messages, temperature=temperature, top_p=top_p, frequency_penalty=freq,
        presence_penalty=pres, stream_cb=stream_cb,
    )
    for _ in range(max_iter):
        if gen.completion_tokens <= threshold:
            break
        # 疑似循环: 惩罚升级后重启生成
        freq += step
        pres += step
        gen.extra["penalty_escalations"] = gen.extra.get("penalty_escalations", 0) + 1
        gen = client.chat(
            messages,
            temperature=temperature,
            top_p=top_p,
            frequency_penalty=freq,
            presence_penalty=pres,
            stream_cb=stream_cb,
        )
    return gen
