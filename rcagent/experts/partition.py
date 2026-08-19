"""Algorithm 1 语义分区(论文 §III-B2, 步骤 1-12)。

行切分 → 行级 embedding → 加权无向稠密图(窗口 j-i∈(0,200],
w_ij = sim_ij × exp(−d_ij)) → Louvain 社区检测 → 贪心去重叠保证
每个簇内部连续(日志 chunk 必须是原文中的连续片段)。
"""

from __future__ import annotations

import math
from collections import defaultdict

import community as community_louvain
import networkx as nx

from ..llm.embedding import Embedder

DEFAULT_WINDOW = 200  # 论文: j − i ∈ (0, 200]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def greedy_overlap_removal(labels: list[int]) -> list[int]:
    """贪心切换最小量聚类标签,使每个簇在原文中连续(论文步骤 11)。

    对每个标签保留其最长连续段,其余段整体切换为相邻段的标签;
    迭代至稳定(切换可能合并相邻段)。
    """
    labels = list(labels)
    n = len(labels)
    for _ in range(5):
        segments: dict[int, list[tuple[int, int]]] = defaultdict(list)
        i = 0
        while i < n:
            j = i
            while j + 1 < n and labels[j + 1] == labels[i]:
                j += 1
            segments[labels[i]].append((i, j))
            i = j + 1
        changed = False
        for label, segs in segments.items():
            if len(segs) <= 1:
                continue
            keep = max(segs, key=lambda s: s[1] - s[0])
            for (s, e) in segs:
                if (s, e) == keep:
                    continue
                left = labels[s - 1] if s > 0 else None
                right = labels[e + 1] if e + 1 < n else None
                new_label = left if left is not None else right
                if new_label is None or new_label == label:
                    continue
                for k in range(s, e + 1):
                    labels[k] = new_label
                changed = True
        if not changed:
            break
    return labels


def semantic_partition(
    lines: list[str],
    embedder: Embedder,
    window: int = DEFAULT_WINDOW,
    seed: int = 0,
) -> list[list[str]]:
    """把日志行分为语义相关的连续 chunk。

    权重截断负余弦到 0(负相似度无聚类意义);exp(−d) 使远距离配对
    权重指数衰减,与论文公式一致。
    """
    if len(lines) <= 1:
        return [lines]

    vecs = embedder.embed(lines)
    G = nx.Graph()
    G.add_nodes_from(range(len(lines)))
    for i in range(len(lines)):
        for j in range(i + 1, min(i + window, len(lines))):
            sim = _cosine(vecs[i], vecs[j])
            if sim <= 0:
                continue
            w = sim * math.exp(-(j - i))
            G.add_edge(i, j, weight=w)

    partition = community_louvain.best_partition(G, weight="weight", random_state=seed)
    labels = greedy_overlap_removal([partition[i] for i in range(len(lines))])

    chunks: list[list[str]] = []
    cur = [lines[0]]
    cur_label = labels[0]
    for i in range(1, len(lines)):
        if labels[i] == cur_label:
            cur.append(lines[i])
        else:
            chunks.append(cur)
            cur = [lines[i]]
            cur_label = labels[i]
    chunks.append(cur)
    return chunks
