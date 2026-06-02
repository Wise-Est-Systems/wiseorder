"""Smoke tests for distribution pipeline v0.2 — Mastodon adapter,
dev.to adapter, cross_post_pipeline, mention_monitor_pipeline.

All tests are pure (no services required). Network calls are mocked.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest  # noqa: F401 -- pytest.raises usage

from workflows.cross_post_pipeline import _event_from_payload, _select_channels
from workflows.distribution.adapters.devto import DevToAdapter, _safe_tags
from workflows.distribution.adapters.mastodon import (
    MastodonAdapter,
    _strip_html,
)
from workflows.distribution.registry import ChannelRegistry
from workflows.distribution.types import (
    AskType,
    ChannelDraft,
    ChannelStatus,
    DistributionEvent,
)
from workflows.mention_monitor_pipeline import (
    MentionHit,
    _approval_from_hit,
    _strip_html as _mm_strip_html,
)


# --- Mastodon adapter -----------------------------------------------------


@pytest.mark.asyncio
async def test_mastodon_draft_truncates_to_500_chars() -> None:
    class _Stub:
        async def draft_mastodon_post(self, *, event_type, payload):  # noqa: ARG002
            return "x" * 800, "https://winstack.dev"

    a = MastodonAdapter(drafter=_Stub())
    event = DistributionEvent(event_type="release", payload={"url": "https://winstack.dev"})
    draft = await a.draft(event)
    assert draft.channel == "mastodon"
    assert draft.title is None  # Mastodon has no title
    assert len(draft.body) <= 500
    assert draft.body.endswith("…")


@pytest.mark.asyncio
async def test_mastodon_submit_returns_failure_when_unconfigured() -> None:
    a = MastodonAdapter(drafter=AsyncMock())
    draft = ChannelDraft(
        channel="mastodon", ask_type=AskType.POST, title=None, body="hello"
    )
    with patch(
        "workflows.distribution.adapters.mastodon.get_settings"
    ) as gs:
        gs.return_value.mastodon_instance_url = ""
        gs.return_value.mastodon_access_token = ""
        result = await a.submit(draft)
    assert result.success is False
    assert "not configured" in (result.error or "")


@pytest.mark.asyncio
async def test_mastodon_submit_rejects_wrong_channel() -> None:
    a = MastodonAdapter(drafter=AsyncMock())
    draft = ChannelDraft(
        channel="hacker_news", ask_type=AskType.POST, title="x", body="y"
    )
    result = await a.submit(draft)
    assert result.success is False
    assert "draft.channel mismatch" in (result.error or "")


def test_mastodon_strip_html_removes_tags() -> None:
    assert _strip_html("<p>hello <a href='x'>world</a></p>") == "hello world"
    assert _strip_html("plain") == "plain"
    assert _strip_html("") == ""


# --- dev.to adapter -------------------------------------------------------


def test_devto_safe_tags_enforces_constraints() -> None:
    out = _safe_tags(["AI Safety", "rust!", "ai safety", "wasm", "extra5", "extra6"])
    assert all(t.isalnum() for t in out)
    assert out == ["aisafety", "rust", "wasm", "extra5"]  # dedup + max 4


def test_devto_safe_tags_handles_non_list_input() -> None:
    assert _safe_tags("not a list") == []  # type: ignore[arg-type]
    assert _safe_tags(None) == []  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_devto_draft_propagates_canonical_url() -> None:
    class _Stub:
        async def draft_blog_post(self, *, event_type, payload):  # noqa: ARG002
            return ("Title X", "# Body\n\nbody.", "https://winstack.dev")

    a = DevToAdapter(drafter=_Stub())
    event = DistributionEvent(event_type="release", payload={"url": "https://winstack.dev"})
    draft = await a.draft(event)
    assert draft.title == "Title X"
    assert draft.body.startswith("# Body")
    assert draft.metadata["canonical_url"] == "https://winstack.dev"


@pytest.mark.asyncio
async def test_devto_submit_returns_failure_when_unconfigured() -> None:
    a = DevToAdapter(drafter=AsyncMock())
    draft = ChannelDraft(
        channel="devto", ask_type=AskType.POST, title="t", body="b", url="https://x"
    )
    with patch(
        "workflows.distribution.adapters.devto.get_settings"
    ) as gs:
        gs.return_value.devto_api_key = ""
        result = await a.submit(draft)
    assert result.success is False
    assert "not configured" in (result.error or "")


@pytest.mark.asyncio
async def test_devto_submit_rejects_missing_title() -> None:
    a = DevToAdapter(drafter=AsyncMock())
    draft = ChannelDraft(
        channel="devto", ask_type=AskType.POST, title=None, body="b", url="https://x"
    )
    with patch(
        "workflows.distribution.adapters.devto.get_settings"
    ) as gs:
        gs.return_value.devto_api_key = "fake-key"
        result = await a.submit(draft)
    assert result.success is False
    assert "title" in (result.error or "").lower()


# --- cross_post_pipeline helpers -----------------------------------------


def _stub_adapter(name: str, default_status: ChannelStatus = ChannelStatus.READY):
    from workflows.distribution.adapters.base import ChannelAdapter
    from workflows.distribution.types import SubmissionResult

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


def test_cross_post_event_from_payload_minimal() -> None:
    e = _event_from_payload(
        {"event_type": "release", "payload": {"url": "https://x"}}
    )
    assert e.event_type == "release"


def test_cross_post_event_missing_keys_raises() -> None:
    with pytest.raises(ValueError, match="missing required keys"):
        _event_from_payload({"event_type": "x"})


def test_cross_post_select_channels_fans_out_to_all_ready() -> None:
    reg = ChannelRegistry()
    reg.register(_stub_adapter("hacker_news"))
    reg.register(_stub_adapter("mastodon"))
    reg.register(_stub_adapter("devto"))
    reg.register(_stub_adapter("email_outreach"))
    reg.register(_stub_adapter("degraded_one", default_status=ChannelStatus.DEGRADED))
    e = DistributionEvent(event_type="release", payload={})
    out = _select_channels(event=e, registry=reg)
    assert sorted(out) == sorted(["hacker_news", "mastodon", "devto", "email_outreach"])
    assert "degraded_one" not in out


def test_cross_post_select_channels_respects_target_list() -> None:
    reg = ChannelRegistry()
    reg.register(_stub_adapter("mastodon"))
    reg.register(_stub_adapter("devto"))
    reg.register(_stub_adapter("hacker_news"))
    e = DistributionEvent(
        event_type="release",
        payload={},
        target_channels=["mastodon", "missing"],
    )
    out = _select_channels(event=e, registry=reg)
    assert out == ["mastodon"]


# --- mention_monitor helpers ---------------------------------------------


def test_mention_hit_to_dict_round_trip() -> None:
    h = MentionHit(
        source="hn",
        keyword="winstack",
        external_id="42",
        external_url="https://news.ycombinator.com/item?id=42",
        author="alice",
        title="Show HN: x",
        body="hello",
    )
    out = h.to_dict()
    assert out["source"] == "hn"
    assert out["keyword"] == "winstack"
    assert out["external_id"] == "42"


def test_mention_approval_from_hit_constructs_well() -> None:
    h = MentionHit(
        source="reddit",
        keyword="WISEATA",
        external_id="abc",
        external_url="https://reddit.com/r/x/comments/abc/",
        author="bob",
        title="Anyone tried WISEATA?",
        body="seems interesting",
    )
    req = _approval_from_hit(hit=h, workflow_id=10, task_id=20)
    assert req.workflow_id == 10
    assert req.task_id == 20
    assert "[mention:reddit]" in req.summary
    assert "WISEATA" in req.summary
    assert "url: https://reddit.com/r/x/comments/abc/" in req.affected
    assert req.risk_level == "low"


def test_mention_monitor_strip_html() -> None:
    # Mastodon returns HTML; both adapters use the same helper shape
    assert _mm_strip_html("<p>hi</p>") == "hi"
    assert _mm_strip_html("plain") == "plain"


@pytest.mark.asyncio
async def test_search_hn_parses_algolia_shape() -> None:
    from workflows.mention_monitor_pipeline import _search_hn

    sample = httpx.Response(
        200,
        json={
            "hits": [
                {
                    "objectID": "99",
                    "author": "carol",
                    "title": "Show HN: x",
                    "url": "https://example.com/x",
                    "story_text": "body here",
                    "created_at": "2026-06-01T00:00:00Z",
                }
            ]
        },
        request=httpx.Request(
            "GET", "https://hn.algolia.com/api/v1/search"
        ),
    )

    async def fake_get(self, url, *, params=None, **kwargs):  # noqa: ARG001
        assert "hn.algolia.com" in url
        assert params["query"] == "winstack"
        return sample

    with patch.object(httpx.AsyncClient, "get", new=fake_get):
        hits = await _search_hn("winstack")
    assert len(hits) == 1
    assert hits[0].source == "hn"
    assert hits[0].external_id == "99"
    assert hits[0].external_url == "https://example.com/x"


@pytest.mark.asyncio
async def test_search_reddit_parses_search_shape() -> None:
    from workflows.mention_monitor_pipeline import _search_reddit

    sample = httpx.Response(
        200,
        json={
            "data": {
                "children": [
                    {
                        "data": {
                            "id": "xyz",
                            "author": "dave",
                            "title": "Cool project",
                            "permalink": "/r/programming/comments/xyz/cool_project/",
                            "selftext": "look at this",
                            "created_utc": 1717200000,
                        }
                    }
                ]
            }
        },
        request=httpx.Request("GET", "https://www.reddit.com/search.json"),
    )

    async def fake_get(self, url, *, params=None, headers=None, **kwargs):  # noqa: ARG001
        assert "reddit.com" in url
        assert params["q"] == "WISEATA"
        return sample

    with patch.object(httpx.AsyncClient, "get", new=fake_get):
        hits = await _search_reddit("WISEATA")
    assert len(hits) == 1
    assert hits[0].source == "reddit"
    assert (
        hits[0].external_url
        == "https://www.reddit.com/r/programming/comments/xyz/cool_project/"
    )
