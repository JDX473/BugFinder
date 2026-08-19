"""OBSK(论文 §III-A)单元测试。"""

from rcagent.core.obs import SnapshotStore, build_observation_head, is_snapshot_key, snapshot_key


class TestSnapshotKey:
    def test_deterministic(self):
        assert snapshot_key("abc") == snapshot_key("abc")
        assert snapshot_key("abc") != snapshot_key("abd")

    def test_10_digit_numeric(self):
        key = snapshot_key("some long log content" * 10)
        assert len(key) == 10 and key.isdigit()

    def test_is_snapshot_key(self):
        assert is_snapshot_key(snapshot_key("x"))
        assert not is_snapshot_key("hello")
        assert not is_snapshot_key("12345678901")


class TestObservationHead:
    def test_short_content_not_truncated(self):
        head, key, truncated = build_observation_head("short log", 4000)
        assert head == "short log" and key is None and not truncated

    def test_long_content_truncated_with_snapshot(self):
        content = "\n".join(f"line {i} xxxxxxxxxx" for i in range(500))
        head, key, truncated = build_observation_head(content, 200)
        assert truncated and key is not None
        assert "lines omitted" in head
        assert f"[snapshot: {key}]" in head
        # head 长度受控
        assert len(head) < 400


class TestSnapshotStore:
    def test_put_get_resolve(self):
        store = SnapshotStore()
        key = store.put("full content")
        assert store.get(key) == "full content"
        assert store.resolve(key) == "full content"
        assert store.resolve("not a key") == "not a key"
        assert store.has(key)
        assert not store.has("nope")
