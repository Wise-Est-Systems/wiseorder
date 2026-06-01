"""Distribution pipeline smoke tests — pure, no services required.

Covers:
- Registry round-trip + duplicate-registration error
- ChannelAdapter ABC contract
- ChannelDraft / DistributionEvent dataclass round-trips
- HN draft formatter applies Show HN prefix and length caps
- HN monitor uses the documented Algolia endpoint shape (mocked HTTP)
- Email draft applies the recipient-required check
- distribution_pipeline._classify_channels picks defaults per ask_type
- _approval_request_from_draft populates the ApprovalRequest correctly
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest  # noqa: F401  -- pytest.raises usage

from workflows.distribution.adapters.base import ChannelAdapter
from workflows.distribution.adapters.email_outreach import EmailOutreachAdapter
from workflows.distribution.adapters.hacker_news import HackerNewsAdapter
from workflows.distribution.registry import ChannelRegistry, reset_registry
from workflows.distribution.types import (
    AskType,
    ChannelDraft,
    ChannelStatus,
    DistributionEvent,
    ReplyEvent,
    SubmissionResult,
)
from workflows.distribution_pipeline import (
    _approval_request_from_draft,
    _classify_channels,
    _event_from_payload,
)


# --- types ----------------------------------------------------------------


def test_distribution_event_minimal() -> None:
    e = DistributionEvent(event_type="release_published", payload={"url": "https://x"})
    assert e.event_type == "release_published"
    assert e.target_channels == []
    assert e.ask_type == AskType.POST


def test_channel_draft_to_dict_round_trip() -> None:
    d = ChannelDraft(
        channel="hacker_news",
        ask_type=AskType.POST,
        title="Show HN: x",
        body="hello",
        url="https://x",
    )
    out = d.to_dict()
    assert out["channel"] == "hacker_news"
    assert out["ask_type"] == "post"
    assert out["title"] == "Show HN: x"


def test_submission_result_to_dict() -> None:
    r = SubmissionResult(channel="email_outreach", success=True, external_id="<id@a>")
    out = r.to_dict()
    assert out["success"] is True
    assert out["external_id"] == "<id@a>"


def test_reply_event_to_dict() -> None:
    r = ReplyEvent(
        channel="hacker_news",
        external_submission_id="123",
        reply_id="456",
        author="someone",
        body="hi",
    )
    out = r.to_dict()
    assert out["author"] == "someone"


# --- registry -------------------------------------------------------------


def _stub_adapter(
    name: str, default_status: ChannelStatus = ChannelStatus.READY
) -> ChannelAdapter:
    class _A(ChannelAdapter):
        channel_name = name
        status = default_status

        async def draft(self, event):  # noqa: ARG002
            return ChannelDraft(channel=name, ask_type=AskType.POST, title="t", body="b")

        async def submit(self, draft):  # noqa: ARG002
            return SubmissionResult(channel=name, success=True, external_id="x")

        async def monitor(self, external_submission_id):  # noqa: ARG002
            return []

    return _A()


def test_registry_round_trip() -> None:
    reg = ChannelRegistry()
    a = _stub_adapter("foo")
    reg.register(a)
    assert reg.get("foo") is a
    assert reg.names() == ["foo"]
    assert reg.ready_names() == ["foo"]


def test_registry_duplicate_registration_raises() -> None:
    reg = ChannelRegistry()
    reg.register(_stub_adapter("foo"))
    with pytest.raises(ValueError, match="already registered"):
        reg.register(_stub_adapter("foo"))


def test_registry_unknown_channel_raises() -> None:
    reg = ChannelRegistry()
    reg.register(_stub_adapter("foo"))
    with pytest.raises(KeyError, match="no channel adapter registered"):
        reg.get("does-not-exist")


def test_registry_degraded_separated_from_ready() -> None:
    reg = ChannelRegistry()
    reg.register(_stub_adapter("ok"))
    reg.register(_stub_adapter("sick", default_status=ChannelStatus.DEGRADED))
    assert reg.ready_names() == ["ok"]
    assert reg.degraded_names() == ["sick"]


# --- ABC contract ---------------------------------------------------------


def test_channel_adapter_missing_method_raises() -> None:
    class _Bad(ChannelAdapter):
        channel_name = "bad"
        status = ChannelStatus.READY

        async def draft(self, event):  # noqa: ARG002
            return ChannelDraft(channel="bad", ask_type=AskType.POST, title="t", body="b")

        # submit() and monitor() missing

    with pytest.raises(TypeError):
        _Bad()  # type: ignore[abstract]


# --- HN draft formatter ---------------------------------------------------


@pytest.mark.asyncio
async def test_hn_draft_applies_show_hn_prefix_and_length_caps() -> None:
    class StubDrafter:
        async def draft_hn_post(self, *, event_type, payload):  # noqa: ARG002
            long_title = "no prefix " + ("x" * 200)
            long_body = "y" * 5000
            return long_title, long_body, "https://winstack.dev"

    adapter = HackerNewsAdapter(drafter=StubDrafter())
    event = DistributionEvent(event_type="release", payload={"url": "https://winstack.dev"})
    draft = await adapter.draft(event)
    assert draft.title is not None
    assert draft.title.startswith("Show HN: ")
    assert len(draft.title) <= HackerNewsAdapter.MAX_TITLE_LEN
    assert len(draft.body) <= HackerNewsAdapter.MAX_FIRST_COMMENT_LEN
    assert draft.url == "https://winstack.dev"


@pytest.mark.asyncio
async def test_hn_monitor_uses_algolia_endpoint() -> None:
    sample_response = httpx.Response(
        200,
        json={
            "hits": [
                {
                    "objectID": "999",
                    "author": "alice",
                    "comment_text": "great work",
                }
            ]
        },
        request=httpx.Request("GET", "https://hn.algolia.com/api/v1/search"),
    )

    async def fake_get(self, url, *, params=None, **kwargs):  # noqa: ARG001
        assert url == "https://hn.algolia.com/api/v1/search"
        assert params["tags"] == "comment,story_42"
        return sample_response

    with patch.object(httpx.AsyncClient, "get", new=fake_get):
        adapter = HackerNewsAdapter(drafter=object())
        replies = await adapter.monitor("42")

    assert len(replies) == 1
    assert replies[0].reply_id == "999"
    assert replies[0].author == "alice"
    assert replies[0].body == "great work"


# --- Email adapter --------------------------------------------------------


@pytest.mark.asyncio
async def test_email_draft_requires_recipient() -> None:
    adapter = EmailOutreachAdapter(drafter=AsyncMock())
    event = DistributionEvent(
        event_type="release", payload={}, ask_type=AskType.EMAIL, recipient=None
    )
    with pytest.raises(ValueError, match="recipient"):
        await adapter.draft(event)


@pytest.mark.asyncio
async def test_email_draft_propagates_recipient_and_subject() -> None:
    drafter = AsyncMock()
    drafter.draft_email = AsyncMock(return_value=("Subject X", "Body Y"))
    adapter = EmailOutreachAdapter(drafter=drafter)
    event = DistributionEvent(
        event_type="release",
        payload={"context": "demo"},
        ask_type=AskType.EMAIL,
        recipient="someone@example.com",
    )
    draft = await adapter.draft(event)
    assert draft.recipient == "someone@example.com"
    assert draft.title == "Subject X"
    assert draft.body == "Body Y"


# --- distribution_pipeline helpers ---------------------------------------


def test_event_from_payload_minimal() -> None:
    e = _event_from_payload(
        {"event_type": "release_published", "payload": {"url": "https://x"}}
    )
    assert e.event_type == "release_published"
    assert e.ask_type == AskType.POST


def test_event_from_payload_missing_keys() -> None:
    with pytest.raises(ValueError, match="missing required keys"):
        _event_from_payload({"event_type": "release_published"})


def test_event_from_payload_invalid_ask_type() -> None:
    with pytest.raises(ValueError, match="unknown ask_type"):
        _event_from_payload(
            {"event_type": "x", "payload": {}, "ask_type": "telegraph"}
        )


def test_classify_channels_uses_targets_when_set() -> None:
    reset_registry()
    reg = ChannelRegistry()
    reg.register(_stub_adapter("a"))
    reg.register(_stub_adapter("b"))
    e = DistributionEvent(
        event_type="x",
        payload={},
        target_channels=["a", "missing"],
    )
    out = _classify_channels(event=e, registry=reg)
    assert out == ["a"]


def test_classify_channels_defaults_to_hacker_news_for_post() -> None:
    reg = ChannelRegistry()
    reg.register(_stub_adapter("hacker_news"))
    reg.register(_stub_adapter("email_outreach"))
    e = DistributionEvent(event_type="x", payload={}, ask_type=AskType.POST)
    assert _classify_channels(event=e, registry=reg) == ["hacker_news"]


def test_classify_channels_defaults_to_email_outreach_for_email() -> None:
    reg = ChannelRegistry()
    reg.register(_stub_adapter("hacker_news"))
    reg.register(_stub_adapter("email_outreach"))
    e = DistributionEvent(event_type="x", payload={}, ask_type=AskType.EMAIL)
    assert _classify_channels(event=e, registry=reg) == ["email_outreach"]


def test_approval_request_from_draft_post() -> None:
    d = ChannelDraft(
        channel="hacker_news",
        ask_type=AskType.POST,
        title="Show HN: x",
        body="hello",
        url="https://winstack.dev",
    )
    req = _approval_request_from_draft(draft=d, workflow_id=1, task_id=2)
    assert req.summary.startswith("[hacker_news]")
    assert req.workflow_id == 1
    assert req.task_id == 2
    assert "url: https://winstack.dev" in req.affected
    assert req.risk_level == "medium"


def test_approval_request_from_draft_email() -> None:
    d = ChannelDraft(
        channel="email_outreach",
        ask_type=AskType.EMAIL,
        title="Subject X",
        body="Body Y",
        recipient="someone@example.com",
    )
    req = _approval_request_from_draft(draft=d, workflow_id=3, task_id=4)
    assert "recipient: someone@example.com" in req.affected
