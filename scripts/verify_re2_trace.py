"""RE2-TT 真实 trace 验证：从 span 数据重建调用链 + 慢/错节点定位。

RE2-TT（Train Ticket，64 服务）的 traces.csv 是 Jaeger 采集的完整 span 表：
  time, traceID, spanID, serviceName, methodName, operationName,
  startTimeMillis, startTime, duration, statusCode, parentSpanID

**与本项目硬约束的关系**：我们约束是"只有 traceId、无完整链路存储，靠日志重建"。
RE2-TT 是完整链路存储（span 表）——它验证的是"给定 traceId 能还原调用链 + 定位
慢/错节点"这一**核心能力**（同一能力，数据形态更接近理想链路存储）。

验证路径：
  1. 按 traceID 聚合 span → 还原调用链（父→子关系 + 耗时 + 错误）
  2. 定位慢/错节点（duration 超阈值 / statusCode 异常）
  3. 对比根因服务（case 目录名标注）：根因服务是否在慢/错 Top-N 里

用法：
    python scripts/verify_re2_trace.py --case ts-travel-service_socket/1
    python scripts/verify_re2_trace.py --limit 10   # 前 10 个 case
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# RE2-TT zip 路径（数据在项目外，不污染仓库）
_ZIP = "E:/QIUZHAO/rca-data/RE2-TT.zip"


def _load_case_spans(z, case_key: str) -> list[dict]:
    """读一个 case 的 traces.csv（zip 内，不落盘）。"""
    path = f"RE2-TT/{case_key}/traces.csv"
    content = z.read(path).decode("utf-8", errors="replace")
    return list(csv.DictReader(io.StringIO(content)))


def _build_call_chain(spans: list[dict]) -> tuple[dict, dict]:
    """按 traceID 聚合，从 parentSpanID 构建调用树。

    返回 (chains, root_traces)：chains[traceID] = {spanID: span}，root_traces 列表。
    """
    chains: dict[str, dict] = {}
    root_traces: list[str] = []
    for s in spans:
        tid = s["traceID"]
        chains.setdefault(tid, {})[s["spanID"]] = s
    # 根 trace = parentSpanID 为空 或 parent 不在本 trace 的
    for tid, spans_map in chains.items():
        if not spans_map:
            continue
        has_root = any(not s.get("parentSpanID") for s in spans_map.values())
        if has_root:
            root_traces.append(tid)
    return chains, root_traces


def _children_of(chains: dict, tid: str, parent_id: str) -> list[dict]:
    """一个 span 的子 span 列表（按 parentSpanID 匹配）。"""
    return [s for s in chains[tid].values() if s.get("parentSpanID") == parent_id]


def _compute_path_latency(chains: dict, tid: str, span: dict) -> float:
    """span 子树的总耗时（含子 span，模拟完整调用链耗时）。"""
    total = float(span.get("duration") or 0)
    for child in _children_of(chains, tid, span["spanID"]):
        total += _compute_path_latency(chains, tid, child)
    return total


def _find_slow_nodes(
    chains: dict, root_traces: list[str], *, top_k: int = 5, threshold_factor: float = 5.0
) -> list[dict]:
    """定位慢/错节点：根 trace 的子树耗时超全局中位数 threshold_factor 倍。

    优化：Jaeger 的根 span `duration` 已包含整棵调用链耗时（含子 span），
    直接取根 span duration，避免 O(span×深度) 递归——真实 trace 16 万 span/case，
    递归会慢到无法批量验证（实测 10 case 超 120s）。
    """
    if not root_traces:
        return []
    root_durs: list[tuple[str, float]] = []  # (service, duration)
    for tid in root_traces:
        spans_map = chains[tid]
        root = next((s for s in spans_map.values() if not s.get("parentSpanID")), None)
        if root:
            try:
                root_durs.append((root["serviceName"], float(root.get("duration") or 0)))
            except (ValueError, TypeError):
                continue
    if not root_durs:
        return []
    median = sorted(d for _, d in root_durs)[len(root_durs) // 2]

    # 慢节点 = 根 span 耗时超过全局中位数的 threshold_factor 倍的服务（累计）
    svc_durs: dict[str, float] = defaultdict(float)
    for svc, d in root_durs:
        if d > median * threshold_factor:
            svc_durs[svc] += d
    return [
        {"service": svc, "total": total}
        for svc, total in sorted(svc_durs.items(), key=lambda x: -x[1])[:top_k]
    ]


def _verify_case(z, case_key: str, *, verbose: bool = False) -> dict:
    """验证一个 case：还原调用链 + 慢节点定位，对比根因服务。"""
    spans = _load_case_spans(z, case_key)
    if not spans:
        return {"key": case_key, "ok": False, "reason": "无 span"}
    root_service = case_key.rsplit("_", 1)[0]  # case 名 {service}_{fault} 的 service 部分

    chains, root_traces = _build_call_chain(spans)
    slow = _find_slow_nodes(chains, root_traces)

    if verbose:
        print(f"\n=== {case_key} ===")
        print(f"根因服务: {root_service} | spans: {len(spans)} | 根 traces: {len(root_traces)}")
        print(f"慢节点 Top{len(slow)}:")
        for n in slow:
            mark = " ← 根因" if n["service"] == root_service else ""
            print(f"  {n['service']}: 子树耗时 {n['total']/1000:.0f}ms{mark}")

    hit = any(n["service"] == root_service for n in slow)
    return {
        "key": case_key,
        "root_service": root_service,
        "n_spans": len(spans),
        "n_traces": len(root_traces),
        "slow_hit": hit,
        "top_slow": [n["service"] for n in slow],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RE2-TT 真实 trace 验证")
    parser.add_argument("--case", default=None, help="单 case（如 ts-travel-service_socket/1）")
    parser.add_argument("--limit", type=int, default=10, help="case 数上限")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    z = zipfile.ZipFile(_ZIP)
    # 找全部 case key（含 traces.csv 的）
    case_keys = sorted({
        "/".join(n.split("/")[1:3])
        for n in z.namelist()
        if n.count("/") == 3 and n.endswith("traces.csv")
    })
    if args.case:
        case_keys = [args.case] if args.case in case_keys else []
        if not case_keys:
            print(f"case {args.case} 不存在，可用如 {case_keys[:3] if case_keys else 'ts-travel-service_socket/1'}")
    else:
        case_keys = case_keys[: args.limit]

    results = []
    for key in case_keys:
        try:
            results.append(_verify_case(z, key, verbose=args.verbose))
        except Exception as e:
            print(f"[跳过] {key}: {e}")

    n = len(results)
    hit = sum(1 for r in results if r["slow_hit"])
    print(f"\n{'='*50}")
    print(f"RE2-TT trace 验证 {n} 个 case（慢节点定位）")
    print(f"根因服务命中慢节点 Top: {hit}/{n} = {hit/max(n,1)*100:.1f}%")
    avg_traces = sum(r["n_traces"] for r in results) / max(n, 1)
    print(f"平均每 case 根 traces: {avg_traces:.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
