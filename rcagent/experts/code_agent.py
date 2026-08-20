"""Code Expert Agent(论文 §III-B2, 图4):递归代码分析工具。

流程: 输入类名 → 检索代码文件 → LLM 阅读分析并建议相关类 →
建议加入任务队列(去重)→ 循环直到无新推荐或均为外部依赖 →
LLM 总结全部代码文件,结果作为 observation 返回 controller。

该工具扩展 controller 的领域知识(如诊断工具的工作机制);
输出经 JsonRegen 修复层。
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import Config
from ..core.jsonregen import json_regen
from ..llm.client import LLMClient

logger = logging.getLogger(__name__)

_ANALYZE_TEMPLATE = """\
Analyze the following {lang} source file from the {repo_name} codebase and \
summarize what it does.

```{lang_short}
{code}
```

Think step by step, then output a JSON object:
{{"summary": "<what this file does, how it contributes to {repo_name} diagnosis>",
 "suggested_classes": ["<names of related symbols/files worth analyzing next>"]}}
Suggest symbols referenced by this file (imports, field types, base classes, \
module paths) that exist in this codebase. Empty array if none."""

_SUMMARIZE_TEMPLATE = """\
Below are analyses of the source files of a {repo_name} codebase, discovered by \
following the dependencies from one entry point. Summarize how the diagnostic \
mechanism works end to end.

{analyses}

Output a JSON object:
{{"summary": "<concise end-to-end explanation of the diagnostic mechanism>"}}"""

# 支持的源码扩展名(通用仓库: 按符号名索引,与具体语言解耦)
_SOURCE_EXTENSIONS = (".java", ".py", ".go", ".cpp", ".h", ".c", ".ts", ".js",
                      ".scala", ".kt", ".rs")


class CodeExpertAgent:
    def __init__(self, llm: LLMClient, repo_dir: str | Path, cfg: Config | None = None):
        self.llm = llm
        self.repo_dir = Path(repo_dir)
        self.cfg = cfg.get("code_agent") if cfg is not None else None
        self._missing: set[str] = set()  # 已确认不存在的类名(防模型重复猜测)

    def _opt(self, key: str, default):
        return self.cfg.get(key, default) if self.cfg else default

    # -- 主入口 ------------------------------------------------------------

    def run(self, class_name: str) -> str:
        """递归分析;返回给 controller 的 observation 文本。"""
        class_name = class_name.strip()
        if not class_name:
            return ("{interpretation: no class name provided, evidence: []}")
        if self._resolve_file(class_name) is None:
            # 明确否定 + 禁止猜测: 弱反馈会被模型解读为"再猜别的名字"
            if class_name in self._missing:
                return (
                    f"interpretation: class '{class_name}' was already checked and "
                    "does not exist in this codebase. Stop guessing class names; "
                    "analyze the evidence you already collected or call finalize. "
                    "evidence: []"
                )
            self._missing.add(class_name)
            return (
                f"interpretation: class '{class_name}' does not exist in the "
                f"{self._opt('repo_name', 'service')} codebase. Class names must be "
                "referenced from previously observed code files or advisor records; "
                "do NOT guess class names. evidence: []"
            )

        queue = [class_name]
        visited: set[Path] = set()
        analyses: list[str] = []
        max_files = self._opt("max_files", 20)

        while queue and len(visited) < max_files:
            cls = queue.pop(0)
            path = self._resolve_file(cls)
            if path is None:
                continue  # 外部依赖或仓库内不存在
            if path in visited:
                continue
            visited.add(path)

            code = path.read_text(encoding="utf-8")
            result = self._analyze_file(code, cls)
            if result is None:
                continue
            analyses.append(result["summary"])
            for suggestion in result.get("suggested_classes", []):
                if not isinstance(suggestion, str) or not suggestion.strip():
                    continue
                if self._resolve_file(suggestion) is not None:  # 仅本地文件入队
                    queue.append(suggestion.strip())

        if not analyses:
            return ("{interpretation: no code files analyzed, evidence: []}")

        return self._summarize(analyses, visited)

    # -- 文件检索(与具体语言解耦) ------------------------------------------

    def _index(self) -> dict[str, Path]:
        """符号名 → 文件路径。每次调用重建: 代码仓库频繁变动时索引不过期。

        键覆盖: 文件名(去扩展名)、相对路径、以及"最后一段"的任意后缀
        (如 com.alibaba.FlinkLifecycleMapper → FlinkLifecycleMapper)。
        """
        idx: dict[str, Path] = {}
        for p in self.repo_dir.rglob("*"):
            if not p.is_file() or p.suffix not in _SOURCE_EXTENSIONS:
                continue
            idx[p.stem] = p
            rel = p.relative_to(self.repo_dir).with_suffix("")
            idx[str(rel).replace("\\", ".").replace("/", ".")] = p
        return idx

    def _resolve_file(self, symbol: str) -> Path | None:
        """按符号名解析仓库内文件: 简单名、完全限定名、模块路径均可。

        仓库内无对应文件视为外部依赖(跳过分析)。
        """
        idx = self._index()
        if symbol in idx:
            return idx[symbol]
        simple = symbol.split(".")[-1]
        return idx.get(simple)

    # -- LLM 交互 ------------------------------------------------------------

    def _analyze_file(self, code: str, symbol: str) -> dict | None:
        prompt = _ANALYZE_TEMPLATE.format(
            lang=self._opt("language", "source"),
            lang_short=self._opt("language_short", "text"),
            repo_name=self._opt("repo_name", "service"),
            code=code)
        out = json_regen(self.llm, prompt,
                         retries=self._opt("jsonregen_retries", 2),
                         temperature=self._opt("temperature", 0.0))
        if out is None or not isinstance(out.get("summary"), str):
            logger.debug("code analysis unparsable for %s, skipped", symbol)
            return None
        return out

    def _summarize(self, summaries: list[str], visited: set[Path]) -> str:
        files = sorted(p.name for p in visited)
        analyses = "\n\n".join(
            f"File: {name}\n{summary}" for name, summary in zip(files, summaries))
        prompt = _SUMMARIZE_TEMPLATE.format(
            repo_name=self._opt("repo_name", "service"), analyses=analyses)
        out = json_regen(self.llm, prompt, retries=self._opt("jsonregen_retries", 2))
        if out is not None and isinstance(out.get("summary"), str):
            return f"interpretation: {out['summary'].strip()}\nevidence: analyzed files: {', '.join(files)}"
        return (f"interpretation: {summaries[0]}\nevidence: analyzed files: "
                f"{', '.join(files)}")
