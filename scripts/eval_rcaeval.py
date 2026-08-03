"""RCAEval 真实数据评估：跑 RCAWorkflow 看命中率与报告质量（PRD §9 评测雏形）。

用法（RE1-OB 数据已解压到 rca-data/）：
    python scripts/eval_rcaeval.py --root E:/QIUZHAO/rca-data/RE1-OB --limit 20
    python scripts/eval_rcaeval.py --root E:/QIUZHAO/rca-data/RE1-OB --case productcatalogservice_cpu/1

对每个 case：
  1. 用 RcaEvalMetricSource 构造 IncidentEvent + 全部指标序列
  2. 手动跑场景路由 + 假设打分（复用 workflow 的节点逻辑，避免 mock 数据源干扰）
  3. 判定：候选 Top-3 是否覆盖 ground truth（根因服务）
  4. 汇总 Top-1/Top-3 命中率、场景判定分布

注意：RE1-OB 只有指标（无日志/trace），所以跳过 trace/日志步骤，
只验证指标驱动的场景路由 + 假设打分这条主线。
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 保证脚本直接运行时能找到 app 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _load_case_data(src, key: str):
    """取一个 case 的 incident + 指标序列 + ground truth。"""
    from app.pipeline.anomaly_detection import detect_anomaly

    incident = src.incident_for(key)
    case = src.case(key)
    series_list = src.anomaly_series(key)
    all_results = [detect_anomaly(s) for s in series_list]
    return incident, case, series_list, all_results


def _hit(candidates, case) -> bool:
    """Top-N 是否命中 ground truth 根因服务。"""
    gt_svc = case.service  # 根因服务
    svcs = set()
    for c in candidates:
        for w in (gt_svc, case.fault):
            if w and w.lower() in c.hypothesis.lower():
                svcs.add(gt_svc)
    return bool(svcs)


def _evaluate_case(src, key: str, verbose: bool = False, llm=None) -> dict:
    """评估一个 case，返回判定结果。llm 可选注入（对比规则/LLM 效果）。"""
    from app.pipeline.hypothesis_scoring import generate_hypotheses
    from app.pipeline.scenario_router import route_scenario
    from app.schema.models import Evidence, EvidenceType, TimeRange

    incident, case, series_list, all_results = _load_case_data(src, key)

    # 场景路由（llm 注入时走 LLM 兜底）
    scenario = route_scenario(
        incident_text=incident.alert.title or "",
        anomalies=all_results,
        llm=llm,
    )

    # 构造指标证据（供假设打分）
    abnormal = [a for a in all_results if a.is_anomaly]
    ev = Evidence(
        evidence_id="ev-metric",
        type=EvidenceType.METRIC,
        source="rcaeval",
        summary=f"指标检测 {len(all_results)} 个序列，{len(abnormal)} 个异常",
        payload={"anomalies": abnormal, "tech_signal_clean": not abnormal},
    )

    # 假设打分（llm 注入时走 LLM 排序）
    hyps = generate_hypotheses(
        evidence=[ev],
        scenario=scenario,
        event_start=incident.triggered_at - timedelta(minutes=30),
        llm=llm,
    )

    top1_hit = _hit(hyps.candidates[:1], case)
    top3_hit = _hit(hyps.candidates[:3], case)

    if verbose:
        print(f"\n=== {key} ===")
        print(f"ground truth: {case.service} / {case.fault}")
        print(f"场景: {scenario.scenario.value}（来源 {scenario.source}）")
        print(f"候选: {len(hyps.candidates)}")
        for c in hyps.candidates:
            print(f"  rank{c.rank} [{c.confidence:.2f}] {c.hypothesis[:60]}")
        print(f"Top1命中={top1_hit} Top3命中={top3_hit}")

    return {
        "key": key,
        "service": case.service,
        "fault": case.fault,
        "scenario": scenario.scenario.value,
        "scenario_source": scenario.source,
        "top1": top1_hit,
        "top3": top3_hit,
        "n_candidates": len(hyps.candidates),
        "used_llm": scenario.source == "llm" or hyps.used_llm,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RCAEval 真实数据评估")
    parser.add_argument("--root", default="E:/QIUZHAO/rca-data/RE1-OB", help="RE1-OB 数据根目录")
    parser.add_argument("--limit", type=int, default=20, help="评估 case 数上限")
    parser.add_argument("--case", default=None, help="只评估单个 case（如 productcatalogservice_cpu/1）")
    parser.add_argument("--verbose", action="store_true", help="打印每个 case 详情")
    parser.add_argument("--llm", action="store_true", help="注入真实 DeepSeek（需 RCA_LLM_API_KEY）")
    args = parser.parse_args(argv)

    from app.tools.rcaeval_datasource import RcaEvalMetricSource

    src = RcaEvalMetricSource(args.root)
    llm = None
    if args.llm:
        from app.llm.deepseek_llm import create_deepseek_client

        llm = create_deepseek_client()
        if llm is None:
            print("[警告] 未配置 RCA_LLM_API_KEY，LLM 评估退化为规则模式")
    if args.case:
        cases = [args.case]
    else:
        cases = src.list_cases()[: args.limit]

    results = []
    for key in cases:
        try:
            results.append(_evaluate_case(src, key, verbose=args.verbose, llm=llm))
        except Exception as e:
            print(f"[跳过] {key}: {e}")

    # 汇总
    n = len(results)
    top1 = sum(1 for r in results if r["top1"])
    top3 = sum(1 for r in results if r["top3"])
    llm_used = sum(1 for r in results if r["used_llm"])
    print(f"\n{'='*50}")
    print(f"评估 {n} 个 case（RE1-OB 真实指标数据，{'LLM' if args.llm else '规则'} 模式）")
    print(f"Top-1 命中: {top1}/{n} = {top1/n*100:.1f}%")
    print(f"Top-3 命中: {top3}/{n} = {top3/n*100:.1f}%")
    print(f"场景分布: {dict(Counter(r['scenario'] for r in results))}")
    print(f"平均候选数: {sum(r['n_candidates'] for r in results)/max(n,1):.1f}")
    print(f"LLM 实际参与: {llm_used}/{n} 个 case")
    return 0


if __name__ == "__main__":
    sys.exit(main())
