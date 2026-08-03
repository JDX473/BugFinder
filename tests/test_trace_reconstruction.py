"""trace_reconstruction.py 的测试：强/弱重建、慢/错节点定位、异常路径。"""

from __future__ import annotations

from app.schema.models import ReconstructionConfidence, TimeRange
from app.tools.mock_datasource import MockLogDatasource
from app.tools.trace_reconstruction import (
    TraceReconstructionError,
    find_slow_or_error_hops,
    rebuild_trace,
)


def test_rebuild_strong_trace():
    src = MockLogDatasource()
    graph = rebuild_trace(src, "tr-mock-0001")
    assert graph.reconstruction_confidence == ReconstructionConfidence.STRONG
    # 三层：gateway → checkout → payment
    assert graph.services == ["checkout", "gateway", "payment"]
    edges = [(h.source_service, h.target_service) for h in graph.hops]
    assert ("gateway", "checkout") in edges
    assert ("checkout", "payment") in edges
    # checkout → payment 这跳应超时（3s）
    cp = [h for h in graph.hops if (h.source_service, h.target_service) == ("checkout", "payment")]
    assert cp and cp[0].duration_ms >= 3000
    assert cp[0].has_error


def test_find_slow_or_error_hops():
    src = MockLogDatasource()
    graph = rebuild_trace(src, "tr-mock-0001")
    findings = find_slow_or_error_hops(graph)
    assert findings
    # 至少 checkout → payment 这跳被标记
    marked = [(f["hop"].source_service, f["hop"].target_service) for f in findings]
    assert ("checkout", "payment") in marked


def test_weak_rebuild_no_direction():
    src = MockLogDatasource()
    # 把方向字段清掉，模拟"无 rpc_direction 埋点"的弱重建路径
    logs = src.query_logs(TimeRange(start=min(l.timestamp for l in src.logs), end=max(l.timestamp for l in src.logs)), trace_id="tr-mock-0001")
    stripped = [l.model_copy(update={"rpc_direction": None, "rpc_target": None}) for l in logs]

    # 直接用弱重建路径
    from app.tools.trace_reconstruction import _rebuild_from_logs
    graph = _rebuild_from_logs("tr-mock-0001", stripped)
    assert graph.reconstruction_confidence == ReconstructionConfidence.WEAK
    assert len(graph.hops) >= 1
    assert "弱重建" in graph.coverage_note


def test_empty_trace_raises():
    src = MockLogDatasource()
    try:
        rebuild_trace(src, "tr-nonexistent")
        assert False, "应当抛错"
    except TraceReconstructionError:
        pass


def test_baseline_compare():
    src = MockLogDatasource()
    graph = rebuild_trace(src, "tr-mock-0001")
    # 用很小的基线：checkout→payment 基线 500ms，慢因子 3 → 3000ms 必然超
    findings = find_slow_or_error_hops(graph, baseline_ms={("checkout", "payment"): 500.0})
    marked = {(f["hop"].source_service, f["hop"].target_service) for f in findings}
    assert ("checkout", "payment") in marked


def test_coverage_note_mentions_service():
    src = MockLogDatasource()
    graph = rebuild_trace(src, "tr-mock-0001")
    assert "checkout" in graph.coverage_note
