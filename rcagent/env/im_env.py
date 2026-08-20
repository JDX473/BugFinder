"""IM 环境(QuantumLink IM 真实服务):工具实现与数据源。

服务专属件(PRD §2.11 耦合点 1):把 RCAgent 接入用户的真实 IM 项目。
阶段 1 数据源为真实日志文件(chat/connect);时间截止约束按日志行首
的 ISO 时间戳过滤(chat 日志);connect 日志暂无时间戳,整段尾部返回。

工具(语义极简参数):
  chat_log(query, before)  — 按关键词 + 检测时刻过滤 chat 日志
  connect_log(before)      — connect 长连接层日志尾部
  error_summary(before)    — chat 日志 ERROR 类型分布 + 时间范围
  outbox_error(before)     — outbox scan error 详情(含堆栈)
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..core.tools import ToolError, ToolRegistry, ToolSpec

IM_JOBS_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "im_jobs"

# chat 日志行首时间戳: 2026-08-19T00:06:03.599+08:00(ISO,可字典序比较)
_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")


class IMEnvironment:
    def __init__(self, job_dir: str | Path | None = None, llm=None, embedder=None,
                 kb=None, code_repo: str | Path | None = None):
        self.job_dir = Path(job_dir) if job_dir else IM_JOBS_DIR
        self._code_repo = Path(code_repo) if code_repo else Path(
            r"E:\QIUZHAO\IM")
        # 专家工具(与 demo 环境一致): 日志专家 + 代码专家(仓库=IM 源码)
        self._log_expert = None
        if llm is not None and embedder is not None and kb is not None:
            from ..experts.log_agent import LogExpertAgent

            self._log_expert = LogExpertAgent(llm, embedder, kb)
        self._code_expert = None
        if llm is not None and self._code_repo.exists():
            from ..experts.code_agent import CodeExpertAgent

            self._code_expert = CodeExpertAgent(llm, self._code_repo)

    # -- 数据访问 ------------------------------------------------------------

    def _chat_log_path(self, job_id: str) -> Path:
        meta = json.loads((self.job_dir / job_id / "job.json").read_text(encoding="utf-8"))
        return Path(meta["data_sources"]["chat_log"])

    def _connect_log_path(self, job_id: str) -> Path:
        meta = json.loads((self.job_dir / job_id / "job.json").read_text(encoding="utf-8"))
        return Path(meta["data_sources"]["connect_log"])

    def _tail_filtered(self, path: Path, before: str, query: str | None,
                       max_lines: int = 4000) -> str:
        """从文件尾部读,保留时间戳 <= before 且匹配 query 的行。

        大日志(100MB+)不能全读:从尾部反向读块,直到取够行数或到达文件头。
        堆栈行(无时间戳)跟随其所属的 ERROR 行保留(否则异常详情丢失,
        如 RedisSystemException 只出现在堆栈里,搜索会落空)。
        """
        if not path.exists():
            raise ToolError(f"log file not found: {path}")
        size = path.stat().st_size
        chunk = 64 * 1024
        pos = size
        buf = ""
        lines: list[str] = []
        pending_stack: list[str] = []  # 反向暂存的堆栈行(属于下一个 ERROR 行)
        while pos > 0 and len(lines) < max_lines * 4:
            read = min(chunk, pos)
            pos -= read
            with open(path, "rb") as f:
                f.seek(pos)
                buf = f.read(read).decode("utf-8", errors="replace") + buf
            parts = buf.split("\n")
            buf = parts[0]  # 可能被截断的半行
            for line in reversed(parts[1:]):
                if not line.strip():
                    continue
                ts = _TS_RE.match(line)
                if ts:
                    stack = pending_stack
                    pending_stack = []
                    if ts.group(1) > before:
                        continue  # 检测时刻之后(时间截止约束)
                    # query 同时匹配 ERROR 行与其堆栈(异常类常只出现在堆栈里)
                    matched = (query is None or query in line
                               or any(query in s for s in stack))
                    if not matched:
                        continue
                    lines.append(line)
                    lines.extend(reversed(stack))  # 恢复堆栈顺序
                else:
                    pending_stack.append(line)
                if len(lines) >= max_lines:
                    break
            if len(lines) >= max_lines:
                break
        lines.reverse()
        return "\n".join(lines)

    def _error_summary(self, path: Path, before: str, max_lines: int = 500) -> str:
        """ERROR 类型分布摘要(按 logger 名聚合 + 时间范围)。

        只统计检测时刻前最近 max_lines 条 ERROR(默认 500,约最后 40 分钟)
        ——避免 36 小时的历史故障噪音把模型带偏到无关故障。
        """
        text = self._tail_filtered(path, before, "ERROR", max_lines=max_lines)
        if not text:
            return "(no ERROR lines before detection time)"
        counts: dict[str, int] = {}
        first_ts = last_ts = None
        for line in text.splitlines():
            ts = _TS_RE.match(line)
            if ts:
                last_ts = ts.group(1)
                if first_ts is None:
                    first_ts = ts.group(1)
            m = re.search(r"ERROR \d+ --- \[[^\]]*\] \[[^\]]*\] ([a-zA-Z0-9.]+) *:", line)
            if m:
                counts[m.group(1)] = counts.get(m.group(1), 0) + 1
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:10]
        summary = f"ERROR window: {first_ts} ~ {last_ts}, total {sum(counts.values())} lines (last {max_lines} ERRORs)\n"
        summary += "\n".join(f"  {name}: {n}" for name, n in top)
        return summary

    # -- 工具注册 ------------------------------------------------------------

    def expert_tool_names(self) -> list[str]:
        return ["log_agent", "code_agent"]

    def register_tools(self, registry: ToolRegistry, *, include_experts: bool = True) -> None:
        def chat_log_handler(kw, env):
            return self._tail_filtered(self._chat_log_path(env.job_id),
                                       env.detect_time, kw.get("query"), 4000)

        def connect_log_handler(kw, env):
            return self._tail_filtered(self._connect_log_path(env.job_id),
                                       env.detect_time, kw.get("query"), 4000)

        registry.register(ToolSpec(
            name="chat_log",
            description="Query the im-chat service log (Spring Boot, 8081). Pass a "
                        "keyword (e.g. 'ERROR', 'outbox', class name) to filter lines. "
                        "Only lines before the detection time are returned.",
            params={"query": "keyword to filter log lines (optional, empty = all)"},
            handler=chat_log_handler,
            examples='{"query": "outbox scan error"}',
        ))
        registry.register(ToolSpec(
            name="connect_log",
            description="Query the im-connect long-connection layer log (Netty, 19001). "
                        "Pass a keyword to filter lines.",
            params={"query": "keyword to filter log lines (optional)"},
            handler=connect_log_handler,
            examples='{"query": "ERROR"}',
        ))
        registry.register(ToolSpec(
            name="error_summary",
            description="Summary of ERROR lines in the im-chat log: time window and "
                        "distribution by logger class.",
            params={},
            handler=lambda kw, env: self._error_summary(self._chat_log_path(env.job_id),
                                                        env.detect_time),
        ))
        registry.register(ToolSpec(
            name="outbox_error",
            description="Details of 'outbox scan error' occurrences (with exception "
                        "stack traces) in the im-chat log.",
            params={},
            handler=lambda kw, env: self._tail_filtered(
                self._chat_log_path(env.job_id), env.detect_time, "outbox scan error", 600),
        ))
        # 专家工具(与 demo 环境对齐;RCAgent.build 的 include_experts 控制)
        if include_experts:
            if self._log_expert is not None:
                registry.register(ToolSpec(
                    name="log_agent",
                    description="Analyze a long log excerpt with another LLM agent. "
                                "Pass a log excerpt (or snapshot key). Returns "
                                "interpretation and verbatim evidence.",
                    params={"snapshot": "log excerpt text or snapshot key"},
                    handler=lambda kw, env: self._log_expert.run(kw.get("snapshot", "")),
                    is_expert=True,
                ))
            if self._code_expert is not None:
                registry.register(ToolSpec(
                    name="code_agent",
                    description="Analyze the QuantumLink IM source code with another "
                                "LLM agent. Pass a class name to start; the agent "
                                "recursively follows code dependencies.",
                    params={"class_name": "name of the class to start analysis from"},
                    handler=lambda kw, env: self._code_expert.run(kw.get("class_name", "")),
                    is_expert=True,
                    examples='{"class_name": "OutboxService"}',
                ))

    # -- 工具默认实现依赖注入(供 RCAgent.build 使用) ------------------------

    def __repr__(self) -> str:
        return f"IMEnvironment(job_dir={self.job_dir})"
