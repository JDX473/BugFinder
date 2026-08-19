"""Controller Agent 主循环(论文 §III):thought-action-observation 循环。

流程: 组装 prompt → LLM 生成 → JsonRegen 解析 → 工具校验/错误检测 →
执行工具(参数经 OBSK snapshot 解析)→ 观察注入 → 循环,直至 finalize
或达到步数上限。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from ..config import Config
from ..llm.client import LLMClient
from ..llm.decode import generate
from . import prompts as prompt_builder
from .errors import ErrorDetector
from .obs import SnapshotStore
from .parser import ParseFailure, parse_action
from .tools import FINALIZE_NAME, ToolError, ToolRegistry, make_finalize_spec
from .trajectory import STATUS_FAILED, STATUS_PASSED, StepRecord, Trajectory

logger = logging.getLogger(__name__)

UNCLEAR = "Unclear"  # 论文 §IV-C: 失败/不完整结果自动填 baseline content

OBSERVATION_FEEDBACK = "Observation:\n{head}"
ERROR_FEEDBACK = "System: {message}"


@dataclass
class JobDesc:
    """异常作业/实例描述(环境适配层提供的运行时输入)。"""

    job_id: str
    anomaly: str       # 异常描述(不可恢复失败/启动失败等)
    detect_time: str   # 检测时刻,数据访问截止约束
    extra: dict = field(default_factory=dict)


VARIANTS = ("full", "react", "no_experts", "no_jsonregen", "no_obsk", "no_obs_head")


class RCAgent:
    def __init__(
        self,
        cfg: Config,
        llm: LLMClient,
        registry: ToolRegistry,
        store: SnapshotStore,
        detector: ErrorDetector,
        *,
        variant: str = "full",
        framework_rules: str | None = None,
        task_requirements: str | None = None,
    ):
        self.cfg = cfg
        self.llm = llm
        self.registry = registry
        self.store = store
        self.detector = detector
        self.variant = variant
        self.framework_rules = framework_rules or prompt_builder.build_framework_rules()
        self.task_requirements = task_requirements or prompt_builder.build_task_requirements()

    # -- 变体(消融/基线) --------------------------------------------------

    @property
    def use_regen(self) -> bool:
        """JsonRegen 开关(论文 §III-C1): react/no_jsonregen 关闭。"""
        return self.variant not in ("react", "no_jsonregen")

    @property
    def obs_mode(self) -> str:
        """OBSK 观察模式(论文 §III-A/消融): full | no_obsk | no_obs_head。"""
        if self.variant in ("no_obsk", "react"):
            return "no_obsk"
        if self.variant == "no_obs_head":
            return "no_obs_head"
        return "full"

    # -- 构造 ------------------------------------------------------------

    @classmethod
    def build(cls, cfg: Config, llm: LLMClient, env, *, variant: str | None = None,
              framework_rules=None, task_requirements=None) -> "RCAgent":
        """从配置与目标服务环境构建完整 agent(工具注册 + finalize + 检测器)。

        variant 控制消融/基线(论文 §V-B): full 默认;react 关闭全部增强;
        no_experts / no_jsonregen / no_obsk / no_obs_head 单组件消融。
        """
        variant = variant or cfg.agent.get("variant", "full")
        if variant not in VARIANTS:
            raise ValueError(f"unknown variant {variant!r}; choose from {VARIANTS}")
        store = SnapshotStore()
        a = cfg.agent
        registry = ToolRegistry(
            store=store,
            obs_head_chars=a.obs_head_chars,
            dedup_ratio=cfg.tools.dedup_ratio,
            max_obs_chars=cfg.tools.max_obs_chars,
        )
        registry.register(make_finalize_spec(a.finalize_required_fields))
        env.register_tools(registry, include_experts=variant not in ("react", "no_experts"))
        detector = ErrorDetector(
            cfg.agent,
            tool_names=set(registry.names()),
            expert_names=set(env.expert_tool_names()),
            enabled=variant != "react",
        )
        return cls(
            cfg, llm, registry, store, detector, variant=variant,
            framework_rules=framework_rules, task_requirements=task_requirements,
        )

    # -- 主循环 ------------------------------------------------------------

    def run(self, job: JobDesc, *, decode_mode: str = "greedy",
            max_steps: int | None = None) -> Trajectory:
        """完整轨迹:重置检测器 → 初始消息 → 循环到 finalize 或步数上限。"""
        self.detector.reset()
        messages = self._initial_messages(job)
        traj = Trajectory(job_id=job.job_id)
        result = self._loop(messages, job, traj, decode_mode=decode_mode,
                            max_steps=max_steps)
        traj.finished = time.time()
        traj.status = STATUS_PASSED if result is not None else STATUS_FAILED
        traj.result = result
        return traj

    def replay_messages(self, job: JobDesc, traj: Trajectory,
                        n_kept: int) -> list[dict]:
        """从轨迹 records 重建历史消息(1..n_kept 步的 assistant+user 对)。

        供 TSC 从倒数第二步重放:主轨迹 records 保存了每步原始输出与
        注入的反馈,可精确重建送入 LLM 的消息序列。
        """
        msgs = self._initial_messages(job)
        for rec in traj.records[:n_kept]:
            msgs.append({"role": "assistant", "content": rec.raw_action})
            msgs.append({"role": "user", "content": rec.observation_head})
        return msgs

    def fork_sampler(self, inherit_detector: bool = True) -> "RCAgent":
        """创建采样子轨迹用的 agent:共享 llm/store/registry。

        inherit_detector=True 时继承当前轨迹的错误检测器状态
        (已调查工具/调用历史)——子轨迹重放了主轨迹的调查过程,
        其 finalize 不应被"过早 finalize"误拦截。
        """
        detector = ErrorDetector(
            self.cfg.agent,
            tool_names=set(self.registry.names()),
            expert_names=set(self.registry.expert_names()),
        )
        if inherit_detector:
            detector.calls = list(self.detector.calls)
            detector.info_tool_calls = set(self.detector.info_tool_calls)
        return RCAgent(
            self.cfg, self.llm, self.registry, self.store, detector,
            variant=self.variant,
            framework_rules=self.framework_rules,
            task_requirements=self.task_requirements,
        )

    def _initial_messages(self, job: JobDesc) -> list[dict]:
        system_prompt = prompt_builder.build_system_prompt(
            self.registry, self.task_requirements, self.framework_rules
        )
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt_builder.build_user_prompt(self._describe(job))},
        ]

    def _loop(self, messages: list[dict], job: JobDesc, traj: Trajectory, *,
              decode_mode: str, max_steps: int | None = None) -> dict | None:
        """循环主体:生成→解析→执行→观察,直至 finalize(返回四项)或步数耗尽(None)。"""
        cfg = self.cfg
        max_steps = max_steps or cfg.agent.max_steps

        for step in range(1, max_steps + 1):
            gen = generate(self.llm, cfg, messages, mode=decode_mode)
            text = gen.text
            record = StepRecord(
                step=step,
                thought="",
                raw_action=text,
                prompt_text=json_dump(messages) if cfg.trajectory.save_prompt_text else "",
                llm_meta={"tokens": gen.completion_tokens, "model": gen.model,
                          "penalty_escalations": gen.extra.get("penalty_escalations", 0)},
                t=time.time(),
            )
            feedback = self._handle_step(step, text, job, record, traj)
            record.observation_head = feedback if feedback is not None else ""
            traj.add(record)

            if feedback is None:  # finalize 成功
                return record.action["kwargs"] if record.action else None
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": feedback})
        return None

    def _describe(self, job: JobDesc) -> str:
        return (
            f"An anomaly has been detected on job/instance '{job.job_id}'.\n"
            f"Anomaly: {job.anomaly}\n"
            f"Detection time: {job.detect_time}\n"
            "Investigate the root cause using the available tools and report the "
            "final result with the finalize tool."
        )

    def _handle_step(self, step: int, text: str, job: JobDesc, record: StepRecord,
                     traj: Trajectory) -> str | None:
        """处理单步:解析→校验→执行。返回注入的反馈文本;finalize 成功返回 None。"""
        cfg = self.cfg.agent
        parsed = parse_action(text, use_regen=self.use_regen)
        if isinstance(parsed, ParseFailure):
            record.error = parsed.reason
            traj.invalid_actions += 1
            return ERROR_FEEDBACK.format(
                message=f"Error: {parsed.reason}. Respond with a valid JSON action in "
                        "{'function': ..., 'kwargs': {...}} format."
            )

        record.thought = parsed.thought
        record.action = {"function": parsed.function, "kwargs": parsed.kwargs}
        tool = parsed.function
        kwargs = parsed.kwargs

        spec = self.registry.get(tool)
        if spec is None:
            record.error = f"unknown tool: {tool}"
            traj.invalid_actions += 1
            return ERROR_FEEDBACK.format(
                message=f"Error: tool '{tool}' does not exist. Use only tools from the "
                        "TOOLS DOCUMENTATION."
            )

        # 参数校验:仅接受已声明参数,且必填参数齐备
        unknown = set(kwargs) - set(spec.params)
        if unknown:
            record.error = f"unknown params: {sorted(unknown)}"
            traj.invalid_actions += 1
            return ERROR_FEEDBACK.format(
                message=f"Error: tool '{tool}' received undeclared parameter(s) "
                        f"{sorted(unknown)}. Declared: {sorted(spec.params)}."
            )
        missing = [p for p in spec.params if p not in kwargs]
        if missing:
            record.error = f"missing params: {missing}"
            traj.invalid_actions += 1
            return ERROR_FEEDBACK.format(
                message=f"Error: tool '{tool}' is missing required parameter(s) "
                        f"{missing}."
            )

        # 论文 §III-C2 错误处理: 重复调用 / trivial 输入 / 过早 finalize
        err_msg = self.detector.detect(tool, kwargs, step)
        if err_msg is not None:
            record.error = err_msg
            traj.invalid_actions += 1
            return ERROR_FEEDBACK.format(message=err_msg)

        self.detector.record_call(tool, kwargs, step)

        # snapshot 解析: 工具参数中的快照键替换为完整内容
        resolved = {k: self.store.resolve(v) for k, v in kwargs.items()}

        if tool == FINALIZE_NAME:
            result = self._extract_finalize(resolved, record)
            if result is None:
                traj.invalid_actions += 1
                return ERROR_FEEDBACK.format(
                    message="Error: finalize requires all fields "
                            f"{cfg.finalize_required_fields}; missing or invalid fields."
                )
            record.action["kwargs"] = result
            return None

        # 执行工具
        try:
            tool_result = self.registry.call(tool, resolved, job, obs_mode=self.obs_mode)
        except ToolError as e:
            record.error = str(e)
            traj.invalid_actions += 1
            return ERROR_FEEDBACK.format(message=f"Error: tool '{tool}' failed: {e}")

        record.snapshot = tool_result.snapshot
        head = tool_result.head
        if tool_result.snapshot is not None:
            self.detector.record_info_tool(tool)
        return OBSERVATION_FEEDBACK.format(head=head)

    def _extract_finalize(self, kwargs: dict, record: StepRecord) -> dict | None:
        cfg = self.cfg.agent
        result: dict = {}
        for f in cfg.finalize_required_fields:
            v = kwargs.get(f)
            if not isinstance(v, str) or not v.strip():
                record.error = f"finalize missing field: {f}"
                return None
            result[f] = v.strip()
        return result


def json_dump(messages: list[dict]) -> str:
    import json

    return json.dumps(messages, ensure_ascii=False)
