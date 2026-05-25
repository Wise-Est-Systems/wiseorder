from __future__ import annotations

import argparse
import asyncio
import signal
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

import uvicorn
from sqlalchemy import select, update

from configs.logging import bind_job, bind_workflow, current_job_id, current_workflow_id, get_logger
from configs.settings import get_settings
from core.events.watcher import EventWatcher
from core.memory.db import init_db, ping as db_ping, session_scope
from core.memory.models import Task, Workflow
from core.queues import QueueName, get_queue
from core.queues.redis_queue import Job
from core.watchers.integrity_watcher import IntegrityWatcher
from workflows.commit_pipeline import run_commit_pipeline
from workflows.daily_summary import schedule_daily_summary


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
        self.integrity_watcher: IntegrityWatcher | None = None  # exposed for dashboard
        self._integrity_task: asyncio.Task | None = None
        self._daily_summary_task: asyncio.Task | None = None
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
            await self._run_job(worker_id, q, job)
        log.info({"msg": "worker_stopped", "id": worker_id})

    async def _run_job(self, worker_id: int, q, job: Job) -> None:
        handler = self._handlers.get(job.type)
        if handler is None:
            log.error({"msg": "worker_no_handler", "type": job.type, "job_id": job.id})
            await q.fail(job, f"no handler for type {job.type!r}")
            return
        job_token = bind_job(job.id)
        try:
            log.info({"msg": "worker_job_start", "worker": worker_id, "type": job.type})
            result = await handler(job.payload)
            log.info({
                "msg": "worker_job_done",
                "worker": worker_id,
                "result_keys": list(result.keys()) if isinstance(result, dict) else [],
            })
        except Exception as e:
            log.exception({"msg": "worker_job_failed", "worker": worker_id, "err": str(e)})
            await q.fail(job, str(e))
        finally:
            current_job_id.reset(job_token)

    async def _serve_api(self) -> None:
        from api.server import build_app

        s = get_settings()
        _validate_bind_safety(s.api_host, s.api_allow_remote_bind, s.api_auth_token)
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
            log.error({"msg": "database_unreachable", "url": s.database_url,
                       "hint": "run `docker compose up -d` then retry"})
            raise RuntimeError(f"cannot connect to database: {s.database_url}")
        await init_db()
        log.info({"msg": "database_ready"})

        q = await get_queue()
        if not await q.ping():
            log.error({"msg": "redis_unreachable", "url": s.redis_url,
                       "hint": "run `docker compose up -d` then retry"})
            raise RuntimeError(f"cannot connect to redis: {s.redis_url}")
        log.info({"msg": "redis_ready"})

        reaped = await reap_orphan_workflows(s.orphan_workflow_max_age_seconds)
        if reaped:
            log.warning({"msg": "orphan_workflows_reaped", "count": reaped})
        else:
            log.info({"msg": "no_orphan_workflows"})

        for i in range(self.num_workers):
            self._workers.append(asyncio.create_task(self._worker(i), name=f"worker-{i}"))

        loop = asyncio.get_running_loop()
        self._watcher = EventWatcher()
        self._watcher.start(loop)

        # Background services: integrity watcher (polls protocol chain) +
        # daily-summary scheduler (writes one summary per UTC day).
        self.integrity_watcher = IntegrityWatcher()
        self._integrity_task = asyncio.create_task(
            self.integrity_watcher.run(), name="integrity-watcher"
        )
        self._daily_summary_task = asyncio.create_task(
            schedule_daily_summary(), name="daily-summary-scheduler"
        )

        self._api_task = asyncio.create_task(self._serve_api(), name="api-server")
        log.info({"msg": "orchestrator_ready", "api": f"http://{s.api_host}:{s.api_port}"})

    async def stop(self) -> None:
        log.info({"msg": "orchestrator_stopping"})
        self._stop.set()
        if self._watcher is not None:
            self._watcher.stop()
        if self.integrity_watcher is not None:
            self.integrity_watcher.stop()
        if self._integrity_task is not None:
            self._integrity_task.cancel()
        if self._daily_summary_task is not None:
            self._daily_summary_task.cancel()
        if self._api_server is not None:
            self._api_server.should_exit = True
        if self._api_task is not None:
            try:
                await asyncio.wait_for(self._api_task, timeout=5)
            except asyncio.TimeoutError:
                self._api_task.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        # Await the background tasks to actually exit (they cancel cleanly)
        for t in (self._integrity_task, self._daily_summary_task):
            if t is not None:
                try:
                    await asyncio.wait_for(t, timeout=3)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
        q = await get_queue()
        await q.close()
        log.info({"msg": "orchestrator_stopped"})

    async def enqueue_manual(self, type: str, payload: dict) -> str:
        q = await get_queue()
        job = Job.new(type=type, payload=payload)
        await q.enqueue(job, queue=QueueName.HIGH)
        return job.id


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _validate_bind_safety(host: str, allow_remote: bool, auth_token: str) -> None:
    """Refuse to start with an unsafe bind configuration.

    Rules:
      * Loopback (127.0.0.1, localhost, ::1) is always allowed.
      * Any other bind requires both ``WISEORDER_API_ALLOW_REMOTE_BIND=true``
        AND a non-empty ``WISEORDER_API_AUTH_TOKEN``. The token gates every
        endpoint except the dashboard and health probes (see api/server.py).
      * Without the token, remote-bind is refused even if allow_remote=true,
        because the dashboard's POST endpoints (approvals/decide, trigger)
        would otherwise be unauthenticated on a public interface.
    """
    if host in _LOOPBACK_HOSTS:
        return
    if not allow_remote:
        raise RuntimeError(
            f"refusing to bind API to non-loopback host {host!r}. "
            f"Set WISEORDER_API_ALLOW_REMOTE_BIND=true to opt in."
        )
    if not auth_token:
        raise RuntimeError(
            f"refusing to bind API to {host!r} without auth. "
            f"Set WISEORDER_API_AUTH_TOKEN to a non-empty secret first."
        )
    log.warning({
        "msg": "api_bound_to_remote_interface",
        "host": host,
        "hint": "endpoints with side effects require Authorization: Bearer <token>",
    })


async def reap_orphan_workflows(max_age_seconds: int) -> int:
    """Mark workflows still 'running' past max_age as 'interrupted'.

    Run at orchestrator start. Any workflow whose host process crashed before
    it could mark itself completed or failed will be claimed by this reaper,
    so the dashboard does not show ghost workflows that never resolve. Their
    in-progress tasks are also marked 'interrupted'.

    Returns the number of workflows reaped.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)
    interrupted_marker = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": "workflow_interrupted_at_startup",
        "data": {"reason": "orphan reaper", "max_age_seconds": max_age_seconds},
    }
    async with session_scope() as session:
        wf_rows = (await session.execute(
            select(Workflow).where(
                Workflow.status == "running",
                Workflow.created_at < cutoff,
            )
        )).scalars().all()
        for wf in wf_rows:
            wf.status = "interrupted"
            wf.completed_at = datetime.now(timezone.utc)
            wf.logs = list(wf.logs) + [interrupted_marker]
            await session.execute(
                update(Task)
                .where(Task.workflow_id == wf.id, Task.status == "running")
                .values(status="interrupted", completed_at=datetime.now(timezone.utc),
                        error="orphaned at orchestrator startup")
            )
        return len(wf_rows)


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


async def _probe(num_workers: int) -> int:
    """`--probe-services`: verify Postgres + Redis reachable and exit without
    starting workers. Useful in CI / pre-flight checks."""
    s = get_settings()
    ok_db = await db_ping()
    q = await get_queue()
    ok_redis = await q.ping()
    await q.close()
    print(f"database: {'OK' if ok_db else 'UNREACHABLE'}  ({s.database_url})")
    print(f"redis:    {'OK' if ok_redis else 'UNREACHABLE'}  ({s.redis_url})")
    return 0 if (ok_db and ok_redis) else 2


def cli() -> None:
    p = argparse.ArgumentParser(prog="wiseorder")
    p.add_argument("--workers", type=int, default=2, help="number of async workers (default 2)")
    p.add_argument("--probe-services", action="store_true",
                   help="check DB + Redis reachability and exit (no workers started)")
    args = p.parse_args()
    if args.probe_services:
        raise SystemExit(asyncio.run(_probe(args.workers)))
    asyncio.run(_run(args.workers))


if __name__ == "__main__":
    cli()
