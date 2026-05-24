from __future__ import annotations

import asyncio
import enum
import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as aioredis

from configs.settings import get_settings


class QueueName(str, enum.Enum):
    HIGH = "wiseorder:queue:high_priority"
    NORMAL = "wiseorder:queue:normal_priority"
    FAILED = "wiseorder:queue:failed_jobs"


@dataclass
class Job:
    id: str
    type: str
    payload: dict[str, Any]
    created_at: str
    attempt: int = 0
    workflow_id: int | None = None
    parent_job_id: str | None = None
    notes: list[str] = field(default_factory=list)

    @classmethod
    def new(
        cls,
        type: str,
        payload: dict[str, Any],
        workflow_id: int | None = None,
        parent_job_id: str | None = None,
    ) -> "Job":
        return cls(
            id=str(uuid.uuid4()),
            type=type,
            payload=payload,
            created_at=datetime.now(timezone.utc).isoformat(),
            workflow_id=workflow_id,
            parent_job_id=parent_job_id,
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, blob: str) -> "Job":
        return cls(**json.loads(blob))


class RedisQueue:
    """Async Redis-backed queue using RPUSH/BLPOP semantics.

    Three queues: high, normal, failed. The worker drains high before normal.
    Failed jobs land in `FAILED` for inspection (no automatic retry loop — by design).
    """

    def __init__(self, url: str | None = None) -> None:
        self.url = url or get_settings().redis_url
        self._client: aioredis.Redis | None = None

    async def client(self) -> aioredis.Redis:
        if self._client is None:
            self._client = aioredis.from_url(self.url, decode_responses=True)
        return self._client

    async def enqueue(self, job: Job, queue: QueueName = QueueName.NORMAL) -> None:
        c = await self.client()
        await c.rpush(queue.value, job.to_json())

    async def dequeue(
        self,
        timeout: float = 1.0,
        queues: list[QueueName] | None = None,
    ) -> Job | None:
        c = await self.client()
        qs = [q.value for q in (queues or [QueueName.HIGH, QueueName.NORMAL])]
        res = await c.blpop(qs, timeout=timeout)
        if res is None:
            return None
        _qname, blob = res
        return Job.from_json(blob)

    async def fail(self, job: Job, error: str) -> None:
        job.notes.append(f"failed: {error}")
        c = await self.client()
        await c.rpush(QueueName.FAILED.value, job.to_json())

    async def depth(self, queue: QueueName) -> int:
        c = await self.client()
        return int(await c.llen(queue.value))

    async def all_depths(self) -> dict[str, int]:
        return {q.name.lower(): await self.depth(q) for q in QueueName}

    async def peek_failed(self, limit: int = 20) -> list[Job]:
        c = await self.client()
        items = await c.lrange(QueueName.FAILED.value, 0, limit - 1)
        return [Job.from_json(x) for x in items]

    async def ping(self) -> bool:
        try:
            c = await self.client()
            return await c.ping()
        except Exception:
            return False

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None


_queue: RedisQueue | None = None
_lock = asyncio.Lock()


async def get_queue() -> RedisQueue:
    global _queue
    async with _lock:
        if _queue is None:
            _queue = RedisQueue()
        return _queue
