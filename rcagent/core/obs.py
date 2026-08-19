"""OBSK(Observation Snapshot Key,论文 §III-A):上下文压缩机制。

长 observation 只向 controller 展示 head,完整内容映射为快照键存入
键值库;controller 可以快照键作为工具参数,由外部方法(如 log agent)
处理长数据。
"""

from __future__ import annotations

import hashlib

_SNAPSHOT_PREFIX = "[snapshot: "
_SNAPSHOT_SUFFIX = " ]"


def snapshot_key(content: str) -> str:
    """确定性快照键:内容 hash 取 10 位数字(对齐论文示例风格)。"""
    digest = hashlib.sha1(content.encode("utf-8")).hexdigest()
    return str(int(digest[:12], 16) % 10**10).zfill(10)


def is_snapshot_key(value: str) -> bool:
    return len(value) == 10 and value.isdigit()


class SnapshotStore:
    """快照键值库:key -> 完整观察内容。"""

    def __init__(self):
        self._store: dict[str, str] = {}

    def put(self, content: str) -> str:
        key = snapshot_key(content)
        self._store[key] = content
        return key

    def get(self, key: str) -> str | None:
        return self._store.get(key)

    def resolve(self, value: str) -> str:
        """若 value 是快照键则返回完整内容,否则原样返回(供工具参数解析)。"""
        return self._store.get(value, value)

    def has(self, key: str) -> bool:
        return key in self._store


def build_observation_head(content: str, head_chars: int) -> tuple[str, str | None, bool]:
    """OBSK 包装:返回 (head, snapshot_key, truncated)。

    截断时按论文图3风格追加 "…N lines omitted. [snapshot: xxxxxx]"
    """
    if len(content) <= head_chars:
        return content, None, False
    head = content[:head_chars]
    key = snapshot_key(content)
    omitted = content.count("\n", head_chars) + 1
    head = (
        head
        + "\n"
        + f"...{omitted} lines omitted. {_SNAPSHOT_PREFIX}{key}{_SNAPSHOT_SUFFIX.strip()}"
    )
    return head, key, True
