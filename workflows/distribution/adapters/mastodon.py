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

_MAX_TOOT_LEN = 500  # default Mastodon character limit; some instances allow more


class MastodonAdapter(ChannelAdapter):
    """Mastodon adapter.

    draft()    — generates a toot under MAX_TOOT_LEN.
    submit()   — POST /api/v1/statuses with bearer token; returns toot id + URL.
    monitor()  — GET /api/v1/notifications, filters to mentions / replies on
                 the submitted status id.
    """

    channel_name = "mastodon"
    status = ChannelStatus.READY

    def __init__(self, drafter: Any | None = None) -> None:
        if drafter is None:
            from agents.outreach.drafter import OutreachDrafter
            drafter = OutreachDrafter()
        self._drafter = drafter

    async def draft(self, event: DistributionEvent) -> ChannelDraft:
        body, url = await self._drafter.draft_mastodon_post(
            event_type=event.event_type,
            payload=event.payload,
        )
        if len(body) > _MAX_TOOT_LEN:
            body = body[: _MAX_TOOT_LEN - 1].rstrip() + "…"
        return ChannelDraft(
            channel=self.channel_name,
            ask_type=AskType.POST,
            title=None,  # Mastodon has no title field
            body=body,
            url=url,
            metadata={"event_type": event.event_type},
        )

    async def submit(self, draft: ChannelDraft) -> SubmissionResult:
        if draft.channel != self.channel_name:
            return SubmissionResult(
                channel=self.channel_name,
                success=False,
                error=f"draft.channel mismatch: got {draft.channel!r}",
            )
        s = get_settings()
        if not (s.mastodon_instance_url and s.mastodon_access_token):
            return SubmissionResult(
                channel=self.channel_name,
                success=False,
                error="WISEORDER_MASTODON_INSTANCE_URL / WISEORDER_MASTODON_ACCESS_TOKEN not configured",
            )
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(
                    f"{s.mastodon_instance_url.rstrip('/')}/api/v1/statuses",
                    headers={"Authorization": f"Bearer {s.mastodon_access_token}"},
                    data={"status": draft.body, "visibility": s.mastodon_default_visibility},
                )
                r.raise_for_status()
                data = r.json()
        except Exception as exc:
            log.error({"msg": "mastodon_submit_failed", "err": str(exc)})
            return SubmissionResult(
                channel=self.channel_name,
                success=False,
                error=f"Mastodon API error: {exc!r}",
            )

        external_id = str(data.get("id") or "")
        external_url = str(data.get("url") or data.get("uri") or "")
        log.info(
            {
                "msg": "mastodon_submitted",
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
        s = get_settings()
        if not (s.mastodon_instance_url and s.mastodon_access_token):
            return []
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(
                    f"{s.mastodon_instance_url.rstrip('/')}/api/v1/notifications",
                    headers={"Authorization": f"Bearer {s.mastodon_access_token}"},
                    params={"types[]": ["mention"], "limit": 80},
                )
                r.raise_for_status()
                notifications = r.json() or []
        except Exception as exc:
            log.error({"msg": "mastodon_monitor_failed", "err": str(exc)})
            return []

        replies: list[ReplyEvent] = []
        for n in notifications:
            status = n.get("status") or {}
            in_reply_to = status.get("in_reply_to_id")
            # Surface mentions whose status replies to (or is) our submission.
            if in_reply_to != external_submission_id and status.get("id") != external_submission_id:
                continue
            account = n.get("account") or {}
            replies.append(
                ReplyEvent(
                    channel=self.channel_name,
                    external_submission_id=external_submission_id,
                    reply_id=str(status.get("id") or n.get("id")),
                    author=account.get("acct"),
                    body=_strip_html(status.get("content") or ""),
                )
            )
        return replies


def _strip_html(s: str) -> str:
    """Mastodon returns toot content as HTML. Strip tags for plain-text logging."""
    out: list[str] = []
    in_tag = False
    for ch in s:
        if ch == "<":
            in_tag = True
            continue
        if ch == ">":
            in_tag = False
            continue
        if not in_tag:
            out.append(ch)
    return "".join(out).strip()
