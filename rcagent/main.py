"""CLI 入口:python -m rcagent [--job <id>] [--mock] [--decode greedy|sampling]

mock 模式无需 API key,用于验证循环/错误处理/OBSK 逻辑;
真实模式需要环境变量 DEEPSEEK_API_KEY(config.llm.api_key_env)。
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

from .config import load_config
from .core.agent import JobDesc, RCAgent
from .env.local import LocalEnvironment, load_job

logger = logging.getLogger(__name__)


def make_demo_mock(job_id: str, finalize_result: dict):
    """可编程 mock LLM: 按阶段返回脚本化动作,模拟完整 RCA 轨迹。

    阶段0: runtime_log → 阶段1: log_agent(从上一步观察提取真实 snapshot key)
    → 阶段2: finalize。演示 OBSK 快照传参与最终出口。
    """
    phase = {"i": 0}

    def _mock(messages, params):
        last_user = messages[-1]["content"] if messages else ""
        i = phase["i"]
        phase["i"] += 1
        if i == 0:
            return (
                'Thought: 我需要查看该作业的运行时日志。'
                f'\nFunction: {{"function": "runtime_log", "kwargs": {{"job_id": "{job_id}"}}}}'
            )
        if i == 1:
            snap = re.search(r"\[snapshot: (\d{10})\]", last_user)
            key = snap.group(1) if snap else "0000000000"
            return (
                'Thought: 日志较长,使用 log agent 分析完整日志。'
                f'\nFunction: {{"function": "log_agent", "kwargs": {{"snapshot": "{key}"}}}}'
            )
        return (
            "Thought: 分析完成,证据充分,报告最终结果。"
            f'\nFunction: {{"function": "finalize", "kwargs": '
            f'{json.dumps(finalize_result, ensure_ascii=False)}}}'
        )

    return _mock


def run_one(cfg, job_id: str, *, force_mock: bool, decode_mode: str,
            out_dir: str | Path, sc_method: str | None, sc_samples: int | None,
            variant: str | None = None, env_name: str = "demo") -> int:
    if env_name == "im":
        from .env.im_env import IMEnvironment, IM_JOBS_DIR
        from .experts.knowledge import build_im_kb

        env_cls = IMEnvironment
        data_dir = IM_JOBS_DIR
        kb_fn = build_im_kb
        task_requirements = (Path(__file__).resolve().parent.parent
                             / "config" / "prompts" / "task_requirements_im.txt").read_text(
            encoding="utf-8")
    else:
        from .env.local import LocalEnvironment, DATA_DIR
        from .experts.knowledge import build_demo_kb

        env_cls = LocalEnvironment
        data_dir = DATA_DIR
        kb_fn = build_demo_kb
        task_requirements = None  # 用默认模板

    meta = load_job(job_id, data_dir)

    llm_cfg = cfg.llm
    if force_mock or llm_cfg.provider == "mock":
        from .llm.client import LLMClient

        gt = meta.get("ground_truth", {})
        finalize_result = {
            "root_cause": gt.get("root_cause", "unknown"),
            "solution": gt.get("solution", "unknown"),
            "evidence": "observed fatal error lines in runtime log",
            "responsibility": gt.get("responsibility", "platform"),
        }
        llm = LLMClient(llm_cfg, mock_script=make_demo_mock(job_id, finalize_result))
        mode = "mock"
    else:
        from .llm.client import LLMClient

        llm = LLMClient(llm_cfg)
        mode = f"{llm_cfg.provider}:{llm_cfg.model}"

    from .llm.embedding import Embedder

    embedder = Embedder(cfg.embedding)
    kb = kb_fn(embedder)
    env = env_cls(llm=llm, embedder=embedder, kb=kb)
    agent = RCAgent.build(cfg, llm, env, variant=variant,
                          task_requirements=task_requirements)
    job = JobDesc(
        job_id=meta["job_id"],
        anomaly=meta["anomaly"],
        detect_time=meta["detect_time"],
    )

    sc_cfg = cfg.get("sc") or {}
    method = sc_method if sc_method is not None else sc_cfg.get("method", "none")

    if method != "none":
        from .sc.tsc import TSCRunner

        runner = TSCRunner(agent, cfg, embedder)
        res = runner.run(
            job,
            samples=sc_samples if sc_samples is not None else sc_cfg.get("samples", 10),
            method=method,
            aggregate=sc_cfg.get("aggregate", "llm"),
        )
        main_path = res.main_traj.save(cfg.trajectory.dir,
                                       save_prompt=cfg.trajectory.save_prompt_text)
        print(f"[{mode}] job={job_id} method={res.method} aggregate={res.aggregate} "
              f"K={len(res.sub_trajs)}")
        print(f"  main trajectory: {'PASSED' if res.main_traj.passed else 'FAILED'} "
              f"steps={len(res.main_traj.records)} "
              f"invalid={res.main_traj.invalid_actions} -> {main_path}")
        print(f"  samples: pass_rate={res.sample_pass_rate:.0%} "
              f"steps={[len(s.records) for s in res.sub_trajs]}")
        print(f"  result: {json.dumps(res.result, ensure_ascii=False, indent=2)}")
        print(f"  llm cost: {llm.cost_estimate()}")
        return 0 if res.result is not None else 1

    traj = agent.run(job, decode_mode=decode_mode)
    path = traj.save(cfg.trajectory.dir, save_prompt=cfg.trajectory.save_prompt_text)

    print(f"[{mode}] job={job_id} decode={decode_mode}")
    print(f"  status: {'PASSED' if traj.passed else 'FAILED'}  steps={len(traj.records)}"
          f"  invalid={traj.invalid_actions}")
    print(f"  result: {json.dumps(traj.result, ensure_ascii=False, indent=2)}")
    print(f"  trajectory: {path}")
    print(f"  llm cost: {llm.cost_estimate()}")
    return 0 if traj.passed else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="RCAgent reproduction - run an RCA trajectory")
    ap.add_argument("--job", default="demo_es_conn_timeout", help="job id in data/demo_jobs")
    ap.add_argument("--mock", action="store_true", help="force mock LLM (no API key needed)")
    ap.add_argument("--decode", choices=["greedy", "sampling"], default=None)
    ap.add_argument("--sc", choices=["none", "tsc", "sc"], default=None,
                    help="self-consistency method (overrides config)")
    ap.add_argument("--samples", type=int, default=None, help="SC sample count K")
    ap.add_argument("--variant", default=None,
                    help="agent variant: full|react|no_experts|no_jsonregen|no_obsk|no_obs_head")
    ap.add_argument("--env", choices=["demo", "im"], default="demo",
                    help="target service environment: demo(合成) | im(QuantumLink IM 真实服务)")
    ap.add_argument("--config", default=None)
    ap.add_argument("--list-jobs", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.WARNING)

    cfg = load_config(args.config)
    if args.list_jobs:
        from .env.local import DATA_DIR

        jobs = sorted(p.name for p in Path(DATA_DIR).iterdir() if (p / "job.json").exists())
        print("\n".join(jobs) if jobs else "(no demo jobs; run 'python -m rcagent.env.local --generate')")
        return 0

    return run_one(cfg, args.job, force_mock=args.mock,
                   decode_mode=args.decode or "greedy", out_dir=cfg.trajectory.dir,
                   sc_method=args.sc, sc_samples=args.samples, variant=args.variant,
                   env_name=args.env)


if __name__ == "__main__":
    sys.exit(main())
