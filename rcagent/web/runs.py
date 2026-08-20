"""RunManager: agent 运行的线程管理与事件日志(Web 运行时核心)。

- 每 run 一条 daemon 线程,并发上限 1(本地单用户);
- 内存事件日志 list[envelope] + Lock + seq,SSE 端点按索引轮询
  (零跨线程 async 调用,天然支持多订阅者与 Last-Event-ID 重放);
- 取消: threading.Event,agent 步边界生效;
- 结束后: 轨迹落盘 runs/、事件落盘 .events.json、快照 dump 到
  runs/snapshots/{run_id}/;历史 run 回放优先读 .events.json,
  旧轨迹(无事件)由 derive_events_from_trajectory 合成。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config
from ..core.events import AgentEvents
from .events import derive_events_from_trajectory, format_sse

logger = logging.getLogger(__name__)

EVENT_LOG_CAP = 2000


class RunBusy(Exception):
    pass


@dataclass
class RunState:
    run_id: str
    job_id: str
    mock: bool
    variant: str
    decode: str
    anomaly: str | None = None
    env: str = "demo"          # demo(合成) | im(QuantumLink IM)
    detect_time: str | None = None  # 缺省 = 当前时刻(实时排查模式)
    status: str = "running"
    events: list[dict] = field(default_factory=list)
    seq: int = 0
    cancel: threading.Event = field(default_factory=threading.Event)
    started: float = field(default_factory=time.time)
    finished: float = 0.0
    traj: object = None          # Trajectory(结束后可用)
    store: object = None         # SnapshotStore(live 期间可取快照)
    error: str | None = None
    traj_path: str | None = None
    events_path: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class RunManager:
    def __init__(self, cfg: Config, runs_dir: str | Path = "runs"):
        self.cfg = cfg
        self.runs_dir = Path(runs_dir)
        self.snapshots_dir = self.runs_dir / "snapshots"
        self._active: RunState | None = None
        self._active_lock = threading.Lock()

    # -- 对外 API -----------------------------------------------------------

    def start(self, job_id: str | None = None, *, variant: str = "full",
              decode: str = "greedy", mock: bool = True,
              anomaly: str | None = None, env: str = "demo",
              detect_time: str | None = None) -> str:
        """启动一个 run;已有活跃 run 时抛 RunBusy。

        job_id 缺省 = 实时排查模式(im 环境): 数据源用环境默认日志,
        detect_time 取当前时刻,Agent 自动聚焦最近故障。
        """
        with self._active_lock:
            if self._active is not None:
                raise RunBusy(f"run {self._active.run_id} is still active")
            state = RunState(
                run_id=f"live_{job_id or 'realtime'}_{int(time.time() * 1000)}",
                job_id=job_id or "", mock=mock, variant=variant, decode=decode,
                anomaly=anomaly, env=env, detect_time=detect_time,
            )
            self._active = state
        t = threading.Thread(target=self._worker, args=(state,), daemon=True)
        t.start()
        return state.run_id

    def cancel(self, run_id: str) -> bool:
        state = self._active
        if state is None or state.run_id != run_id:
            return False
        state.cancel.set()
        return True

    def active_run_id(self) -> str | None:
        return self._active.run_id if self._active else None

    def get_run(self, run_id: str) -> dict | None:
        """完整快照 {meta, trajectory, events}: live 从内存,archive 从落盘。"""
        if self._active and self._active.run_id == run_id:
            s = self._active
            return {
                "meta": self._meta(s),
                "trajectory": s.traj.to_dict() if s.traj else None,
                "events": list(s.events),
            }
        # archive: 优先 .events.json,否则从轨迹合成
        base = self._find_archive_base(run_id)
        if base is None:
            return None
        traj = json.loads(base.read_text(encoding="utf-8"))
        events_path = base.with_suffix(".events.json")
        events = (json.loads(events_path.read_text(encoding="utf-8"))
                  if events_path.exists() else derive_events_from_trajectory(traj))
        # 真实终态优先取 run_finished 事件(cancelled 等内存态不落轨迹 JSON)
        status = traj.get("status", "failed")
        for e in reversed(events):
            if e["type"] == "run_finished":
                status = e["payload"].get("status", status)
                break
        return {
            "meta": {"run_id": run_id, "job_id": traj.get("job_id"),
                     "status": status,
                     "started": traj.get("started"), "finished": traj.get("finished"),
                     "source": "archive"},
            "trajectory": traj,
            "events": events,
        }

    def list_runs(self, limit: int = 50) -> list[dict]:
        """live 优先,随后 archive(runs/traj_*.json 按时间倒序)。"""
        out: list[dict] = []
        if self._active is not None:
            out.append(self._meta(self._active))
        for p in sorted(self.runs_dir.glob("traj_*.json"),
                        key=lambda x: x.stat().st_mtime, reverse=True):
            if len(out) >= limit:
                break
            try:
                traj = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            out.append({
                "run_id": p.stem, "job_id": traj.get("job_id"),
                "status": traj.get("status"), "steps": len(traj.get("records", [])),
                "started": traj.get("started"), "finished": traj.get("finished"),
                "mode": "archive", "source": "archive",
            })
        return out

    def get_snapshot(self, run_id: str, key: str) -> str | None:
        """快照全文: live 从内存 store;archive 从 runs/snapshots/{run_id}/{key}.txt。"""
        if self._active and self._active.run_id == run_id:
            store = self._active.store
            return store.get(key) if store else None
        p = self.snapshots_dir / run_id / f"{key}.txt"
        return p.read_text(encoding="utf-8") if p.exists() else None

    def events_since(self, run_id: str, after_seq: int) -> tuple[list[dict], bool]:
        """SSE 轮询: 返回 (seq > after_seq 的事件, run 是否已终结)。"""
        state = None
        if self._active and self._active.run_id == run_id:
            state = self._active
        else:
            base = self._find_archive_base(run_id)
            if base is not None and base.with_suffix(".events.json").exists():
                evs = json.loads(base.with_suffix(".events.json").read_text(encoding="utf-8"))
                return ([e for e in evs if e["seq"] > after_seq], True)
            return ([], True)
        with state.lock:
            return ([e for e in state.events if e["seq"] > after_seq],
                    state.status != "running")

    # -- worker -------------------------------------------------------------

    def _worker(self, state: RunState) -> None:
        events = AgentEvents(sink=lambda t, p: self._emit(state, t, p),
                             cancel=state.cancel)
        try:
            traj, store = self._run_agent(state, events)
            state.traj = traj
            state.store = store
            path = traj.save(self.runs_dir, save_prompt=self.cfg.trajectory.save_prompt_text)
            state.traj_path = str(path)
            # 按 run_id 落盘副本 + 事件 + 快照(live 结束后可精确回放/取快照)
            run_path = self.runs_dir / f"{state.run_id}.json"
            run_path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            state.events_path = str(self.runs_dir / f"{state.run_id}.events.json")
            Path(state.events_path).write_text(
                json.dumps(state.events, ensure_ascii=False), encoding="utf-8")
            self._dump_snapshots(state, store)
            with state.lock:
                state.status = "passed" if traj.passed else "failed"
                if state.cancel.is_set():
                    state.status = "cancelled"
        except Exception as e:  # noqa: BLE001 — worker 兜底,状态置 error
            logger.exception("run %s failed", state.run_id)
            state.error = str(e)
            with state.lock:
                state.status = "error"
        finally:
            state.finished = time.time()
            with self._active_lock:
                if self._active is state:
                    self._active = None

    def _run_agent(self, state: RunState, events: AgentEvents):
        """装配并执行 agent。

        两种模式:
        - 评估模式(job_id 指定): 用案例的检测时刻与数据源(ground_truth 仅评估用);
        - 实时排查模式(job_id 空, im 环境): 数据源为环境固定日志,
          检测时刻 = 当前时刻,Agent 自动聚焦最近故障(需填异常描述,不支持 mock)。
        """
        from ..core.agent import JobDesc, RCAgent
        from ..experts.knowledge import build_demo_kb, build_im_kb
        from ..llm.embedding import Embedder

        if state.env == "im":
            from ..env.im_env import IMEnvironment, IM_JOBS_DIR
            from ..env.local import load_job

            env_cls = IMEnvironment
            data_dir = IM_JOBS_DIR
            kb_fn = build_im_kb
            task_requirements = (
                Path(__file__).resolve().parent.parent.parent
                / "config" / "prompts" / "task_requirements_im.txt"
            ).read_text(encoding="utf-8")
        else:
            from ..env.local import DATA_DIR, LocalEnvironment, load_job

            env_cls = LocalEnvironment
            data_dir = DATA_DIR
            kb_fn = build_demo_kb
            task_requirements = None

        if state.job_id:
            meta = load_job(state.job_id, data_dir)
            detect_time = state.detect_time or meta["detect_time"]
            anomaly = (state.anomaly or meta["anomaly"]).strip() or meta["anomaly"]
        else:
            # 实时排查模式: 数据源固定,检测时刻 = 当前
            if state.env != "im":
                raise ValueError("demo 环境为评估数据集,请指定实例 ID")
            if state.mock:
                raise ValueError("实时排查模式不支持 mock,请取消 mock 勾选")
            if not (state.anomaly or "").strip():
                raise ValueError("实时排查模式必须填写异常描述")
            import datetime

            meta = {"job_id": "realtime"}
            detect_time = state.detect_time or datetime.datetime.now(
                datetime.timezone(datetime.timedelta(hours=8))).strftime(
                "%Y-%m-%dT%H:%M:%S+08:00")
            anomaly = state.anomaly.strip()

        job = JobDesc(
            job_id=meta["job_id"],
            anomaly=anomaly,
            detect_time=detect_time,
        )

        if state.mock:
            from ..llm.client import LLMClient
            from ..main import make_demo_mock

            gt = meta.get("ground_truth", {})
            finalize_result = {
                "root_cause": gt.get("root_cause", "unknown"),
                "solution": gt.get("solution", "unknown"),
                "evidence": "observed fatal error lines in runtime log",
                "responsibility": gt.get("responsibility", "platform"),
            }
            llm = LLMClient(self.cfg.llm,
                            mock_script=make_demo_mock(state.job_id, finalize_result))
            mock_cfg = _override_embedding_provider(self.cfg, "mock")
            embedder = Embedder(mock_cfg.embedding)
        else:
            from ..llm.client import LLMClient

            llm = LLMClient(self.cfg.llm)
            embedder = Embedder(self.cfg.embedding)

        env = env_cls(llm=llm, embedder=embedder, kb=kb_fn(embedder))
        agent = RCAgent.build(self.cfg, llm, env, variant=state.variant,
                              events=events, task_requirements=task_requirements)
        traj = agent.run(job, decode_mode=state.decode)
        return traj, agent.store

    def _dump_snapshots(self, state: RunState, store) -> None:
        """run 结束后快照 dump 到 runs/snapshots/{run_id}/{key}.txt。"""
        if store is None:
            return
        out = self.snapshots_dir / state.run_id
        out.mkdir(parents=True, exist_ok=True)
        for key, content in list(store._store.items()):
            (out / f"{key}.txt").write_text(content, encoding="utf-8")

    def _emit(self, state: RunState, etype: str, payload: dict) -> None:
        with state.lock:
            state.seq += 1
            envelope = {"seq": state.seq, "run_id": state.run_id,
                        "ts": time.time(), "type": etype, "payload": payload}
            state.events.append(envelope)
            if len(state.events) > EVENT_LOG_CAP:
                del state.events[: len(state.events) - EVENT_LOG_CAP]

    def _meta(self, s: RunState) -> dict:
        return {
            "run_id": s.run_id, "job_id": s.job_id, "status": s.status,
            "steps": len(s.traj.records) if s.traj else 0,
            "started": s.started, "finished": s.finished,
            "mode": "mock" if s.mock else "real",
            "variant": s.variant, "decode": s.decode,
            "source": "live",
        }

    def _find_archive_base(self, run_id: str) -> Path | None:
        """按 run_id 找轨迹 JSON;排除 .events.json 事件文件(防御误匹配)。"""
        if run_id.endswith(".events"):
            return None
        p = self.runs_dir / f"{run_id}.json"
        return p if p.exists() else None


def _override_embedding_provider(cfg: Config, provider: str) -> Config:
    """复制配置并覆写 embedding.provider(mock 分支用,不落盘)。"""
    import copy

    data = copy.deepcopy(cfg.to_dict())
    data.setdefault("embedding", {})["provider"] = provider
    return Config(data)
