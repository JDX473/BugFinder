"""TSC(论文 §III-D2, 图5):Trajectory-level Self-Consistency。

完整轨迹级 SC 从第一步采样开销过大(expert 激活昂贵),且随机采样缺少
历史示例会导致错误动作泛滥(论文实测 RCAgent+Sampling 崩溃至 70% Pass
Rate)。TSC 仅在 controller 进入 finalization 时从倒数第二步开始采样:

  主轨迹(greedy):   step1 → ... → step_{t-1} → finalize
                                    ↓ 从 step_{t-1} 起采样 K 条
  子轨迹(采样解码):  step'_{t-1} → finalize \
                     step'_{t-1} → step'_t → finalize  } 自由 0~N 步
                     ...                                /  直到 finalize 或上限
  聚合: K+1 个候选 → embedding 投票 或 LLM 聚合

greedy 主轨迹的稳定 action history 充当 few-shot 示例(不额外消耗上下文),
抑制采样阶段的有效性下降。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Config
from ..core.agent import JobDesc, RCAgent
from ..core.parser import ParseFailure, parse_action
from ..core.tools import FINALIZE_NAME
from ..core.trajectory import Trajectory
from ..llm.client import LLMClient
from ..llm.embedding import Embedder
from ..llm.decode import generate
from .aggregate import aggregate_by_embedding, aggregate_by_llm


@dataclass
class TSCResult:
    result: dict | None                  # 聚合后的最终四项
    main_traj: Trajectory
    sub_trajs: list[Trajectory] = field(default_factory=list)
    method: str = "tsc"
    aggregate: str = "llm"

    @property
    def sample_pass_rate(self) -> float:
        if not self.sub_trajs:
            return 0.0
        return sum(1 for s in self.sub_trajs if s.passed) / len(self.sub_trajs)


class TSCRunner:
    def __init__(self, agent: RCAgent, cfg: Config, embedder: Embedder,
                 llm: LLMClient | None = None):
        self.agent = agent
        self.cfg = cfg
        self.embedder = embedder
        self.llm = llm or agent.llm

    def _sc_cfg(self):
        return self.cfg.get("sc") or {}

    def run(self, job: JobDesc, *, samples: int | None = None,
            method: str | None = None, aggregate: str | None = None,
            max_steps: int | None = None) -> TSCResult:
        sc_cfg = self._sc_cfg()
        samples = samples or sc_cfg.get("samples", 10)
        method = method or sc_cfg.get("method", "tsc")
        aggregate = aggregate or sc_cfg.get("aggregate", "llm")
        max_steps = max_steps or self.cfg.agent.max_steps
        required = list(self.cfg.agent.finalize_required_fields)

        # 1. greedy 主轨迹
        main = self.agent.run(job, decode_mode="greedy", max_steps=max_steps)
        if not main.passed:
            return TSCResult(result=None, main_traj=main, method=method, aggregate=aggregate)

        # 2. 从倒数第二步重放采样(主轨迹最后一步是 finalize, 保留其前 t-2 步历史)
        keep = max(0, len(main.records) - 2)
        base_messages = self.agent.replay_messages(job, main, keep)
        subs: list[Trajectory] = []
        for k in range(samples):
            sampler = self.agent.fork_sampler()
            sub = Trajectory(job_id=f"{job.job_id}#s{k}")
            if method == "sc":
                # 步进 SC: 仅采样 thinking + finalize,不允许额外动作步骤
                result = self._synchronous_sample(sampler, base_messages)
            else:
                # TSC: 自由 0~N 步,直至 finalize 或全局上限
                result = sampler._loop(base_messages, job, sub,
                                       decode_mode="sampling", max_steps=max_steps)
            sub.finished = sub.started
            sub.status = "passed" if result is not None else "failed"
            sub.result = result
            subs.append(sub)

        # 3. 聚合 K+1 个候选
        candidates = [main.result] + [s.result for s in subs]
        if aggregate == "embedding":
            final = aggregate_by_embedding(candidates, self.embedder, required)
        else:
            final = aggregate_by_llm(self.llm, candidates, required)
            if final is None:  # LLM 聚合失败,退化为主轨迹结果
                final = main.result
        return TSCResult(result=final, main_traj=main, sub_trajs=subs,
                         method=method, aggregate=aggregate)

    def _synchronous_sample(self, sampler: RCAgent, messages: list[dict]) -> dict | None:
        """步进 SC(论文 §IV-A): 只采样与 greedy 同步 finalize 的样本。

        仅允许生成 finalize 动作;生成到其它动作或解析失败则丢弃该样本。
        """
        from ..core.trajectory import StepRecord

        gen = generate(self.llm, self.cfg, messages, mode="sampling")
        parsed = parse_action(gen.text)
        if isinstance(parsed, ParseFailure) or parsed.function != FINALIZE_NAME:
            return None
        return sampler._extract_finalize(parsed.kwargs, StepRecord(step=0, thought="",
                                                                   raw_action=gen.text))
