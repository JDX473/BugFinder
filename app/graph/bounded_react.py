"""有界 ReAct harness（PRD §6.3 #2：子 Agent 委派与证据压制）。

**用途**：在"需要 LLM 判断下一步查什么"的调查点上，跑一个**有界**的 ReAct
循环，而不是让 LLM 无界自由发挥（PRD §6.3 #1 的架构约束）。

核心约束（"有界"体现在四处）：
  1. **迭代上限**：`max_iters`（默认 4），LLM 最多决策 4 次，到点强制收敛。
  2. **工具受限**：只有注册进 harness 的工具可调，LLM 无法凭空调任意接口。
  3. **动作结构化**：LLM 每次决策走 `ask_json` 强约束（只能选 tool_name +
     args，或 final_answer），schema 校验拒绝非法动作。
  4. **结论压制**：循环结果压成一条 `Evidence` 写回共享状态，不把 LLM 的
     中间思考/工具调用原文泼出去——互不污染、单点失败不拖垮全局。

**确定性优先**：`llm=None` 时不做循环，直接跑 `fallback`（确定性实现）。
LLM 失败/坏 JSON/超迭代 → 也落到 fallback。只有 LLM 成功产出 final_answer
时才采用循环结论。这保证"LLM 只是增强，不是依赖"。

**输出**：`ReActResult`（结论 dict + 工具调用轨迹 + used_llm）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from app.llm.ask_json import ask_json
from app.llm.protocol import LLMClient
from app.schema.models import Evidence

# 默认迭代上限（PRD §6.3 #2：max_iters≈4）
_DEFAULT_MAX_ITERS = 4
# LLM 决策被采纳的最低置信度（低于此值 → 落 fallback）
_REACT_MIN_CONFIDENCE = 0.5
# 喂回给 LLM 的观察文本单段截断上限（评审 #4/#15：防止 observation 无界累积撑爆上下文）
_OBSERVATION_MAX_CHARS = 500


class ReActTool(Protocol):
    """一个可被 LLM 调用的调查工具（有界动作空间的一个成员）。"""

    name: str  # 工具名（LLM 决策时引用）
    description: str  # 给 LLM 看的用途说明
    args_schema: Mapping[str, Any]  # 参数 JSON Schema（ask_json 校验用）

    def run(self, args: dict) -> str:
        """执行工具，返回观察结果（喂回给 LLM 的下一条上下文）。"""
        ...


@dataclass
class ReActResult:
    """一次有界 ReAct 循环的结论。"""

    conclusion: dict  # 最终结论（final_answer 的 payload）
    used_llm: bool  # True = LLM 决策产出；False = 走了确定性 fallback
    steps: list[dict] = field(default_factory=list)  # 工具调用轨迹（审计用）
    basis: str = ""  # 结论依据摘要

    def to_evidence(self, eid: str, etype: str, source: str) -> Evidence:
        """把结论压成一条 Evidence（证据压制：LLM 的中间思考不进共享状态）。"""
        return Evidence(
            evidence_id=eid,
            type=etype,
            source=source,
            summary=self.basis or (f"ReAct 结论（{'LLM' if self.used_llm else '确定性'}）：{self.conclusion}"),
            payload={"conclusion": self.conclusion, "steps": self.steps, "used_llm": self.used_llm},
        )


# LLM 动作决策 schema：只能选 tool 或 final_answer（二选一，结构强约束）。
_REACT_ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "tool_name": {"type": "string"},
        "args": {"type": "object"},
        "final_answer": {"type": "object"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string"},
    },
    "required": ["confidence"],
    # 不用 additionalProperties:False——DeepSeek 常附带 reason（与场景路由一致）。
}


def _tools_prompt(tools: list[ReActTool]) -> str:
    """把工具清单压成给 LLM 的提示词。"""
    lines = ["可用工具："]
    for t in tools:
        lines.append(f"  - {t.name}: {t.description}")
    lines.append("  - final_answer: 结束调查并给出结论")
    return "\n".join(lines)


def _llm_react_prompt(task: str, tools: list[ReActTool], observation: str = "") -> tuple[str, str]:
    system = (
        "你是一个有界调查代理。每次只能选一个动作：要么调用一个工具（给出参数），"
        "要么给出 final_answer（结束调查）。工具与 final_answer 二选一，不能同时。"
        "最多决策 4 次，尽快收敛到结论。不确定时给低 confidence。"
    )
    user = (
        f"调查任务：{task}\n"
        f"{_tools_prompt(tools)}\n"
        f"当前观察：{observation or '（无，开始调查）'}\n\n"
        "输出 JSON：调用工具 → {tool_name, args, confidence, reason}；"
        "结束调查 → {final_answer: {...结论字段...}, confidence, reason}。"
        "不要包含 final_answer 与 tool_name 两者同有。"
    )
    return system, user


def run_bounded_react(
    *,
    task: str,
    tools: list[ReActTool],
    llm: LLMClient | None = None,
    fallback: Callable[[], dict] | None = None,
    max_iters: int = _DEFAULT_MAX_ITERS,
    observation_prefix: str = "",
) -> ReActResult:
    """跑一个有界 ReAct 循环。

    参数：
      task: 调查任务描述（给 LLM）
      tools: 可调用的工具列表（LLM 只能从这些里选）
      llm: 可选 LLMClient（None → 直接 fallback，纯确定性）
      fallback: 确定性兜底（无参函数，返回结论 dict）
      max_iters: 循环迭代上限（默认 4）
      observation_prefix: 初始观察（给 LLM 的第一条上下文）

    返回：ReActResult（结论 + 轨迹 + 是否用了 LLM）。
    """
    steps: list[dict] = []

    # 确定性优先：无 LLM → 直接跑 fallback
    if llm is None:
        conclusion = fallback() if fallback else {"status": "no_llm"}
        return ReActResult(
            conclusion=conclusion,
            used_llm=False,
            steps=steps,
            basis=f"无 LLM，确定性兜底：{conclusion}",
        )

    tool_map = {t.name: t for t in tools}
    observation = observation_prefix
    final: dict | None = None
    used_llm = False

    for it in range(max_iters):
        system, user = _llm_react_prompt(task, tools, observation)
        try:
            result = ask_json(
                llm, system, user, _REACT_ACTION_SCHEMA,
                fallback=lambda: {"tool_name": "", "final_answer": {}, "confidence": 0.0},
                temperature=0.0,
            )
        except Exception:
            # LLM 兜底自身失败 → 落确定性 fallback
            break
        if result is None or result.data is None or not result.ok:
            break  # 结构化解析失败 → fallback

        data = result.data
        conf = float(data.get("confidence", 0.0))
        tool_name = str(data.get("tool_name", "") or "")
        final_ans = data.get("final_answer")

        # 动作合法性校验（结构强约束）。评审 #26：final_answer 优先——
        # 部分模型会同时带 tool_name 和 final_answer，此时视为想结束调查，
        # 不要误当成工具调用继续循环。
        if final_ans and isinstance(final_ans, dict):
            if conf >= _REACT_MIN_CONFIDENCE:
                final = final_ans
                used_llm = True
                steps.append({"iter": it, "action": "final_answer", "confidence": conf})
            break  # 无论置信度，final_answer 都终止循环（低置信 → 用 fallback）

        if tool_name and tool_name in tool_map:
            try:
                obs = tool_map[tool_name].run(dict(data.get("args", {}) or {}))
            except Exception as e:
                obs = f"工具执行失败：{e}"
            # 评审 #4/#15：观察喂回 LLM 前截断，防止无界累积撑爆上下文
            obs_trunc = obs if len(obs) <= _OBSERVATION_MAX_CHARS else obs[:_OBSERVATION_MAX_CHARS] + "…"
            steps.append({"iter": it, "action": f"tool:{tool_name}", "args": data.get("args", {}), "observation": obs_trunc})
            observation = (observation + "\n" + obs_trunc).strip() if observation else obs_trunc
            continue  # 调用工具后继续循环

        # 非法动作（未知工具 / 同有两者 / 无动作）→ 中断，落 fallback
        break

    if final is not None and used_llm:
        return ReActResult(
            conclusion=final,
            used_llm=True,
            steps=steps,
            basis=f"ReAct（{len(steps)} 步）：{final}",
        )

    # 未收敛 / LLM 失败 / 低置信 → 确定性兜底
    conclusion = fallback() if fallback else {"status": "fallback"}
    return ReActResult(
        conclusion=conclusion,
        used_llm=False,
        steps=steps,
        basis=f"ReAct 未收敛（{len(steps)} 步），确定性兜底：{conclusion}",
    )
