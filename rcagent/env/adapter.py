"""环境适配层(FR-13):把论文的 SLS 日志/数据库/代码仓库抽象为可插拔接口。

服务专属件(PRD §2.11 耦合点 1):每个目标服务实现一个 Environment,
注册自己的信息收集工具;框架本体与具体实现解耦。
"""

from __future__ import annotations

from typing import Protocol

from ..core.tools import ToolRegistry


class Environment(Protocol):
    """目标服务环境:注册工具、声明专家工具名、提供作业描述。"""

    def register_tools(self, registry: ToolRegistry, *, include_experts: bool = True) -> None: ...

    def expert_tool_names(self) -> list[str]:
        """返回专家工具(LLM 分析工具)名称列表,供 trivial 输入检查。"""
        ...
