"""Web 运行时单元测试: RunManager 行为 / 事件合成 / SSE 格式化。"""

import json
import time

from rcagent.config import load_config
from rcagent.web.events import classify_error, derive_events_from_trajectory, format_sse
from rcagent.web.runs import RunBusy, RunManager

CFG = load_config()


class TestRunManager:
    def test_mock_run_lifecycle(self, tmp_path):
        """mock run 完整生命周期: running → 事件 → passed + 落盘。"""
        mgr = RunManager(CFG, runs_dir=tmp_path)
        run_id = mgr.start("demo_es_conn_timeout", mock=True)
        assert mgr.active_run_id() == run_id

        # 轮询等待完成
        deadline = time.time() + 30
        while time.time() < deadline:
            batch, done = mgr.events_since(run_id, -1)
            if done:
                break
            time.sleep(0.05)
        assert done, "run did not finish in time"

        types = [e["type"] for e in batch]
        assert types[0] == "run_started"
        assert types[-1] == "run_finished"
        assert "finalize_result" in types
        assert mgr.get_run(run_id)["trajectory"]["status"] == "passed"
        # seq 单调
        seqs = [e["seq"] for e in batch]
        assert seqs == sorted(seqs)

    def test_concurrency_limit(self, tmp_path):
        mgr = RunManager(CFG, runs_dir=tmp_path)
        run_id = mgr.start("demo_es_conn_timeout", mock=True)
        try:
            import pytest

            with pytest.raises(RunBusy):
                mgr.start("demo_task_evicted", mock=True)
        finally:
            mgr.cancel(run_id)

    def test_cancel(self, tmp_path):
        mgr = RunManager(CFG, runs_dir=tmp_path)
        run_id = mgr.start("demo_es_conn_timeout", mock=True)
        assert mgr.cancel(run_id)
        deadline = time.time() + 30
        while time.time() < deadline:
            _, done = mgr.events_since(run_id, -1)
            if done:
                break
            time.sleep(0.05)
        snap = mgr.get_run(run_id)
        assert snap["meta"]["status"] in ("cancelled", "passed")  # mock 可能已跑完

    def test_events_persisted(self, tmp_path):
        mgr = RunManager(CFG, runs_dir=tmp_path)
        run_id = mgr.start("demo_es_conn_timeout", mock=True)
        deadline = time.time() + 30
        while time.time() < deadline:
            _, done = mgr.events_since(run_id, -1)
            if done:
                break
            time.sleep(0.05)
        # 事件落盘 .events.json,历史可精确回放
        ev_files = list(tmp_path.glob("*.events.json"))
        assert ev_files, "events file not persisted"
        evs = json.loads(ev_files[0].read_text(encoding="utf-8"))
        assert evs[-1]["type"] == "run_finished"


class TestDeriveEvents:
    def test_classify_error(self):
        assert classify_error("Error: tool 'x' does not exist.") == "unknown_tool"
        assert classify_error("undeclared parameter(s)") == "unknown_params"
        assert classify_error("missing required parameter") == "missing_params"
        assert classify_error("already invoked with identical arguments") == "error_detected"
        assert classify_error("tool 'x' failed: boom") == "tool_failed"
        assert classify_error("action is not a parsable JSON") == "parse_failed"

    def test_derive_from_trajectory(self):
        traj = {
            "job_id": "j1", "status": "passed", "started": 1.0, "finished": 2.0,
            "invalid_actions": 0,
            "records": [{
                "step": 1, "thought": "t", "t": 1.1,
                "raw_action": '{"function": "runtime_log", "kwargs": {}}',
                "action": {"function": "runtime_log", "kwargs": {}},
                "error": None,
                "observation_head": "Observation:\nhead...",
            }],
            "result": {"root_cause": "c"},
        }
        evs = derive_events_from_trajectory(traj)
        types = [e["type"] for e in evs]
        assert types[0] == "run_started"
        assert "parse_ok" in types and "step_completed" in types
        assert types[-2] == "finalize_result"
        assert types[-1] == "run_finished"


class TestSSEFormat:
    def test_format_utf8(self):
        s = format_sse(3, "live_j1_1", 123.0, "parse_ok",
                       {"thought": "中文思考"})
        assert s.startswith("id: 3\n")
        assert "中文思考" in s
        assert s.endswith("\n\n")
