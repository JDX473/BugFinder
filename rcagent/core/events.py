"""Agent 运行事件契约:可视化(Web 运行时)与取消协作。

RCAgent 通过可选参数 `events: AgentEvents | None` 接入:
- 默认 None 时 emit 为 no-op、cancelled 恒 False —— 现有测试与 CLI 零影响;
- Web 运行时传入带 sink 的实例,每步执行向 sink 发射事件(类型见
  `rcagent/web/events.py` 的 EVENT_TYPES),供 SSE 推送与前端节点高亮;
- cancel: threading.Event,agent 在步边界检查,实现协作式取消。
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

logger = logging.getLogger(__name__)

# 事件类型(与 web/events.py 的 EVENT_TYPES 保持一致)
EVENT_RUN_STARTED = "run_started"
EVENT_LLM_GENERATING = "llm_generating"
EVENT_LLM_GENERATED = "llm_generated"
EVENT_PARSE_OK = "parse_ok"
EVENT_PARSE_FAILED = "parse_failed"
EVENT_TOOL_VALIDATION_ERROR = "tool_validation_error"
EVENT_ERROR_DETECTED = "error_detected"
EVENT_TOOL_STARTED = "tool_started"
EVENT_TOOL_FINISHED = "tool_finished"
EVENT_TOOL_FAILED = "tool_failed"
EVENT_OBSERVATION_INJECTED = "observation_injected"
EVENT_STEP_COMPLETED = "step_completed"
EVENT_FINALIZE_RESULT = "finalize_result"
EVENT_RUN_FINISHED = "run_finished"


class AgentEvents:
    """agent 可选运行钩子:事件发射 + 协作取消。默认全部为 no-op。"""

    def __init__(
        self,
        sink: Callable[[str, dict], None] | None = None,
        cancel: threading.Event | None = None,
    ):
        self.sink = sink
        self.cancel = cancel

    def emit(self, etype: str, **payload) -> None:
        if self.sink is not None:
            try:
                self.sink(etype, payload)
            except Exception:  # noqa: BLE001 — 事件失败不得影响 agent 执行
                logger.exception("event sink failed for %s", etype)

    @property
    def cancelled(self) -> bool:
        return self.cancel is not None and self.cancel.is_set()
