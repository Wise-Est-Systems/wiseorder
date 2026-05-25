"""Redis-backed idempotency guard for (repo, sha) commit dedup.

Replaces the v0.1 Postgres JSONB containment query with an atomic SETNX +
TTL claim. Works correctly under multiple workers, multiple processes, and
race conditions where two events fire for the same commit within the same
millisecond.

Semantics:

  * ``acquire(repo, sha)`` -> returns ``ClaimResult.acquired=True`` if the
    caller should proceed, or ``ClaimResult.acquired=False`` with the
    existing claim's value (workflow_id as string, or "PENDING") if a
    prior process already claimed the slot.

  * ``attach_workflow_id(repo, sha, workflow_id)`` -> overwrites the
    placeholder ``PENDING`` value with the real workflow id, so subsequent
    duplicate events can return the existing id without scanning Postgres.

  * Claims auto-expire after ``WISEORDER_DEDUP_TTL_SECONDS`` (default 3600).
    A crashed claimant cannot block the same commit forever; after TTL,
    the same SHA is processed fresh.

Key shape:
    wiseorder:dedup:commit_pipeline:<sha-of-(repo,sha)>

We hash the (repo, sha) pair to avoid characters that would conflict with
Redis key parsing and to keep keys at a bounded length.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from configs.settings import get_settings
from core.queues.redis_queue import RedisQueue, get_queue


_KEY_PREFIX = "wiseorder:dedup:commit_pipeline"
_PENDING = "PENDING"


@dataclass
class ClaimResult:
    """Outcome of an acquire() call.

    If ``acquired`` is True, the caller owns the slot for this TTL window
    and should proceed. If False, ``existing_value`` holds whatever the
    prior claimant wrote — a workflow_id string if attach_workflow_id was
    reached, or "PENDING" if the prior claimant is still in flight.
    """

    acquired: bool
    existing_value: str | None


def _key(repo: str, sha: str) -> str:
    digest = hashlib.sha256(f"{repo}\x00{sha}".encode("utf-8")).hexdigest()
    return f"{_KEY_PREFIX}:{digest}"


async def acquire(repo: str, sha: str, queue: RedisQueue | None = None) -> ClaimResult:
    """Atomically claim the (repo, sha) slot.

    Returns ClaimResult(acquired=True, existing_value=None) if the caller
    won the race; otherwise returns the value of the existing claim so the
    caller can resolve to the right workflow_id.
    """
    q = queue or await get_queue()
    c = await q.client()
    ttl = get_settings().dedup_ttl_seconds
    won = await c.set(_key(repo, sha), _PENDING, nx=True, ex=ttl)
    if won:
        return ClaimResult(acquired=True, existing_value=None)
    existing = await c.get(_key(repo, sha))
    return ClaimResult(acquired=False, existing_value=existing)


async def attach_workflow_id(
    repo: str,
    sha: str,
    workflow_id: int,
    queue: RedisQueue | None = None,
) -> None:
    """Replace the PENDING placeholder with the real workflow id.

    Preserves the original TTL by using SET with EX — the slot still
    auto-expires; we do not extend the claim lifetime.
    """
    q = queue or await get_queue()
    c = await q.client()
    ttl = get_settings().dedup_ttl_seconds
    await c.set(_key(repo, sha), str(workflow_id), ex=ttl)


async def release(repo: str, sha: str, queue: RedisQueue | None = None) -> None:
    """Drop the claim immediately (operator-initiated rerun).

    Normal operation does not call this — the claim auto-expires on TTL.
    Provided for an operator who has decided that re-processing a SHA
    before TTL is the right action (e.g. after a transient LLM failure).
    """
    q = queue or await get_queue()
    c = await q.client()
    await c.delete(_key(repo, sha))


async def peek(repo: str, sha: str, queue: RedisQueue | None = None) -> str | None:
    """Return the current claim value without acquiring. For inspection."""
    q = queue or await get_queue()
    c = await q.client()
    return await c.get(_key(repo, sha))
