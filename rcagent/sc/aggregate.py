"""文本级 Self-Consistency 聚合(论文 §III-D1)。

两种聚合方式:
- Embedding 投票: 选与 K 个候选均值 embedding 余弦相似度最高者
  (直接推广无权重多数投票, a 从 one-hot 换成语义 embedding)。
- LLM 聚合: 提示 LLM 综合候选,输出相似形式与长度的结果(论文实测更优,
  采样越多优势越大)。
失败/不完整轨迹按论文 §IV-C 自动填 baseline content "Unclear"。
"""

from __future__ import annotations

import math

from ..core.jsonregen import json_regen
from ..llm.client import LLMClient
from ..llm.embedding import Embedder

UNCLEAR = "Unclear"


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def embedding_vote(texts: list[str], embedder: Embedder) -> str:
    """argmax_i similarity(a_i, 1/K Σ_j a_j): 距候选均值最近的文本胜出。"""
    if not texts:
        return UNCLEAR
    vecs = embedder.embed(texts)
    dim = len(vecs[0])
    mean = [sum(v[d] for v in vecs) / len(vecs) for d in range(dim)]
    best = max(zip(texts, vecs), key=lambda tv: _cosine(tv[1], mean))
    return best[0]


def majority_vote(items: list[str]) -> str:
    from collections import Counter

    return Counter(items).most_common(1)[0][0]


def aggregate_by_embedding(candidates: list[dict | None], embedder: Embedder,
                           required_fields: list[str]) -> dict:
    """逐字段投票:文本字段 embedding 投票;枚举字段多数投票。

    失败候选(None)的文本字段填 "Unclear" 参与投票(论文 baseline 填充)。
    """
    filled = [c if c is not None else {f: UNCLEAR for f in required_fields}
              for c in candidates]
    result: dict = {}
    for f in required_fields:
        values = [c[f] for c in filled]
        if f == "responsibility":
            result[f] = majority_vote(values)
        else:
            result[f] = embedding_vote(values, embedder)
    return result


_AGGREGATE_TEMPLATE = """\
You are aggregating {n} candidate root cause analyses produced by different \
runs of an agent investigating the SAME anomaly. Produce ONE final result \
that is the most accurate and helpful synthesis of the candidates.

{candidates}

Respond with a JSON object with exactly these fields:
{{"root_cause": "<final root cause>",
 "solution": "<final solution>",
 "evidence": "<strongest evidence, quoted from actual logs>",
 "responsibility": "platform" or "user"}}"""


def aggregate_by_llm(llm: LLMClient, candidates: list[dict | None],
                     required_fields: list[str],
                     retries: int = 2) -> dict | None:
    """LLM 聚合:只聚合成功候选(失败候选填 Unclear 会干扰综合判断)。"""
    valid = [c for c in candidates if c is not None]
    if not valid:
        return None
    blocks = []
    for i, c in enumerate(valid, 1):
        lines = "\n".join(f"{f}: {c.get(f, UNCLEAR)}" for f in required_fields)
        blocks.append(f"Candidate {i}:\n{lines}")
    prompt = _AGGREGATE_TEMPLATE.format(n=len(valid), candidates="\n\n".join(blocks))
    out = json_regen(llm, prompt, retries=retries)
    if out is None:
        return None
    result = {}
    for f in required_fields:
        v = out.get(f)
        if not isinstance(v, str) or not v.strip():
            return None
        result[f] = v.strip()
    return result
