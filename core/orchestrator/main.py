from __future__ import annotations

import argparse
import asyncio
import signal
from typing import Awaitable, Callable

import uvicorn

from configs.logging import get_logger
from configs.settings import get_settings
from core.events.watcher import EventWatcher
from core.memory.db import init_db, ping as db_ping
from core.queues import QueueName, get_queue
from core.queues.redis_queue import Job
from workflows.commit_pipeline import run_commit_pipeline


log = get_logger(__name__)


HandlerFn = Callable[[dict], Awaitable[dict]]


class Orchestrator:
    """Central async runtime: workers, event watcher, FastAPI server."""

    def __init__(self, num_workers: int = 2) -> None:
        self.num_workers = num_workers
        self._handlers: dict[str, HandlerFn] = {}
        self._stop = asyncio.Event()
        self._workers: list[asyncio.Task] = []
        self._watcher: EventWatcher | None = None
        self._api_server: uvicorn.Server | None = None
        self._api_task: asyncio.Task | None = None
        self.register("commit_pipeline", run_commit_pipeline)

    def register(self, job_type: str, fn: HandlerFn) -> None:
        self._handlers[job_type] = fn

    async def _worker(self, worker_id: int) -> None:
        log.info({"msg": "worker_started", "id": worker_id})
        q = await get_queue()
        while not self._stop.is_set():
            try:
                job = await q.dequeue(timeout=1.0)
            except Exception as e:
                log.error({"msg": "worker_dequeue_error", "id": worker_id, "err": str(e)})
                await asyncio.sleep(1)
                continue
            if job is None:
                continue
            handler = self._handlers.get(job.type)
            if handler is None:
                log.error({"msg": "worker_no_handler", "type": job.type, "job_id": job.id})
                await q.fail(job, f"no handler for type {job.type!r}")
                continue
            log.info({"msg": "worker_job_start", "id": worker_id, "job_id": job.id, "type": job.type})
            try:
                result = await handler(job.payload)
                log.info(
                    {
                        "msg": "worker_job_done",
                        "id": worker_id,
                        "job_id": job.id,
                        "result_keys": list(result.keys()) if isinstance(result, dict) else [],
                    }
                )
            except Exception as e:
                log.exception({"msg": "worker_job_failed", "id": worker_id, "job_id": job.id, "err": str(e)})
                await q.fail(job, str(e))
        log.info({"msg": "worker_stopped", "id": worker_id})

    async def _serve_api(self) -> None:
        from api.server import build_app

        s = get_settings()
        app = build_app(self)
        config = uvicorn.Config(
            app,
            host=s.api_host,
            port=s.api_port,
            log_level=s.log_level.lower(),
            loop="asyncio",
            lifespan="on",
        )
        self._api_server = uvicorn.Server(config)
        await self._api_server.serve()

    async def start(self) -> None:
        s = get_settings()
        log.info({"msg": "orchestrator_starting", "workers": self.num_workers})

        if not await db_ping():
            log.error(
                {
                    "msg": "database_unreachable",
                    "url": s.database_url,
                    "hint": "run `docker compose up -d` then retry",
                }
            )
            raise RuntimeError(f"cannot connect to database: {s.database_url}")

        await init_db()
        log.info({"msg": "database_ready"})

        q = await get_queue()
        if not await q.ping():
            log.error(
                {
                    "msg": "redis_unreachable",
                    "url": s.redis_url,
                    "hint": "run `docker compose up -d` then retry",
                }
            )
            raise RuntimeError(f"cannot connect to redis: {s.redis_url}")
        log.info({"msg": "redis_ready"})

        for i in range(self.num_workers):
            self._workers.append(asyncio.create_task(self._worker(i), name=f"worker-{i}"))

        loop = asyncio.get_running_loop()
        self._watcher = EventWatcher()
        self._watcher.start(loop)

        self._api_task = asyncio.create_task(self._serve_api(), name="api-server")
        log.info({"msg": "orchestrator_ready", "api": f"http://{s.api_host}:{s.api_port}"})

    async def stop(self) -> None:
        log.info({"msg": "orchestrator_stopping"})
        self._stop.set()
        if self._watcher is not None:
            self._watcher.stop()
        if self._api_server is not None:
            self._api_server.should_exit = True
        if self._api_task is not None:
            try:
                await asyncio.wait_for(self._api_task, timeout=5)
            except asyncio.TimeoutError:
                self._api_task.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        q = await get_queue()
        await q.close()
        log.info({"msg": "orchestrator_stopped"})

    async def enqueue_manual(self, type: str, payload: dict) -> str:
        """Used by the API for manual triggering / testing."""
        q = await get_queue()
        job = Job.new(type=type, payload=payload)
        await q.enqueue(job, queue=QueueName.HIGH)
        return job.id


async def _run(num_workers: int) -> None:
    orch = Orchestrator(num_workers=num_workers)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    await orch.start()
    try:
        await stop_event.wait()
    finally:
        await orch.stop()


def cli() -> None:
    p = argparse.ArgumentParser(prog="wiseorder")
    p.add_argument(
        "--workers", type=int, default=2, help="number of async workers (default 2)"
    )
    args = p.parse_args()
    asyncio.run(_run(args.workers))


if __name__ == "__main__":
    cli()
