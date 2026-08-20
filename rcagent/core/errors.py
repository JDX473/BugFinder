"""错误处理(论文 §III-C2):预定义错误检测与反馈。

三类错误,检测到后不终止循环,而是向 controller 注入错误消息与建议,
降低无意义动作频率(对齐 Reflexion 的错误反馈思想):
  (i)   无状态工具以相同参数重复调用
  (ii)  传给 expert agent 的 trivial 输入(过短/无信息量)
  (iii) 未充分调查就过早 finalize
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum

from .tools import FINALIZE_NAME


class ErrorKind(Enum):
    DUPLICATE_CALL = "duplicate_call"
    TRIVIAL_INPUT = "trivial_input"
    EARLY_FINALIZE = "early_finalize"


@dataclass
class CallRecord:
    tool: str
    args_hash: str
    step: int


class ErrorDetector:
    def __init__(self, cfg, tool_names: set[str], expert_names: set[str],
                 enabled: bool = True):
        self.cfg = cfg
        self.tool_names = tool_names
        self.expert_names = expert_names
        self.enabled = enabled
        self.calls: list[CallRecord] = []
        self.info_tool_calls: set[str] = set()  # 已成功调用的信息收集工具名
        self.last_kind: ErrorKind | None = None  # 最近一次 detect 命中的错误类型(事件用)

    def _args_key(self, kwargs: dict) -> str:
        return hashlib.md5(
            json.dumps(kwargs, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()

    def reset(self) -> None:
        self.calls.clear()
        self.info_tool_calls.clear()

    def record_call(self, tool: str, kwargs: dict, step: int) -> None:
        self.calls.append(CallRecord(tool, self._args_key(kwargs), step))

    def record_info_tool(self, tool: str) -> None:
        self.info_tool_calls.add(tool)

    def detect(self, tool: str, kwargs: dict, step: int) -> str | None:
        """返回错误反馈文本;无错误返回 None。"""
        self.last_kind = None
        if not self.enabled:
            return None
        eh = self.cfg.get("error_handling") or {}
        if not eh:
            return None

        # (i) 重复调用无状态工具(含 expert:相同输入的重复分析同样无意义,
        #     论文 §III-C2 的目标正是抑制这类 meaningless actions)
        if eh.get("duplicate_call", True):
            key = self._args_key(kwargs)
            for rec in self.calls:
                if rec.tool == tool and rec.args_hash == key:
                    self.last_kind = ErrorKind.DUPLICATE_CALL
                    return (
                        f"Error: tool '{tool}' was already invoked with identical arguments "
                        f"at step {rec.step}. Duplicate calls yield no new "
                        "information. Analyze the existing observations or try another "
                        "tool/argument instead."
                    )

        # (ii) expert 工具的 trivial 输入
        if tool in self.expert_names:
            min_chars = eh.get("trivial_input_min_chars", 5)
            text = " ".join(str(v) for v in kwargs.values()).strip()
            if len(text) < min_chars:
                self.last_kind = ErrorKind.TRIVIAL_INPUT
                return (
                    f"Error: input to expert tool '{tool}' is trivial "
                    f"({len(text)} chars). Provide a substantial log excerpt or a "
                    "snapshot key from a previous observation."
                )

        # (iii) 过早 finalize
        if tool == FINALIZE_NAME:
            min_tools = eh.get("early_finalize_min_tools", 1)
            if len(self.info_tool_calls) < min_tools:
                self.last_kind = ErrorKind.EARLY_FINALIZE
                return (
                    "Error: finalize called before thorough investigation. You have not "
                    "collected enough information yet. Gather evidence from "
                    "information-gathering tools before reporting the result."
                )
        return None
