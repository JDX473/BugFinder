"""评估 CLI(论文 §IV-C):批量跑轨迹 → 语义指标 + 轨迹统计 + LLM 评估。

用法:
  python -m rcagent.eval [--job-dir data/demo_jobs] [--metrics METEOR,EmbScore]
                         [--method rcagent] [--mock] [--judge] [--out runs/eval]
  python -m rcagent.eval --win-rate runs/eval/rcagent.json runs/eval/react.json

第一步先跑轨迹并保存结果 JSON;--win-rate 对比两个已保存结果文件。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..config import load_config
from ..core.agent import JobDesc, RCAgent
from ..env.local import LocalEnvironment, load_job
from ..llm.embedding import Embedder
from .metrics import UNCLEAR, METRIC_NAMES, MetricUnavailable, compute
from .report import MethodEval, render_table, render_trajectory_stats

FIELDS = ["root_cause", "solution", "evidence"]


def _resolve_metric(metric: str, embedder) -> callable:
    def fn(preds, refs):
        return compute(metric, preds, refs, embedder)
    return fn


def run_batch(cfg, job_dir: str | Path, *, force_mock: bool,
              metrics: list[str], judge: bool, out: str | Path,
              variant: str | None = None, limit: int | None = None) -> int:
    """对数据集所有 job 跑轨迹并计算语义指标与轨迹统计。"""
    from ..experts.knowledge import build_demo_kb

    variant = variant or cfg.agent.get("variant", "full")
    job_dir = Path(job_dir)
    jobs = sorted(p.name for p in job_dir.iterdir() if (p / "job.json").exists())
    if limit:
        jobs = jobs[:limit]
    if not jobs:
        print(f"no jobs under {job_dir}")
        return 1

    llm_cfg = cfg.llm
    if force_mock or llm_cfg.provider == "mock":
        from ..llm.client import LLMClient
        from ..main import make_demo_mock

        embedder = Embedder(cfg.embedding)
        # 每个 job 的 mock 结果直接来自 ground truth(评估流水线验证用)
        preds = {f: [] for f in FIELDS}
        stats = {"pass": 0, "invalid": 0, "steps": 0}
        for job_id in jobs:
            meta = load_job(job_id, job_dir)
            gt = meta.get("ground_truth", {})
            llm = LLMClient(llm_cfg, mock_script=make_demo_mock(
                job_id, {f: gt.get(f, UNCLEAR) for f in FIELDS + ["responsibility"]}))
            env = LocalEnvironment(data_dir=job_dir, llm=llm, embedder=embedder,
                                   kb=build_demo_kb(embedder))
            agent = RCAgent.build(cfg, llm, env, variant=variant)
            traj = agent.run(JobDesc(job_id=job_id, anomaly=meta["anomaly"],
                                     detect_time=meta["detect_time"]))
            r = traj.result
            stats["pass"] += 1 if traj.passed else 0
            stats["invalid"] += traj.invalid_actions
            stats["steps"] += len(traj.records)
            for f in FIELDS:
                preds[f].append((r or {}).get(f, UNCLEAR))
        results = {
            "method": "rcagent(mock)",
            "predictions": preds,
            "references": {f: [load_job(j, job_dir)["ground_truth"].get(f, UNCLEAR)
                               for j in jobs] for f in FIELDS},
            "trajectory": {
                "pass_rate": stats["pass"] / len(jobs),
                "invalid_rate": stats["invalid"] / stats["steps"] if stats["steps"] else 0,
                "avg_steps": stats["steps"] / len(jobs),
            },
            "jobs": jobs,
        }
    else:
        from ..llm.client import LLMClient

        embedder = Embedder(cfg.embedding)
        llm = LLMClient(llm_cfg)
        env = LocalEnvironment(llm=llm, embedder=embedder, kb=build_demo_kb(embedder))
        preds = {f: [] for f in FIELDS}
        refs = {f: [] for f in FIELDS}
        stats = {"pass": 0, "invalid": 0, "steps": 0}
        for job_id in jobs:
            meta = load_job(job_id, job_dir)
            gt = meta.get("ground_truth", {})
            agent = RCAgent.build(cfg, llm, env)
            traj = agent.run(JobDesc(job_id=job_id, anomaly=meta["anomaly"],
                                     detect_time=meta["detect_time"]))
            r = traj.result
            stats["pass"] += 1 if traj.passed else 0
            stats["invalid"] += traj.invalid_actions
            stats["steps"] += len(traj.records)
            for f in FIELDS:
                preds[f].append((r or {}).get(f, UNCLEAR))
                refs[f].append(gt.get(f, UNCLEAR))
        results = {
            "method": f"rcagent({llm_cfg.provider})",
            "variant": variant,
            "predictions": preds,
            "references": refs,
            "trajectory": {
                "pass_rate": stats["pass"] / len(jobs),
                "invalid_rate": stats["invalid"] / stats["steps"] if stats["steps"] else 0,
                "avg_steps": stats["steps"] / len(jobs),
            },
            "jobs": jobs,
        }

    # 语义指标
    me = MethodEval(name=results["method"])
    me.pass_rate = results["trajectory"]["pass_rate"]
    me.invalid_rate = results["trajectory"]["invalid_rate"]
    me.avg_steps = results["trajectory"]["avg_steps"]
    for f in FIELDS:
        for mt in metrics:
            try:
                scores = compute(mt, results["predictions"][f], results["references"][f],
                                 embedder)
                me.add_scores(f, mt, scores)
            except (MetricUnavailable, ValueError) as e:
                print(f"  [skip] {mt}: {e}", file=sys.stderr)

    # LLM 评估(可选)
    if judge and not (force_mock or llm_cfg.provider == "mock"):
        from .judge import judge_score

        kind = "correctness"
        g = []
        for p, r in zip(results["predictions"]["root_cause"],
                        results["references"]["root_cause"]):
            g.append(judge_score(llm, kind, p, r))
        me.add_scores("root_cause", "G-Correctness", g)

    out_path = Path(out) / "results.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"jobs={len(jobs)}  method={results['method']}")
    print(render_table([me], metrics))
    print()
    print(render_trajectory_stats([me]))
    print(f"\nresults -> {out_path}")
    return 0


def compare_win_rate(a_path: str, b_path: str, cfg) -> int:
    """对比两个结果文件的方法,输出 Win Rate(需 API key)。"""
    from ..llm.client import LLMClient
    from .judge import compute_win_rate

    a = json.loads(Path(a_path).read_text(encoding="utf-8"))
    b = json.loads(Path(b_path).read_text(encoding="utf-8"))
    llm = LLMClient(cfg.llm)
    for f in FIELDS:
        rate, verdicts = compute_win_rate(
            llm, a["predictions"][f], b["predictions"][f], a["references"][f])
        print(f"{f}: {rate:.2%} ({verdicts.count('BETTER')} better / "
              f"{verdicts.count('EQUAL')} equal / {verdicts.count('WORSE')} worse)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="RCAgent evaluation (paper §IV-C)")
    ap.add_argument("--job-dir", default="data/demo_jobs")
    ap.add_argument("--metrics", default="METEOR,EmbScore",
                    help=f"comma-separated from {METRIC_NAMES}")
    ap.add_argument("--variant", default=None,
                    help="agent variant: full|react|no_experts|no_jsonregen|no_obsk|no_obs_head")
    ap.add_argument("--limit", type=int, default=None, help="only evaluate first N jobs")
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--judge", action="store_true", help="run LLM G-Correctness")
    ap.add_argument("--out", default="runs/eval")
    ap.add_argument("--win-rate", nargs=2, metavar=("A_JSON", "B_JSON"),
                    help="compare two saved result files")
    ap.add_argument("--config", default=None)
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    if args.win_rate:
        return compare_win_rate(*args.win_rate, cfg)
    metrics = [m.strip() for m in args.metrics.split(",") if m.strip()]
    return run_batch(cfg, args.job_dir, force_mock=args.mock, metrics=metrics,
                     judge=args.judge, out=args.out, variant=args.variant,
                     limit=args.limit)


if __name__ == "__main__":
    sys.exit(main())
