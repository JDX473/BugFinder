"""本地文件环境:合成日志 demo 服务(PRD §2.11 方案 A 的雏形)。

为每个 demo job 生成三级日志(runtime/platform/infra)与 advisor 历史
记录,工具遵循论文"语义极简参数 + 模糊去重 + 时间截止"原则。
数据在 data/demo_jobs/{job_id}/ 下,可一键重建(--generate)。
"""

from __future__ import annotations

import json
import random
import re
from datetime import datetime, timedelta
from pathlib import Path

from ..core.tools import ToolError, ToolRegistry, ToolSpec

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "demo_jobs"
CODE_REPO_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "code_repo"

# 场景: (key, anomaly描述, 根因, 责任, 错误行模板, 错误logger)
SCENARIOS = {
    "es_conn_timeout": dict(
        anomaly="job failed with fatal error: TaskManager exited abnormally",
        root_cause="High pressure or anomalies in the Elasticsearch client, "
                   "resulting in connection timeouts",
        solution="If there are multiple timeouts, seek help from the Elasticsearch "
                 "product team or check the cluster health.",
        responsibility="platform",
        err_logger="org.apache.flink.connector.elasticsearch",
        err_lines=[
            "SocketTimeoutException: Connect timed out [Elasticsearch:9200]",
            "org.apache.flink.util.FlinkRuntimeException: The task did not exit "
            "gracefully within 180 + seconds.",
            "Caused by: java.net.SocketTimeoutException: Read timed out",
        ],
        evidence="SocketTimeoutException: Connect timed out [Elasticsearch:9200]",
    ),
    "oss_lifecycle": dict(
        anomaly="job failed to start: checkpoint setup failed",
        root_cause="Bucket lacks lifecycle rules for version control",
        solution="Configure lifecycle rules on OSS to periodically clean up and "
                 "delete unnecessary tagging and historical versions.",
        responsibility="user",
        err_logger="com.alibaba.oss",
        err_lines=[
            "OSSException: RequestTimeTooSkewed",
            "The difference between the request time and the current time is too large.",
            "at ...oshadoop.shaded.com.alibaba.oss.OSSException",
        ],
        evidence="OSSException: RequestTimeTooSkewed; The difference between the "
                 "request time and the current time is too large.",
    ),
    "checkpoint_timeout": dict(
        anomaly="job failed: checkpoint exceeded timeout",
        root_cause="Checkpoint timeout caused by insufficient checkpointing "
                   "configuration and slow state backend",
        solution="Increase checkpoint interval or configure state backend properly.",
        responsibility="user",
        err_logger="org.apache.flink.runtime.checkpoint",
        err_lines=[
            "Checkpoint 42 expired before completing",
            "Failed to trigger checkpoint for job: timeout of 60000 ms exceeded",
            "Exception: org.apache.flink.runtime.state.StateBackendException",
        ],
        evidence="Checkpoint 42 expired before completing; timeout of 60000 ms exceeded.",
    ),
    "task_evicted": dict(
        anomaly="job failed: task manager lost due to resource eviction",
        root_cause="TaskManager evicted by platform for higher-priority jobs",
        solution="Contact platform team; the eviction is caused by oversold resources.",
        responsibility="platform",
        err_logger="org.apache.flink.runtime.taskexecutor",
        err_lines=[
            "TaskManagerRunner fatal error: resource released by scheduler",
            "Eviction notice: node oversold, releasing taskmanager",
            "jobmanager lost connection to taskmanager: heartbeat timeout",
        ],
        evidence="Eviction notice: node oversold, releasing taskmanager.",
    ),
}

_LEVELS = ["INFO", "INFO", "INFO", "WARN", "ERROR"]
_BACKGROUND_LOGGERS = [
    "org.apache.flink.runtime.jobmanager.JobManager",
    "org.apache.flink.streaming.runtime.tasks.StreamTask",
    "org.apache.flink.runtime.operators.sort.MergeIterator",
    "org.apache.flink.streaming.api.operators.windowing",
    "org.apache.flink.metrics.MetricRegistry",
]
_BACKGROUND_MSGS = [
    "Received heartbeat from taskmanager",
    "Triggering checkpoint 4{seq}",
    "Completed checkpoint 4{seq} in {t} ms",
    "Rescheduling failed subtask {seq}",
    "Restarting job with strategy: fixed delay",
    "Sending metrics to reporter",
]


def synthesize_logs(scenario: str, n_lines: int = 4000, seed: int = 0) -> str:
    """生成含错误模式的合成日志:正常行 + 集中于末尾的错误块。"""
    rng = random.Random(seed)
    s = SCENARIOS[scenario]
    base = datetime(2024, 1, 1, 9, 0, 0)
    lines: list[str] = []
    err_start = n_lines - max(200, n_lines // 10)

    for i in range(n_lines):
        ts = base + timedelta(milliseconds=rng.randint(100, 5000) * i)
        if i >= err_start:
            level = rng.choices(["ERROR", "WARN", "INFO"], weights=[6, 3, 1])[0]
            logger_name = s["err_logger"]
            if level == "ERROR":
                msg = s["err_lines"][i % len(s["err_lines"])]
                # 错误行带堆栈格式的延续行
                if i % 3 == 0:
                    msg += f"\n\tat com.example.job{scenario}.source.SourceFunction.run(SourceFunction.java:{rng.randint(1,999)})"
            else:
                msg = f"error rate rising: {rng.randint(30, 99)}% failures in window"
        else:
            level = rng.choices(_LEVELS, weights=[6, 2, 1, 0.8, 0.2])[0]
            logger_name = rng.choice(_BACKGROUND_LOGGERS)
            msg = _BACKGROUND_MSGS[rng.randrange(len(_BACKGROUND_MSGS))].format(
                seq=rng.randint(10, 99), t=rng.randint(50, 900))
        lines.append(f"{ts.strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]} {level:<5} "
                     f"{logger_name} - {msg}")
    return "\n".join(lines)


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class LocalEnvironment:
    """基于 data/demo_jobs/ 目录文件的 demo 环境。"""

    def __init__(self, data_dir: str | Path | None = None, max_log_chars: int = 120_000,
                 llm=None, embedder=None, kb=None, code_repo: str | Path | None = None):
        self.data_dir = Path(data_dir) if data_dir else DATA_DIR
        self.max_log_chars = max_log_chars
        self._code_repo = Path(code_repo) if code_repo else CODE_REPO_DIR
        # M3: 日志专家(Algorithm 1);依赖缺失时退回 stub
        self._log_expert = None
        if llm is not None and embedder is not None and kb is not None:
            from ..experts.log_agent import LogExpertAgent

            self._log_expert = LogExpertAgent(llm, embedder, kb)
        # 代码专家(图4): 递归分析 advisor 服务源码
        self._code_expert = None
        if llm is not None and self._code_repo.exists():
            from ..experts.code_agent import CodeExpertAgent

            self._code_expert = CodeExpertAgent(llm, self._code_repo)

    # -- 数据访问(时间截止约束在读取层执行: 仅取 detect_time 之前的行) ---

    def _read_log(self, job_id: str, level: str, detect_time: str) -> str:
        path = self.data_dir / job_id / f"{level}.log"
        if not path.exists():
            raise ToolError(f"no {level} log for job {job_id}")
        lines = path.read_text(encoding="utf-8").splitlines()
        kept = [ln for ln in lines if ln <= detect_time]
        return "\n".join(kept) or "(no log entries before detection time)"

    def _advisor_record(self, job_id: str, detect_time: str) -> str:
        path = self.data_dir / job_id / "advisor.txt"
        if not path.exists():
            return "(no advisor history)"
        text = path.read_text(encoding="utf-8")
        return text[: self.max_log_chars]

    # -- 工具注册 --------------------------------------------------------

    def expert_tool_names(self) -> list[str]:
        names = []
        if self._registry and "log_agent" in self._registry.names():
            names.append("log_agent")
        if self._registry and "code_agent" in self._registry.names():
            names.append("code_agent")
        return names

    def register_tools(self, registry: ToolRegistry, *, include_experts: bool = True) -> None:
        self._registry = registry
        registry.register(ToolSpec(
            name="runtime_log",
            description="Runtime logs of the taskmanager and jobmanager of the job "
                        "(task execution, state, checkpoints, connectors).",
            params={"job_id": "id of the anomalous job"},
            handler=lambda kw, env: self._read_log(kw["job_id"], "runtime", env.detect_time),
            examples='{"job_id": "demo1"}',
        ))
        registry.register(ToolSpec(
            name="platform_log",
            description="Platform-layer logs: scheduling, resource management, "
                        "eviction and administrative service records.",
            params={"job_id": "id of the anomalous job"},
            handler=lambda kw, env: self._read_log(kw["job_id"], "platform", env.detect_time),
        ))
        registry.register(ToolSpec(
            name="infrastructure_log",
            description="Infrastructure-layer logs: network, storage (OSS), "
                        "Elasticsearch and OS-level records.",
            params={"job_id": "id of the anomalous job"},
            handler=lambda kw, env: self._read_log(kw["job_id"], "infra", env.detect_time),
        ))
        registry.register(ToolSpec(
            name="advisor_db",
            description="Historical analysis records of the advisor service for "
                        "this job (previous diagnoses and decisions).",
            params={"job_id": "id of the anomalous job"},
            handler=lambda kw, env: self._advisor_record(kw["job_id"], env.detect_time),
        ))
        # 日志专家(Algorithm 1): 分析长日志,返回解释与证据
        if include_experts:
            registry.register(ToolSpec(
                name="log_agent",
                description="Analyze a long log with another LLM agent. Pass the "
                            "snapshot key of the log observation (or a log excerpt). "
                            "Returns interpretation and verbatim evidence.",
                params={"snapshot": "snapshot key of the log observation"},
                handler=lambda kw, env: self._run_log_agent(kw.get("snapshot", "")),
                is_expert=True,
                examples='{"snapshot": "2975241420"}',
            ))
            # 代码专家(图4): 递归分析 advisor 服务源码,扩展领域知识
            if self._code_expert is not None:
                registry.register(ToolSpec(
                    name="code_agent",
                    description="Analyze the advisor service source code with another "
                                "LLM agent. Pass a class name to start; the agent "
                                "recursively follows code dependencies. Returns an "
                                "explanation of the diagnostic mechanism.",
                    params={"class_name": "name of the class to start analysis from"},
                    handler=lambda kw, env: self._code_expert.run(kw.get("class_name", "")),
                    is_expert=True,
                    examples='{"class_name": "JobConnectorSinkConnectionFailService"}',
                ))

    def _run_log_agent(self, snapshot: str) -> str:
        """agent 层已把 snapshot key 解析为完整日志(见 core/agent.py resolve)。"""
        if self._log_expert is not None:
            return self._log_expert.run(snapshot)
        return self._stub_log_analysis(snapshot)

    def _stub_log_analysis(self, snapshot: str) -> str:
        """占位分析(M3 实现 Algorithm 1 后替换)。

        agent 层已把 snapshot key 解析为完整日志(见 core/agent.py 的
        store.resolve),直接提取 ERROR 行作为证据与初步解释。
        """
        content = snapshot or ""
        if not content.strip():
            return ("{interpretation: insufficient log content provided, "
                    "evidence: []}")
        err_lines = [ln for ln in content.splitlines() if "ERROR" in ln or "Exception" in ln]
        evidence = err_lines[:5]
        interp = ("The log contains fatal errors around task execution. "
                  "Detailed analysis pending (stub).")
        return (f"interpretation: {interp}\nevidence: " + "\n".join(evidence))


# ---- 数据生成工具 ----

def generate_demo_data(data_dir: str | Path | None = None, n_lines: int = 4000,
                       seed: int = 0) -> list[str]:
    """为全部场景生成 demo job 数据(runtime/platform/infra 日志 + 描述)。"""
    d = Path(data_dir) if data_dir else DATA_DIR
    jobs: list[str] = []
    for key, s in SCENARIOS.items():
        job_dir = d / f"demo_{key}"
        job_dir.mkdir(parents=True, exist_ok=True)
        runtime = synthesize_logs(key, n_lines, seed)
        (job_dir / "runtime.log").write_text(runtime, encoding="utf-8")
        # platform/infra: 少量相关行 + 噪声
        (job_dir / "platform.log").write_text(
            synthesize_logs(key, max(200, n_lines // 8), seed + 1), encoding="utf-8")
        (job_dir / "infra.log").write_text(
            synthesize_logs(key, max(300, n_lines // 6), seed + 2), encoding="utf-8")
        detect = _now_str()
        meta = {
            "job_id": f"demo_{key}",
            "anomaly": s["anomaly"],
            "detect_time": detect,
            "ground_truth": {
                "root_cause": s["root_cause"],
                "solution": s["solution"],
                "evidence": s.get("evidence", ""),
                "responsibility": s["responsibility"],
            },
        }
        (job_dir / "job.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                                          encoding="utf-8")
        (job_dir / "advisor.txt").write_text(
            f"Advisor history for {key}: no previous analysis recorded.\n",
            encoding="utf-8")
        jobs.append(meta["job_id"])
    return jobs


def load_job(job_id: str, data_dir: str | Path | None = None) -> dict:
    d = Path(data_dir) if data_dir else DATA_DIR
    path = d / job_id / "job.json"
    if not path.exists():
        raise FileNotFoundError(
            f"job data not found: {path}. Run 'python -m rcagent.env.local --generate' first.")
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="demo 数据生成")
    ap.add_argument("--generate", action="store_true")
    ap.add_argument("--n-lines", type=int, default=4000)
    args = ap.parse_args()
    if args.generate:
        jobs = generate_demo_data(n_lines=args.n_lines)
        print(f"generated {len(jobs)} demo jobs: {jobs}")
