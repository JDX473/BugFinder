"""DeepSeek 结构化输出 shim（PRD §6.4 硬约束 3 的落地）。

DeepSeek OpenAI 兼容 API 不支持 `response_format=json_schema`，因此统一走：

    提示词强约束 JSON → json.loads 解析 → jsonschema 校验
    → 失败重试（≤3 次，把上次解析错误回喂给模型）
    → 仍失败则确定性兜底（调用方提供 fallback）

所有 LLM 结构化产出步骤（抽取/分类/假设生成/排序）都经过此入口，
不在别处裸调 LLM，保证"模型输出必须是合法且符合 schema 的 JSON"。

模型通过 `LLMClient` 协议注入：生产用 DeepSeekOpenAILLM，测试用 FakeLLM。
"""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping

import jsonschema  # type: ignore

from app.llm.protocol import LLMClient

# 引导模型只输出 JSON（不含 markdown 围栏）。失败时此提示追加在 system 之后。
_JSON_SYSTEM_PROMPT = (
    "你是一个严格遵循输出格式的助手。只输出一个合法的 JSON 对象，"
    "不要包含任何 Markdown 代码围栏、解释或前后缀文字。"
)


def _extract_json(text: str) -> Any:
    """从模型输出中提取 JSON。优先 json.loads 全文；失败时尝试剥离围栏/取首个 {。"""
    text = text.strip()
    # 尝试全文解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 剥离 ```json ... ``` 围栏
    if text.startswith("```"):
        body = text.strip("`").strip()
        if body.lower().startswith("json"):
            body = body[4:].strip()
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            pass
    # 取首个平衡 { ... } 区间
    start = text.find("{")
    if start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break
    raise json.JSONDecodeError("无法从模型输出中提取合法 JSON", text, 0)


class AskJsonResult:
    """一次 ask_json 的完整结果（含是否走了兜底）。"""

    def __init__(self, data: Any, *, ok: bool, retries: int, used_fallback: bool, error: str | None = None):
        self.data = data
        self.ok = ok  # ok=True：校验通过（即便重试过）；ok=False：最终走了确定性兜底
        self.retries = retries  # 实际重试次数（0..max_retries）
        self.used_fallback = used_fallback
        self.error = error

    def __repr__(self) -> str:  # pragma: no cover
        return f"AskJsonResult(ok={self.ok}, retries={self.retries}, used_fallback={self.used_fallback})"


def ask_json(
    client: LLMClient,
    system_prompt: str,
    user_prompt: str,
    schema: Mapping[str, Any],
    *,
    fallback: Callable[[], Any],
    temperature: float = 0.0,
    max_retries: int = 3,
) -> AskJsonResult:
    """请求 LLM 输出符合 schema 的 JSON；失败重试；仍失败走确定性兜底。

    - client: 模型客户端（DeepSeek 或 FakeLLM）
    - system_prompt: 任务提示（含字段要求），会与 JSON 强约束拼接
    - user_prompt: 本轮要分析的输入
    - schema: JSON Schema（jsonschema 校验用）
    - fallback: 确定性兜底（无参函数，返回合法 JSON 对象）
    - 返回 AskJsonResult
    """
    messages: list[dict[str, str]] = [
        {"role": "system", "content": f"{system_prompt}\n\n{_JSON_SYSTEM_PROMPT}"},
        {"role": "user", "content": user_prompt},
    ]

    retries = 0
    last_error: str | None = None
    while retries <= max_retries:
        if retries > 0:
            # 追加上次失败的反馈，帮助模型修正
            feedback = (
                f"你上次的输出未通过校验，错误：{last_error}\n"
                f"请重新只输出符合要求的 JSON。"
            )
            messages.append({"role": "user", "content": feedback})

        raw = client.complete(messages, temperature=temperature)

        try:
            data = _extract_json(raw)
            jsonschema.validate(instance=data, schema=dict(schema))
        except (json.JSONDecodeError, jsonschema.ValidationError) as e:
            last_error = str(e)
            retries += 1
            if retries > max_retries:
                # 重试耗尽，走确定性兜底
                try:
                    fallback_data = fallback()
                except Exception as fb_err:  # 兜底自身也不该炸
                    return AskJsonResult(
                        None, ok=False, retries=retries, used_fallback=True,
                        error=f"fallback 失败: {fb_err}",
                    )
                return AskJsonResult(
                    fallback_data, ok=False, retries=retries, used_fallback=True,
                    error=last_error,
                )
            continue

        return AskJsonResult(data, ok=True, retries=retries, used_fallback=False)

    # 理论不可达（while 内已返回）
    return AskJsonResult(None, ok=False, retries=retries, used_fallback=True, error=last_error)  # pragma: no cover
