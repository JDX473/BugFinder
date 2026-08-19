"""错误处理(论文 §III-C2)单元测试: 重复调用 / trivial 输入 / 过早 finalize。"""

from rcagent.config import load_config
from rcagent.core.errors import ErrorDetector


def make_detector():
    cfg = load_config()
    return ErrorDetector(cfg.agent, tool_names={"a", "b", "finalize"}, expert_names={"log_agent"})


class TestDuplicateCall:
    def test_detected(self):
        d = make_detector()
        d.record_call("a", {"x": "1"}, step=1)
        err = d.detect("a", {"x": "1"}, step=2)
        assert err is not None and "already invoked" in err

    def test_different_args_ok(self):
        d = make_detector()
        d.record_call("a", {"x": "1"}, step=1)
        assert d.detect("a", {"x": "2"}, step=2) is None

    def test_expert_duplicate_detected(self):
        d = make_detector()
        d.record_call("log_agent", {"snapshot": "s123456789012345"}, step=1)
        # expert 相同输入的重复调用同样无意义,论文 §III-C2(i) 覆盖
        err = d.detect("log_agent", {"snapshot": "s123456789012345"}, step=2)
        assert err is not None and "already invoked" in err

    def test_expert_different_input_ok(self):
        d = make_detector()
        d.record_call("log_agent", {"snapshot": "s123456789012345"}, step=1)
        assert d.detect("log_agent", {"snapshot": "s999999999999999"}, step=2) is None


class TestTrivialInput:
    def test_trivial_detected(self):
        d = make_detector()
        err = d.detect("log_agent", {"snapshot": ""}, step=1)
        assert err is not None and "trivial" in err

    def test_substantial_ok(self):
        d = make_detector()
        assert d.detect("log_agent", {"snapshot": "1234567890"}, step=1) is None


class TestEarlyFinalize:
    def test_early_finalize_detected(self):
        d = make_detector()
        err = d.detect("finalize", {"root_cause": "x"}, step=1)
        assert err is not None and "before thorough investigation" in err

    def test_after_investigation_ok(self):
        d = make_detector()
        d.record_info_tool("runtime_log")
        assert d.detect("finalize", {"root_cause": "x"}, step=2) is None
