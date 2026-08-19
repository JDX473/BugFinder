"""Prompt 管理(论文 §III):三件套组装。

① Framework Rules   — thought-action-observation 循环规则 + JSON 格式 + OBSK 规则
② Task Requirements — RCA 任务 + 领域知识 + 责任判定规则(服务适配时替换该模板)
③ Tools Documentation — 由工具注册表动态生成

模板文件位于 config/prompts/,通过 {placeholder} 注入运行时参数。
"""

from __future__ import annotations

from pathlib import Path

from .tools import FINALIZE_NAME, ToolRegistry

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent.parent / "config" / "prompts"


def _load(name: str) -> str:
    return (_TEMPLATE_DIR / name).read_text(encoding="utf-8")


def build_framework_rules(template_dir: str | Path | None = None) -> str:
    d = Path(template_dir) if template_dir else _TEMPLATE_DIR
    return (d / "framework_rules.txt").read_text(encoding="utf-8")


def build_task_requirements(template_dir: str | Path | None = None, **kwargs) -> str:
    d = Path(template_dir) if template_dir else _TEMPLATE_DIR
    tpl = (d / "task_requirements.txt").read_text(encoding="utf-8")
    return tpl.format(**kwargs) if kwargs else tpl


def build_system_prompt(
    registry: ToolRegistry,
    task_requirements: str,
    framework_rules: str,
) -> str:
    """组装 controller 的 system prompt(三件套)。"""
    return (
        framework_rules
        + "\n\n=== TASK REQUIREMENTS ===\n"
        + task_requirements
        + "\n\n=== TOOLS DOCUMENTATION ===\n"
        + registry.docs()
        + "\n\n"
        + f"The tool '{FINALIZE_NAME}' is the exit point of this task: call it only when "
        "you are ready to report the final result."
    )


def build_user_prompt(job_desc: str) -> str:
    """任务描述(实体 ID、异常类型、检测时刻)。"""
    return job_desc
