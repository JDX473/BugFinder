"""工具注册框架与去重单元测试。"""

from rcagent.config import load_config
from rcagent.core.obs import SnapshotStore
from rcagent.core.tools import FINALIZE_NAME, ToolRegistry, ToolSpec, dedup_lines, make_finalize_spec


class TestDedup:
    def test_duplicate_lines_removed(self):
        text = "a\nb\nb\nc\nc\nc\n"
        assert dedup_lines(text) == "a\nb\nc\n"

    def test_adjacent_only(self):
        text = "a\nb\na\nb\n"
        assert dedup_lines(text) == text  # 不连续重复不清除

    def test_similar_lines_deduped(self):
        text = ("2024-01-01 INFO job - heartbeat ok\n"
                "2024-01-01 INFO job - heartbeat oK\n"
                "2024-01-01 ERROR job - boom\n")
        out = dedup_lines(text, ratio=0.9)
        assert out.count("heartbeat") == 1


class TestRegistry:
    def _make(self):
        cfg = load_config()
        store = SnapshotStore()
        reg = ToolRegistry(store=store, obs_head_chars=cfg.agent.obs_head_chars,
                           dedup_ratio=cfg.tools.dedup_ratio,
                           max_obs_chars=cfg.tools.max_obs_chars)
        return cfg, reg

    def test_register_and_call(self):
        _, reg = self._make()
        reg.register(ToolSpec(
            name="t", description="d", params={"x": "x"},
            handler=lambda kw, env: f"result for {kw['x']}",
        ))
        res = reg.call("t", {"x": "1"}, env=None)
        assert res.head == "result for 1"

    def test_duplicate_name_rejected(self):
        _, reg = self._make()
        spec = ToolSpec(name="t", description="d", params={}, handler=lambda kw, env: "")
        reg.register(spec)
        import pytest

        with pytest.raises(ValueError):
            reg.register(spec)

    def test_unknown_tool(self):
        _, reg = self._make()
        import pytest

        with pytest.raises(KeyError):
            reg.call("nope", {}, env=None)

    def test_long_output_obsk_wrapped(self):
        cfg, reg = self._make()
        big = "x" * 10000
        reg.register(ToolSpec(name="big", description="d", params={},
                              handler=lambda kw, env: big))
        res = reg.call("big", {}, env=None)
        assert res.truncated and res.snapshot is not None
        assert res.head.count("lines omitted") == 1
        assert reg.store.get(res.snapshot) == big

    def test_docs_generated(self):
        _, reg = self._make()
        reg.register(ToolSpec(name="t", description="do something",
                              params={"a": "param a"},
                              handler=lambda kw, env: "", examples='{"a": "1"}'))
        docs = reg.docs()
        assert "t(a=param a)" in docs and "do something" in docs


class TestFinalizeSpec:
    def test_fields(self):
        spec = make_finalize_spec(["root_cause", "solution", "evidence", "responsibility"])
        assert spec.name == FINALIZE_NAME
        assert set(spec.params) == {"root_cause", "solution", "evidence", "responsibility"}
        assert not spec.stateless
