from core.queues.dedup import (
    ClaimResult,
    acquire as dedup_acquire,
    attach_workflow_id as dedup_attach,
    peek as dedup_peek,
    release as dedup_release,
)
from core.queues.redis_queue import QueueName, RedisQueue, get_queue

__all__ = [
    "RedisQueue", "QueueName", "get_queue",
    "ClaimResult", "dedup_acquire", "dedup_attach", "dedup_peek", "dedup_release",
]
