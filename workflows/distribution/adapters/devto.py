from __future__ import annotations

from typing import Any

import httpx

from configs.logging import get_logger
from configs.settings import get_settings
from workflows.distribution.adapters.base import ChannelAdapter
from workflows.distribution.types import (
    AskType,
    ChannelDraft,
    ChannelStatus,
    DistributionEvent,
    ReplyEvent,
    SubmissionResult,
)


log = get_logger(__name__)

_DEVTO_API = "https://dev.to/api"


class DevToAdapter(ChannelAdapter):
    """dev.to adapter — long-form engineer-blog cross-publishing.

    draft()    — generates a title + body (Markdown), with `canonical_url`
                 pointing at the source repo so dev.to does not steal SEO
                 ranking from the canonical artifact.
    submit()   — POST /articles with API-Key header; creates a published
                 article and returns its id + URL.
    monitor()  — GET /articles/{id}/comments to fetch new comments.
    """

    channel_name = "devto"
    status = ChannelStatus.READY

    def __init__(self, drafter: Any | None = None) -> None:
        if drafter is None:
            from agents.outreach.drafter import OutreachDrafter
            drafter = OutreachDrafter()
        self._drafter = drafter

    async def draft(self, event: DistributionEvent) -> ChannelDraft:
        title, body_markdown, canonical_url = await self._drafter.draft_blog_post(
            event_type=event.event_type,
            payload=event.payload,
        )
        return ChannelDraft(
            channel=self.channel_name,
            ask_type=AskType.POST,
            title=title,
            body=body_markdown,
            url=canonical_url,
            metadata={
                "event_type": event.event_type,
                "canonical_url": canonical_url,
                "format": "markdown",
            },
        )

    async def submit(self, draft: ChannelDraft) -> SubmissionResult:
        if draft.channel != self.channel_name:
            return SubmissionResult(
                channel=self.channel_name,
                success=False,
                error=f"draft.channel mismatch: got {draft.channel!r}",
            )
        s = get_settings()
        if not s.devto_api_key:
            return SubmissionResult(
                channel=self.channel_name,
                success=False,
                error="WISEORDER_DEVTO_API_KEY not configured",
            )
        if not draft.title:
            return SubmissionResult(
                channel=self.channel_name,
                success=False,
                error="dev.to article requires a title",
            )
        body = {
            "article": {
                "title": draft.title,
                "body_markdown": draft.body,
                "published": True,
                "canonical_url": draft.url or None,
                "tags": _safe_tags(draft.metadata.get("tags", [])),
            }
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(
                    f"{_DEVTO_API}/articles",
                    headers={"api-key": s.devto_api_key, "Content-Type": "application/json"},
                    json=body,
                )
                r.raise_for_status()
                data = r.json()
        except Exception as exc:
            log.error({"msg": "devto_submit_failed", "err": str(exc)})
            return SubmissionResult(
                channel=self.channel_name,
                success=False,
                error=f"dev.to API error: {exc!r}",
            )

        external_id = str(data.get("id") or "")
        external_url = str(data.get("url") or data.get("canonical_url") or "")
        log.info(
            {
                "msg": "devto_submitted",
                "external_id": external_id,
                "external_url": external_url,
            }
        )
        return SubmissionResult(
            channel=self.channel_name,
            success=True,
            external_id=external_id,
            external_url=external_url,
        )

    async def monitor(self, external_submission_id: str) -> list[ReplyEvent]:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(
                    f"{_DEVTO_API}/comments",
                    params={"a_id": external_submission_id},
                )
                r.raise_for_status()
                comments = r.json() or []
        except Exception as exc:
            log.error({"msg": "devto_monitor_failed", "err": str(exc)})
            return []

        replies: list[ReplyEvent] = []
        for c in comments:
            user = c.get("user") or {}
            replies.append(
                ReplyEvent(
                    channel=self.channel_name,
                    external_submission_id=external_submission_id,
                    reply_id=str(c.get("id_code") or c.get("id") or ""),
                    author=user.get("username"),
                    body=(c.get("body_html") or c.get("body_markdown") or "").strip(),
                )
            )
        return replies


def _safe_tags(raw) -> list[str]:
    """dev.to enforces: tags are lowercase, alphanumeric, max 4 tags per article."""
    if not isinstance(raw, list):
        return []
    cleaned = []
    for t in raw:
        if not isinstance(t, str):
            continue
        norm = "".join(ch for ch in t.lower() if ch.isalnum())
        if norm and norm not in cleaned:
            cleaned.append(norm)
        if len(cleaned) >= 4:
            break
    return cleaned
