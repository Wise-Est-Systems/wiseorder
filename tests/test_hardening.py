"""Failure-injection and recovery tests.

Split into two halves:
  * Pure (no services required): LLM timeout/retry, malformed payload,
    correlation IDs, settings race.
  * Integration (Postgres + Redis required, auto-skip otherwise):
    idempotency, orphan reaper, vector upsert, dead-letter inspection.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from agents.engineering.summarizer import EngineeringSummary, _llm_call
from agents.social.post_generator import SocialDraft
from configs.logging import bind_workflow, current_workflow_id
from configs.settings import get_settings
from core.memory.db import init_db, ping as db_ping, session_scope
from core.queues import get_queue


# ---------------------------------------------------------------------------
# Pure tests (no services)
# ---------------------------------------------------------------------------


async def test_llm_timeout_retries_then_raises() -> None:
    """An LLM that always times out must surface a TimeoutError after retries
    rather than hang the worker."""
    calls = {"n": 0}

    async def slow(*a, **kw):
        calls["n"] += 1
        await asyncio.sleep(10)  # would exceed timeout=0.05

    with patch("agents.engineering.summarizer.acompletion", side_effect=slow):
        with pytest.raises(asyncio.TimeoutError):
            await _llm_call(
                model="x", prompt="p", temperature=0, max_tokens=1,
                timeout=0.05, max_retries=2,
            )
    assert calls["n"] == 3  # initial + 2 retries


async def test_llm_succeeds_on_second_attempt() -> None:
    """A transient timeout followed by success should succeed."""
    state = {"n": 0}

    async def flaky(*a, **kw):
        state["n"] += 1
        if state["n"] == 1:
            await asyncio.sleep(10)
        return {"choices": [{"message": {"content": "ok"}}]}

    with patch("agents.engineering.summarizer.acompletion", side_effect=flaky):
        out = await _llm_call(
            model="x", prompt="p", temperature=0, max_tokens=1,
            timeout=0.05, max_retries=2,
        )
    assert out == "ok"
    assert state["n"] == 2


def test_settings_singleton_thread_safe() -> None:
    """Concurrent get_settings() calls return the same instance."""
    # Force reset for this test
    import configs.settings as cs
    cs._settings = None

    results: list = []

    def grab():
        results.append(get_settings())

    threads = [threading.Thread(target=grab) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(set(id(r) for r in results)) == 1


async def test_correlation_id_binding() -> None:
    """bind_workflow/job tokens scope correctly within an async context."""
    assert current_workflow_id.get() is None
    token = bind_workflow(42)
    try:
        assert current_workflow_id.get() == 42
    finally:
        current_workflow_id.reset(token)
    assert current_workflow_id.get() is None


def test_pipeline_rejects_payload_missing_required_keys() -> None:
    """Malformed payloads with no `sha` must raise immediately, not silently."""
    from workflows.commit_pipeline import _require_keys

    with pytest.raises(ValueError, match="missing required keys"):
        _require_keys({"repo": "x"}, {"repo", "sha"})


def test_watcher_skips_non_git_paths(tmp_path) -> None:
    """A watch path without a .git directory is skipped with a warning, not crashed."""
    from core.events.watcher import EventWatcher

    ew = EventWatcher(paths=[str(tmp_path)])  # tmp dir has no .git
    loop = asyncio.new_event_loop()
    try:
        ew.start(loop)
        assert ew._observer is None or len(ew._handlers) == 0
    finally:
        ew.stop()
        loop.close()


# ---------------------------------------------------------------------------
# Integration tests (Postgres + Redis required)
# ---------------------------------------------------------------------------


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


async def test_idempotency_same_sha_skips(services_or_skip) -> None:
    """Running commit_pipeline twice for the same (repo, sha) must produce
    one workflow row, not two."""
    from workflows.commit_pipeline import run_commit_pipeline

    fake_eng = EngineeringSummary(
        summary="x", changed_files=["a"], changelog="y", risk_level="low",
    )
    fake_soc = SocialDraft(post="z")
    sha = "dedupe_" + datetime.now(timezone.utc).isoformat().replace(":", "")
    payload = {
        "repo": "/tmp/idempotency-test", "sha": sha, "prev_sha": None,
        "author": "Test <t@t>", "subject": "first call",
        "diff": "diff --git a/x b/x\n",
    }

    with (
        patch("workflows.commit_pipeline.EngineeringSummarizer") as Eng,
        patch("workflows.commit_pipeline.SocialDrafter") as Soc,
    ):
        Eng.return_value.summarize = AsyncMock(return_value=fake_eng)
        Soc.return_value.draft = AsyncMock(return_value=fake_soc)
        first = await run_commit_pipeline(payload)
        second = await run_commit_pipeline(payload)

    assert "workflow_id" in first
    assert second.get("skipped") is True
    assert second["workflow_id"] == first["workflow_id"]


async def test_orphan_reaper_marks_stale_running(services_or_skip) -> None:
    """A workflow stuck in 'running' past max_age is marked 'interrupted'
    at orchestrator startup."""
    from sqlalchemy import select
    from core.memory.models import Workflow
    from core.orchestrator.main import reap_orphan_workflows

    stale_ts = datetime.now(timezone.utc) - timedelta(hours=1)
    async with session_scope() as session:
        wf = Workflow(
            workflow_name="commit_pipeline",
            task_chain=["x"], status="running",
            logs=[{"ts": stale_ts.isoformat(), "event": "workflow_started", "data": {}}],
        )
        session.add(wf)
        await session.flush()
        # Manually backdate created_at because server_default=NOW() ignores client time
        from sqlalchemy import update
        await session.execute(
            update(Workflow).where(Workflow.id == wf.id).values(created_at=stale_ts)
        )
        wf_id = wf.id

    reaped = await reap_orphan_workflows(max_age_seconds=60)
    assert reaped >= 1

    async with session_scope() as session:
        row = (await session.execute(
            select(Workflow).where(Workflow.id == wf_id)
        )).scalar_one()
        assert row.status == "interrupted"
        assert row.completed_at is not None


async def test_vector_upsert_replaces_existing_id(services_or_skip) -> None:
    """Vector store .upsert() must not raise on duplicate id — the v1
    failure mode was .add() raising IDAlreadyExists and silently corrupting
    the save step."""
    from core.memory.vector import get_vector_store

    vs = get_vector_store()
    vid = "test_upsert_" + datetime.now(timezone.utc).isoformat()
    vs.upsert(ids=[vid], documents=["first"], metadatas=[{"r": 1}])
    vs.upsert(ids=[vid], documents=["second"], metadatas=[{"r": 2}])
    hits = vs.query("second", n_results=5)
    assert any(h["id"] == vid and h["document"] == "second" for h in hits)
    vs.delete([vid])


async def test_dead_letter_inspection_returns_failed_job(services_or_skip) -> None:
    """A failed job must appear in /queues/failed with its error notes."""
    from core.queues import QueueName
    from core.queues.redis_queue import Job

    q = await get_queue()
    job = Job.new(type="commit_pipeline", payload={"repo": "x", "sha": "deadletter_probe"})
    await q.enqueue(job, queue=QueueName.HIGH)
    got = await q.dequeue(timeout=2.0, queues=[QueueName.HIGH])
    assert got is not None and got.id == job.id
    await q.fail(got, "synthetic failure for dead-letter test")

    failed = await q.peek_failed(limit=50)
    assert any(j.id == job.id for j in failed)
    matched = next(j for j in failed if j.id == job.id)
    assert matched.attempt >= 1
    assert any("synthetic failure" in n for n in matched.notes)
