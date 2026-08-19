"""LLM 评估器(论文 §IV-C): G-Correctness / G-Helpfulness / Win Rate。

评估 prompt 照录论文原文:
- G-Correctness:  "Judge the correctness of the prediction, 0 is completely
                   wrong and 10 is well-matched"
- G-Helpfulness:  "Judge the helpfulness of the prediction, 0 is completely
                   misleading and 10 is very helpful"
- Win Rate: 判定各方法结果是否优于 ReAct 结果。

论文使用 gpt-4-0613 greedy 解码;本项目按已确认决策使用 DeepSeek API。
"""

from __future__ import annotations

import re

from ..llm.client import LLMClient

_CORRECTNESS_PROMPT = (
    "Judge the correctness of the prediction, 0 is completely wrong and "
    "10 is well-matched.\n\n"
    "Prediction: {pred}\n\nReference (ground truth): {ref}\n\n"
    "Score: "
)

_HELPFULNESS_PROMPT = (
    "Judge the helpfulness of the prediction, 0 is completely misleading "
    "and 10 is very helpful.\n\n"
    "Prediction: {pred}\n\n"
    "Reference (ground truth): {ref}\n\n"
    "Score: "
)

_WIN_PROMPT = (
    "Two root cause analysis results are given for the same anomaly. "
    "Judge whether Prediction A is better than Prediction B (more accurate "
    "and helpful), or worse, or they are equivalent.\n\n"
    "Prediction A: {pred_a}\n\n"
    "Prediction B: {pred_b}\n\n"
    "Reference (ground truth): {ref}\n\n"
    "Respond with exactly one of: BETTER | WORSE | EQUAL"
)


def _extract_score(text: str) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)", text)
    if m is None:
        return 0.0
    return min(max(float(m.group(1)), 0.0), 10.0)


def judge_score(llm: LLMClient, kind: str, pred: str, ref: str,
                temperature: float = 0.0) -> float:
    """G-Correctness / G-Helpfulness 单例评分(0~10)。"""
    template = _CORRECTNESS_PROMPT if kind == "correctness" else _HELPFULNESS_PROMPT
    gen = llm.chat([{"role": "user", "content": template.format(pred=pred, ref=ref)}],
                   temperature=temperature)
    return _extract_score(gen.text)


def judge_win(llm: LLMClient, pred_a: str, pred_b: str, ref: str,
              temperature: float = 0.0) -> str:
    """Win Rate 判定:BETTER | WORSE | EQUAL。"""
    gen = llm.chat([{"role": "user", "content": _WIN_PROMPT.format(
        pred_a=pred_a, pred_b=pred_b, ref=ref)}], temperature=temperature)
    text = gen.text.strip().upper()
    for tag in ("BETTER", "WORSE", "EQUAL"):
        if tag in text:
            return tag
    return "EQUAL"


def compute_win_rate(llm: LLMClient, method_preds: list[str], react_preds: list[str],
                     refs: list[str]) -> tuple[float, list[str]]:
    """方法 vs ReAct 的 Win Rate: A 判定为 BETTER 的比例。"""
    wins = 0
    verdicts: list[str] = []
    for mp, rp, ref in zip(method_preds, react_preds, refs):
        v = judge_win(llm, mp, rp, ref)
        verdicts.append(v)
        if v == "BETTER":
            wins += 1
    return wins / max(len(verdicts), 1), verdicts
