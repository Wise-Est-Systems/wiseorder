"""Hardening follow-through tests (round 2): API security and Redis dedup.

Pure tests for security validation; service-dependent tests for the Redis
SETNX dedup guard. The Alembic config has its own validation step in CI
(see Makefile / docs); a runtime smoke test simply confirms importability.
"""

from __future__ import annotations

import pytest

from configs.settings import get_settings
from core.orchestrator.main import _validate_bind_safety


# ---------------------------------------------------------------------------
# API bind safety (pure)
# ---------------------------------------------------------------------------


def test_loopback_bind_always_allowed() -> None:
    # No-op for any of the three loopback forms
    _validate_bind_safety("127.0.0.1", allow_remote=False, auth_token="")
    _validate_bind_safety("localhost", allow_remote=False, auth_token="")
    _validate_bind_safety("::1", allow_remote=False, auth_token="")


def test_remote_bind_refused_without_allow_flag() -> None:
    with pytest.raises(RuntimeError, match="non-loopback"):
        _validate_bind_safety("0.0.0.0", allow_remote=False, auth_token="")


def test_remote_bind_refused_without_auth_token() -> None:
    with pytest.raises(RuntimeError, match="without auth"):
        _validate_bind_safety("0.0.0.0", allow_remote=True, auth_token="")


def test_remote_bind_allowed_with_flag_and_token() -> None:
    _validate_bind_safety("0.0.0.0", allow_remote=True, auth_token="ok-token-32-bytes-here")


# ---------------------------------------------------------------------------
# Alembic config (pure)
# ---------------------------------------------------------------------------


def test_alembic_revision_chain_parses() -> None:
    from pathlib import Path

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    script = ScriptDirectory.from_config(cfg)
    revs = list(script.walk_revisions())
    assert len(revs) >= 1
    head = script.get_current_head()
    assert head == "0002_distribution"


def test_dedup_key_is_stable() -> None:
    """The same (repo, sha) must produce the same key bytes; different
    inputs must produce different keys."""
    from core.queues.dedup import _key

    assert _key("/r", "abc") == _key("/r", "abc")
    assert _key("/r", "abc") != _key("/r", "abd")
    assert _key("/r", "abc") != _key("/s", "abc")


# ---------------------------------------------------------------------------
# Redis SETNX dedup (integration)
# ---------------------------------------------------------------------------


async def _redis_up() -> bool:
    from core.queues import get_queue

    q = await get_queue()
    return bool(await q.ping())


@pytest.fixture(scope="session")
async def redis_or_skip():
    if not await _redis_up():
        pytest.skip("Redis not reachable — run `docker compose up -d` first")
    yield


@pytest.mark.services_required
async def test_dedup_acquire_blocks_second_claim(redis_or_skip) -> None:
    from datetime import datetime, timezone

    from core.queues.dedup import acquire, attach_workflow_id, release

    sha = "dedup_acq_" + datetime.now(timezone.utc).isoformat().replace(":", "")
    try:
        first = await acquire("/tmp/test", sha)
        assert first.acquired is True
        assert first.existing_value is None

        second = await acquire("/tmp/test", sha)
        assert second.acquired is False
        assert second.existing_value == "PENDING"

        await attach_workflow_id("/tmp/test", sha, 42)
        third = await acquire("/tmp/test", sha)
        assert third.acquired is False
        assert third.existing_value == "42"
    finally:
        await release("/tmp/test", sha)


@pytest.mark.services_required
async def test_dedup_release_allows_reclaim(redis_or_skip) -> None:
    from datetime import datetime, timezone

    from core.queues.dedup import acquire, release

    sha = "dedup_rel_" + datetime.now(timezone.utc).isoformat().replace(":", "")
    first = await acquire("/tmp/test", sha)
    assert first.acquired is True

    await release("/tmp/test", sha)
    second = await acquire("/tmp/test", sha)
    assert second.acquired is True
    await release("/tmp/test", sha)
