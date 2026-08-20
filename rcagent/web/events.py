"""Web 层事件协议:事件类型、SSE 信封格式化、历史轨迹事件合成。"""

from __future__ import annotations

import json

# 与 core/events.py 的事件常量一致(前端按 type 推导节点状态)
EVENT_TYPES = (
    "run_started", "llm_generating", "llm_generated",
    "parse_ok", "parse_failed",
    "tool_validation_error", "error_detected",
    "tool_started", "tool_finished", "tool_failed",
    "observation_injected", "step_completed",
    "finalize_result", "run_finished",
)


def format_sse(seq: int, run_id: str, ts: float, etype: str, payload: dict) -> str:
    """SSE 信封: id: <seq> + data: JSON(单行,UTF-8)。"""
    envelope = {
        "seq": seq,
        "run_id": run_id,
        "ts": ts,
        "type": etype,
        "payload": payload,
    }
    data = json.dumps(envelope, ensure_ascii=False).replace("\n", "\\n")
    return f"id: {seq}\ndata: {data}\n\n"


def classify_error(message: str) -> str:
    """按错误消息文本分类(历史轨迹无事件时间线时的近似还原)。

    与 agent.py 内 ERROR_FEEDBACK 模板同仓库,分类稳定。
    """
    if "does not exist" in message:
        return "unknown_tool"
    if "undeclared parameter" in message:
        return "unknown_params"
    if "missing required parameter" in message:
        return "missing_params"
    if "already invoked" in message or "trivial" in message \
            or "before thorough investigation" in message:
        return "error_detected"
    if "failed:" in message:
        return "tool_failed"
    if "finalize requires" in message:
        return "finalize_invalid"
    return "parse_failed"


def derive_events_from_trajectory(traj: dict) -> list[dict]:
    """从历史轨迹 JSON(runs/*.json)合成事件流,供回放渲染。

    旧轨迹没有事件时间线;按 records 的 action/error 字段逐条还原,
    尾部合成 finalize_result + run_finished。新 run 有 .events.json 时
    优先用原始事件(精确回放)。
    """
    events: list[dict] = []
    records = traj.get("records", [])
    started = traj.get("started", 0.0)
    finished = traj.get("finished", started)

    def push(seq: int, ts: float, etype: str, payload: dict) -> None:
        events.append({"seq": seq, "run_id": traj.get("job_id", ""),
                       "ts": ts, "type": etype, "payload": payload})

    seq = 0
    push(seq, started, "run_started", {
        "job_id": traj.get("job_id"), "variant": "archive",
        "decode_mode": "greedy", "max_steps": len(records)})
    for rec in records:
        step = rec.get("step", 0)
        ts = rec.get("t", started)
        raw = rec.get("raw_action", "")
        push(seq := seq + 1, ts, "llm_generating", {"step": step})
        push(seq := seq + 1, ts, "llm_generated", {
            "step": step, "text": raw, "model": "archive",
            "tokens": {"prompt": 0, "completion": 0},
            "penalty_escalations": 0, "latency_ms": 0})
        action = rec.get("action")
        if action:
            push(seq := seq + 1, ts, "parse_ok", {
                "step": step, "thought": rec.get("thought", ""),
                "function": action.get("function"), "kwargs": action.get("kwargs")})
        error = rec.get("error")
        if error:
            kind = classify_error(error)
            push(seq := seq + 1, ts, "tool_validation_error", {
                "step": step, "tool": (action or {}).get("function"),
                "kind": kind, "detail": error})
        obs = rec.get("observation_head", "")
        if obs:
            push(seq := seq + 1, ts, "observation_injected", {
                "step": step, "head": obs,
                "is_error_feedback": obs.startswith("System:")})
        push(seq := seq + 1, ts, "step_completed", {"step": step,
                                                    "feedback_present": bool(obs)})
    result = traj.get("result")
    if result:
        push(seq := seq + 1, finished, "finalize_result", {
            "step": len(records), "result": result})
    push(seq := seq + 1, finished, "run_finished", {
        "status": "passed" if traj.get("status") == "passed" else "failed",
        "steps": len(records),
        "invalid_actions": traj.get("invalid_actions", 0),
        "result": result, "cost": {"prompt_tokens": 0, "completion_tokens": 0,
                                   "estimated_cost_usd": 0.0}})
    return events
