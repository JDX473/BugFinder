"""动作解析:从 LLM 输出中提取 thought 与 JSON action。

use_regen=True 走 JsonRegen 管线(论文 Algorithm 2);
False 为简化解析(直接 json.loads),用于 ReAct 基线/消融对照。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .jsonregen import parse_json_or_none


@dataclass
class ParsedAction:
    thought: str
    function: str
    kwargs: dict
    raw: str


@dataclass
class ParseFailure:
    reason: str
    raw: str


def _build(parsed: dict, text: str) -> ParsedAction | None:
    function = parsed.get("function") or parsed.get("tool")
    if not isinstance(function, str) or not function:
        return None
    kwargs = parsed.get("kwargs")
    if kwargs is None:
        kwargs = {k: v for k, v in parsed.items() if k not in ("function", "thought")}
    if not isinstance(kwargs, dict):
        return None
    thought = parsed.get("thought", "")
    if not thought:
        idx = text.find("{")
        thought = text[:idx].strip() if idx >= 0 else ""
    return ParsedAction(thought=str(thought), function=function, kwargs=kwargs, raw=text)


def parse_action(text: str, *, use_regen: bool = True) -> ParsedAction | ParseFailure:
    """解析 "Thought: ... \n Function: {...}" 风格输出。

    JSON 块(最后一个 {...})解析为 action,之前文本作为 thought。
    use_regen=False 时不使用 JsonRegen 修复(基线对照)。
    """
    if use_regen:
        parsed = parse_json_or_none(text)
        if parsed is None:
            return ParseFailure("action is not a parsable JSON object", text)
        action = _build(parsed, text)
        if action is None:
            return ParseFailure("action is not a JSON object with function field", text)
        return action

    # 简化解析: 取最外层花括号块直接 json.loads
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return ParseFailure("action is not a parsable JSON object", text)
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        return ParseFailure(f"action JSON invalid: {e}", text)
    action = _build(parsed, text)
    if action is None:
        return ParseFailure("action is not a JSON object with function field", text)
    return action
