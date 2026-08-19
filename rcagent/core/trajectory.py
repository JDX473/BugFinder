"""轨迹记录(FR-14):完整记录每步 thought/action/observation/错误/prompt。

JSONL 落盘 + 内存,支持回放、Pass/Invalid Rate 统计与 TSC 采样复跑。
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

STATUS_PASSED = "passed"
STATUS_FAILED = "failed"


@dataclass
class StepRecord:
    step: int
    thought: str
    raw_action: str
    action: dict | None = None          # 解析后的 {function, kwargs}
    error: str | None = None            # 错误反馈/解析失败原因
    observation_head: str = ""          # 注入 prompt 的观察 head
    snapshot: str | None = None
    prompt_text: str = ""               # 送入 LLM 的确切 prompt(可配置)
    llm_meta: dict = field(default_factory=dict)
    t: float = 0.0


@dataclass
class Trajectory:
    job_id: str
    records: list[StepRecord] = field(default_factory=list)
    status: str = STATUS_FAILED
    result: dict | None = None          # finalize 的四项结果
    invalid_actions: int = 0            # 无效动作计数(Invalid Rate 分子)
    started: float = field(default_factory=time.time)
    finished: float = 0.0

    @property
    def passed(self) -> bool:
        return self.status == STATUS_PASSED

    def add(self, rec: StepRecord) -> None:
        self.records.append(rec)

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "result": self.result,
            "invalid_actions": self.invalid_actions,
            "steps": len(self.records),
            "started": self.started,
            "finished": self.finished,
            "records": [asdict(r) for r in self.records],
        }

    def save(self, out_dir: str | Path, *, save_prompt: bool = True) -> Path:
        d = Path(out_dir)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"traj_{self.job_id}_{int(self.started)}.json"
        data = self.to_dict()
        if not save_prompt:
            for rec in data["records"]:
                rec.pop("prompt_text", None)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        return path
