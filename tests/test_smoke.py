"""Smoke tests — no external services required.

These verify imports, dataclass behavior, prompt rendering, and JSON parsers.
Service-dependent tests live in test_workflow.py.
"""

from __future__ import annotations

import json

import pytest

from agents.engineering.summarizer import _normalize_risk, _parse_json
from configs.settings import get_settings
from core.queues.redis_queue import Job, QueueName


def test_settings_load() -> None:
    s = get_settings()
    assert s.database_url.startswith("postgresql+asyncpg://")
    assert s.redis_url.startswith("redis://")
    assert s.llm_model


def test_queue_name_values() -> None:
    assert QueueName.HIGH.value == "wiseorder:queue:high_priority"
    assert QueueName.NORMAL.value == "wiseorder:queue:normal_priority"
    assert QueueName.FAILED.value == "wiseorder:queue:failed_jobs"


def test_job_roundtrip() -> None:
    j = Job.new(type="commit_pipeline", payload={"sha": "abc", "diff": "x"})
    blob = j.to_json()
    j2 = Job.from_json(blob)
    assert j.id == j2.id
    assert j.type == j2.type
    assert j.payload == j2.payload


def test_summary_json_parse_clean() -> None:
    data = _parse_json('{"summary":"x","changed_files":["a"],"changelog":"y","risk_level":"low"}')
    assert data["summary"] == "x"
    assert data["risk_level"] == "low"


def test_summary_json_parse_fenced() -> None:
    raw = "Here you go:\n```json\n{\"summary\":\"x\",\"risk_level\":\"high\"}\n```"
    data = _parse_json(raw)
    assert data["summary"] == "x"
    assert data["risk_level"] == "high"


def test_summary_json_parse_falls_back() -> None:
    data = _parse_json("totally not json")
    assert "summary" in data
    assert data["risk_level"] == "low"


def test_normalize_risk() -> None:
    assert _normalize_risk("HIGH") == "high"
    assert _normalize_risk("garbage") == "low"
    assert _normalize_risk("medium") == "medium"


def test_imports_resolve() -> None:
    """All major modules import without side effects beyond logger setup."""
    import api.server  # noqa: F401
    import core.approvals.gateway  # noqa: F401
    import core.events.watcher  # noqa: F401
    import core.memory.db  # noqa: F401
    import core.memory.models  # noqa: F401
    import core.memory.vector  # noqa: F401
    import core.orchestrator.main  # noqa: F401
    import core.queues.redis_queue  # noqa: F401
    import workflows.commit_pipeline  # noqa: F401


def test_prompt_files_exist() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    assert (root / "prompts" / "engineering_summary.md").exists()
    assert (root / "prompts" / "social_post.md").exists()
