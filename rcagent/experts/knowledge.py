"""知识库与 ICP 检索(Algorithm 1 步骤 15-18)。

知识库为目标服务的"历史诊断记录/故障模式库"(论文的 Flink Advisor
历史子集)。检索:chunk 与示例的 embedding 余弦相似度排序,按相似度
依次填充 in-context prompt 直到不超过最大长度 N。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..llm.embedding import Embedder


@dataclass
class KBExample:
    text: str    # 日志片段(示例输入)
    answer: str  # 对应分析(interpretation + evidence)


class KnowledgeBase:
    def __init__(self, examples: list[KBExample], embedder: Embedder, max_chars: int = 8000):
        self.examples = examples
        self.embedder = embedder
        self.max_chars = max_chars
        self._vecs = embedder.embed([e.text for e in examples]) if examples else []

    def search(self, chunk: str, top_k: int = 3) -> list[tuple[str, str]]:
        """返回按相似度排序的 (示例文本, 答案) 对,累积长度不超过 max_chars。"""
        if not self.examples:
            return []
        v = self.embedder.embed([chunk])[0]
        sims = sorted(
            range(len(self.examples)),
            key=lambda i: _cosine(v, self._vecs[i]),
            reverse=True,
        )
        result: list[tuple[str, str]] = []
        used = 0
        for i in sims[:top_k]:
            e, a = self.examples[i].text, self.examples[i].answer
            cost = len(e) + len(a)
            if used + cost > self.max_chars:
                break
            result.append((e, a))
            used += cost
        return result

    def build_icp(self, chunk: str, top_k: int = 3) -> str:
        """组装 in-context prompt:相似示例-答案对(论文步骤 15-21 的 ICP)。"""
        parts = []
        for i, (e, a) in enumerate(self.search(chunk, top_k)):
            parts.append(f"Example {i + 1} log:\n{e}\nAnalysis of example {i + 1}:\n{a}")
        return "\n\n".join(parts)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def build_demo_kb(embedder: Embedder) -> KnowledgeBase:
    """演示知识库:从合成场景的错误模式构建示例-答案对。

    论文要求知识库严格排除标注规则;这里的"答案"是场景的根因描述
    与证据(即诊断知识),标注集(ground truth)不进入知识库。
    """
    return KnowledgeBase(demo_kb_examples(), embedder)


def build_im_kb(embedder: Embedder) -> KnowledgeBase:
    """IM 初始知识库:从 QuantumLink IM 的真实故障模式提炼。

    阶段 1 只有 Redis 故障案例;阶段 2(故障注入)后扩充 RocketMQ/
    积压/连接类模式。答案即诊断知识,与标注集分离。
    """
    return KnowledgeBase(im_kb_examples(), embedder)


def im_kb_examples() -> list[KBExample]:
    """IM 知识库静态内容(供 API/文档展示,不依赖 embedder)。"""
    return [
        KBExample(
            text="""2026-08-19T00:06:03.599+08:00 ERROR --- c.q.im.chat.service.OutboxService: outbox scan error
org.springframework.data.redis.RedisSystemException: Error in execution
	at org.springframework.data.redis.connection.lettuce.LettuceExceptionConverter.convert""",
            answer=("interpretation: OutboxService 定时扫描 Redis 发件箱失败,Redis 连接异常"
                    "(Lettuce 无法执行命令),发件箱重推机制停摆\n"
                    "evidence: outbox scan error; RedisSystemException"),
        ),
        KBExample(
            text="""2026-08-19T00:07:25.139+08:00 ERROR --- c.q.im.chat.service.MessageService: async process error: conv=u_xxx#u_yyy""",
            answer=("interpretation: 消息异步处理失败,与同一时段 Redis 故障相关"
                    "(seq 递增/幂等/缓存依赖 Redis)\n"
                    "evidence: async process error"),
        ),
        KBExample(
            text="""ERROR com.quantumlink.im.connect.handler.UpstreamProducer - send failed topic=client2server
org.apache.rocketmq.remoting.exception.RemotingConnectException: connect to 192.168.40.1:10911 failed""",
            answer=("interpretation: RocketMQ broker 宕机/不可达,im-connect 上行消息无法"
                    "上抛到 im-chat(RemotingConnectException 连 broker 失败)\n"
                    "evidence: send failed topic=client2server; RemotingConnectException"),
        ),
    ]


def demo_kb_examples() -> list[KBExample]:
    """demo 知识库静态内容(供 API/文档展示,不依赖 embedder)。"""
    from ..env.local import SCENARIOS

    examples = []
    for key, s in SCENARIOS.items():
        err_lines = s["err_lines"]
        examples.append(KBExample(
            text="\n".join(err_lines),
            answer=(
                f"interpretation: {s['root_cause']}\n"
                f"evidence: {err_lines[0]}"
            ),
        ))
    return examples
