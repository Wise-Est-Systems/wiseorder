"""Workflow integration test with stubbed LLM + real Postgres/Redis/Chroma.

Requires services: `docker compose up -d` before running.
Skips automatically if Postgres or Redis are not reachable.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from agents.engineering.summarizer import EngineeringSummary
from agents.social.post_generator import SocialDraft
from core.memory.db import init_db, ping as db_ping
from core.queues import get_queue


async def _services_up() -> bool:
    if not await db_ping():
        return False
    q = await get_queue()
    return bool(await q.ping())


@pytest.fixture(scope="session")
async def services_or_skip():
    if not await _services_up():
        pytest.skip("Postgres or Redis not reachable — run `docker compose up -d` first")
    await init_db()
    yield


async def test_commit_pipeline_end_to_end(services_or_skip) -> None:
    from workflows.commit_pipeline import run_commit_pipeline

    fake_summary = EngineeringSummary(
        summary="Refactored the queue dequeue path.",
        changed_files=["core/queues/redis_queue.py"],
        changelog="Refactor queue dequeue",
        risk_level="medium",
    )
    fake_draft = SocialDraft(post="Refactored queue dequeue (medium risk).")

    with (
        patch(
            "workflows.commit_pipeline.EngineeringSummarizer"
        ) as MockEng,
        patch(
            "workflows.commit_pipeline.SocialDrafter"
        ) as MockSoc,
    ):
        MockEng.return_value.summarize = AsyncMock(return_value=fake_summary)
        MockSoc.return_value.draft = AsyncMock(return_value=fake_draft)

        result = await run_commit_pipeline(
            {
                "repo": "/tmp/test-repo",
                "sha": "deadbeef" * 5,
                "prev_sha": "cafef00d" * 5,
                "author": "Test <t@t.local>",
                "subject": "refactor: clean dequeue path",
                "diff": "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n",
            }
        )

    assert "workflow_id" in result
    assert "approval_id" in result
    assert result["summary"]["risk_level"] == "medium"
    assert result["social_post"] == fake_draft.post


async def test_queue_enqueue_dequeue(services_or_skip) -> None:
    from core.queues import QueueName, get_queue
    from core.queues.redis_queue import Job

    q = await get_queue()
    job = Job.new(type="test_job", payload={"k": "v"})
    await q.enqueue(job, queue=QueueName.HIGH)
    got = await q.dequeue(timeout=2.0, queues=[QueueName.HIGH])
    assert got is not None
    assert got.id == job.id
    assert got.payload == {"k": "v"}
