"""FastAPI 服务端: REST 路由 + SSE 事件流 + 静态页面。

启动: python -m rcagent.web [--host 127.0.0.1] [--port 8080] [--mock]
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..config import load_config
from .events import format_sse
from .runs import RunBusy, RunManager
from .schemas import StartRunRequest

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
HEARTBEAT_INTERVAL = 15.0
POLL_INTERVAL = 0.05


def create_app(cfg=None, runs_dir: str = "runs") -> FastAPI:
    cfg = cfg or load_config()
    manager = RunManager(cfg, runs_dir=runs_dir)

    app = FastAPI(
        title="RCAgent Web Runtime",
        description="RCAgent(arXiv:2310.16340 复现)在线运行时:提交任务、实时观察 Loop 节点状态。",
        version="0.1.0",
    )
    app.state.manager = manager

    # -- 任务提交与查询 -----------------------------------------------------

    @app.get("/api/jobs")
    def list_jobs(env: str = "demo"):
        """job 列表: demo(合成)| im(QuantumLink IM 真实案例)。"""
        if env == "im":
            from ..env.im_env import IM_JOBS_DIR

            data_dir = IM_JOBS_DIR
        else:
            from ..env.local import DATA_DIR

            data_dir = DATA_DIR
        jobs = []
        for p in sorted(data_dir.glob("*/job.json")):
            try:
                meta = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            jobs.append({"job_id": meta.get("job_id", p.parent.name),
                         "anomaly": meta.get("anomaly", ""),
                         "detect_time": meta.get("detect_time", "")})
        return jobs

    @app.get("/api/cases")
    def list_cases():
        """案例库: 全部案例(env + job_id + 异常 + ground_truth)。"""
        from ..env.im_env import IM_JOBS_DIR
        from ..env.local import DATA_DIR

        cases = []
        for env_name, data_dir in (("demo", DATA_DIR), ("im", IM_JOBS_DIR)):
            for p in sorted(data_dir.glob("*/job.json")):
                try:
                    meta = json.loads(p.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                cases.append({
                    "env": env_name,
                    "job_id": meta.get("job_id", p.parent.name),
                    "anomaly": meta.get("anomaly", ""),
                    "detect_time": meta.get("detect_time", ""),
                    "ground_truth": meta.get("ground_truth", {}),
                })
        return cases

    @app.get("/api/kb")
    def list_kb():
        """知识库(RAG 检索内容): 示例-答案对(env 分组)。"""
        from ..experts.knowledge import demo_kb_examples, im_kb_examples

        def _dump(examples, env_name):
            return [{"env": env_name, "text": e.text, "answer": e.answer}
                    for e in examples]

        return _dump(demo_kb_examples(), "demo") + _dump(im_kb_examples(), "im")

    @app.post("/api/runs", status_code=201)
    def start_run(req: StartRunRequest):
        try:
            run_id = manager.start(req.job_id, variant=req.variant,
                                   decode=req.decode, mock=req.mock,
                                   anomaly=req.anomaly, env=req.env,
                                   detect_time=req.detect_time)
        except RunBusy as e:
            raise HTTPException(status_code=409, detail=str(e)) from None
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        return {"run_id": run_id, "status": "running"}

    @app.get("/api/runs")
    def list_runs(limit: int = 50):
        return manager.list_runs(limit=limit)

    @app.get("/api/runs/active")
    def active_run():
        run_id = manager.active_run_id()
        return {"run_id": run_id}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str):
        snap = manager.get_run(run_id)
        if snap is None:
            raise HTTPException(status_code=404, detail="run not found")
        return snap

    @app.post("/api/runs/{run_id}/cancel")
    def cancel_run(run_id: str):
        return {"ok": manager.cancel(run_id)}

    @app.get("/api/runs/{run_id}/snapshots/{key}")
    def get_snapshot(run_id: str, key: str):
        content = manager.get_snapshot(run_id, key)
        if content is None:
            raise HTTPException(status_code=404, detail="snapshot not found")
        return {"key": key, "content": content}

    # -- SSE 事件流 -----------------------------------------------------------

    @app.get("/api/runs/{run_id}/events")
    async def event_stream(run_id: str, last_event_id: int | None = None):
        async def gen():
            after = last_event_id if last_event_id is not None else -1
            while True:
                batch, done = manager.events_since(run_id, after)
                for env in batch:
                    yield format_sse(env["seq"], env["run_id"], env["ts"],
                                     env["type"], env["payload"])
                    after = env["seq"]
                if done:
                    # 追平后补发一次心跳再结束,确保浏览器收到末尾事件
                    yield ": heartbeat done\n\n"
                    return
                yield ": heartbeat\n\n"
                await asyncio.sleep(POLL_INTERVAL)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # -- 静态页面 ---------------------------------------------------------------

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    return app


def main(argv: list[str] | None = None) -> int:
    import argparse

    import uvicorn

    ap = argparse.ArgumentParser(description="RCAgent Web Runtime")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--config", default=None)
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)
    app = create_app(cfg)
    print(f"RCAgent Web Runtime: http://{args.host}:{args.port}  (Ctrl+C 停止)")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
