"""工具注册框架(论文 §III-B):语义极简参数 + 返回去重 + finalize 出口。

工具是服务专属件(PRD §2.11 耦合点 1):框架只定义注册与契约,
具体 handler 由环境适配层提供。所有工具返回经 OBSK 包装
(head + snapshot),长内容不直接进 controller prompt。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol

from rapidfuzz import fuzz

from .obs import SnapshotStore, build_observation_head

FINALIZE_NAME = "finalize"


class ToolError(Exception):
    """工具执行期错误(环境异常),与"错误处理"反馈(ErrorDetector)区分。"""


class EnvContext(Protocol):
    """环境上下文:工具 handler 可访问的运行时信息。"""

    job_id: str
    detect_time: str


@dataclass
class ToolResult:
    head: str                    # 展示给 controller 的头部文本
    snapshot: str | None = None  # 快照键(长内容时)
    full: str = ""               # 完整内容(存快照库)
    truncated: bool = False
    meta: dict = field(default_factory=dict)


@dataclass
class ToolSpec:
    name: str
    description: str                 # 工具文档描述(进入 prompt)
    params: dict[str, str]           # 参数名 -> 参数说明
    handler: Callable[[dict, EnvContext], str]  # kwargs -> 原始返回文本
    stateless: bool = True           # 无状态工具(错误处理(i)重复调用检查)
    is_expert: bool = False          # 是否为 LLM 专家工具(错误处理(ii)trivial 检查)
    examples: str = ""               # 可选的 1~2 行调用示例

    def doc(self) -> str:
        lines = [f"- {self.name}({', '.join(f'{k}={v}' for k, v in self.params.items())})"]
        lines.append(f"  {self.description}")
        if self.examples:
            lines.append(f"  Example: {self.examples}")
        return "\n".join(lines)


def dedup_lines(text: str, ratio: float = 0.95) -> str:
    """模糊匹配去重(论文 §III-B1):相似度高于 ratio 的行只保留首条。

    防止重复数据膨胀上下文并诱发 LLM 重复退化。按行比较(O(n) 单趟,
    相邻窗口比较,对日志序重复高效;全对比较交给工具实现自行权衡)。
    """
    lines = text.splitlines(keepends=True)
    if len(lines) <= 1:
        return text
    kept: list[str] = [lines[0]]
    for line in lines[1:]:
        prev = kept[-1]
        a, b = line.strip(), prev.strip()
        if not a or not b:
            kept.append(line)
            continue
        if fuzz.ratio(a, b) / 100.0 >= ratio:
            continue
        kept.append(line)
    return "".join(kept)


class ToolRegistry:
    def __init__(self, store: SnapshotStore, obs_head_chars: int, dedup_ratio: float,
                 max_obs_chars: int):
        self.store = store
        self._specs: dict[str, ToolSpec] = {}
        self._obs_head_chars = obs_head_chars
        self._dedup_ratio = dedup_ratio
        self._max_obs_chars = max_obs_chars

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"duplicate tool name: {spec.name}")
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def names(self) -> list[str]:
        return list(self._specs)

    def expert_names(self) -> list[str]:
        return [n for n, s in self._specs.items() if s.is_expert]

    def docs(self) -> str:
        return "\n".join(self._specs[n].doc() for n in self._specs)

    def call(self, name: str, kwargs: dict, env: EnvContext,
             *, obs_mode: str = "full") -> ToolResult:
        """执行工具:去重 → 截断上限 → OBSK 包装 → 快照入库。

        obs_mode 用于消融(论文 TABLE I 注释):
          full       — head + snapshot 键(默认)
          no_obsk    — 直接截断,不生成快照(w/o OBSK)
          no_obs_head— 只给快照键,不给 head(w/o Obs Head)
        """
        spec = self._specs[name]
        raw = spec.handler(kwargs, env)
        raw = dedup_lines(raw, self._dedup_ratio)
        # 超长时保留头尾各半(尾部常含致命错误块,对齐论文图1的裁剪精神)
        if len(raw) > self._max_obs_chars:
            half = self._max_obs_chars // 2
            raw = raw[:half] + "\n...[middle truncated]...\n" + raw[-half:]

        if obs_mode == "no_obsk":
            head = raw[: self._obs_head_chars]
            if len(raw) > self._obs_head_chars:
                head += "\n[truncated]"
            return ToolResult(head=head, full=raw, truncated=True,
                              meta={"tool": name, "kwargs": kwargs, "obs_mode": obs_mode})

        if obs_mode == "no_obs_head":
            key = self.store.put(raw)
            return ToolResult(head=f"[snapshot: {key}]", snapshot=key, full=raw,
                              truncated=True,
                              meta={"tool": name, "kwargs": kwargs, "obs_mode": obs_mode})

        head, key, truncated = build_observation_head(raw, self._obs_head_chars)
        if key is not None:
            self.store.put(raw)
        return ToolResult(head=head, snapshot=key, full=raw, truncated=truncated,
                          meta={"tool": name, "kwargs": kwargs})


# ---- 内建 finalize 工具(论文 §III: 出口点,可解析格式报告四项结果) ----

RESPONSIBILITIES = ("platform", "user")


def make_finalize_spec(required_fields: list[str]) -> ToolSpec:
    params = {f: f"string, the final {f} of the RCA result" for f in required_fields}
    params["responsibility"] = "string, one of platform|user"

    def handler(kwargs: dict, env: EnvContext) -> str:
        return "finalized"

    return ToolSpec(
        name=FINALIZE_NAME,
        description=(
            "Report the final root cause analysis result and exit. Call this ONLY when "
            "you have gathered sufficient evidence. All fields are required."
        ),
        params=params,
        handler=handler,
        stateless=False,
    )
