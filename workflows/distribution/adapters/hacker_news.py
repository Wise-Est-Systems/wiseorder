from __future__ import annotations

import asyncio
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

_HN_ALGOLIA_SEARCH = "https://hn.algolia.com/api/v1/search"


class HackerNewsAdapter(ChannelAdapter):
    """Hacker News adapter.

    HN has no submission API. submit() uses Playwright to drive the form;
    monitor() uses the public Algolia search API to fetch comments by
    submission URL.

    draft() produces a Show HN-shaped title (<=80 chars, leading 'Show HN: ')
    and a first comment (<=2000 chars) intended to be posted into the thread
    immediately after the submission.
    """

    channel_name = "hacker_news"
    status = ChannelStatus.READY

    SHOW_HN_PREFIX = "Show HN: "
    MAX_TITLE_LEN = 80
    MAX_FIRST_COMMENT_LEN = 2000

    def __init__(self, drafter: Any | None = None) -> None:
        if drafter is None:
            from agents.outreach.drafter import OutreachDrafter
            drafter = OutreachDrafter()
        self._drafter = drafter

    async def draft(self, event: DistributionEvent) -> ChannelDraft:
        title, first_comment, url = await self._drafter.draft_hn_post(
            event_type=event.event_type,
            payload=event.payload,
        )
        if not title.startswith(self.SHOW_HN_PREFIX):
            title = self.SHOW_HN_PREFIX + title
        if len(title) > self.MAX_TITLE_LEN:
            title = title[: self.MAX_TITLE_LEN]
        if len(first_comment) > self.MAX_FIRST_COMMENT_LEN:
            first_comment = first_comment[: self.MAX_FIRST_COMMENT_LEN]
        return ChannelDraft(
            channel=self.channel_name,
            ask_type=AskType.POST,
            title=title,
            body=first_comment,
            url=url,
            metadata={"event_type": event.event_type},
        )

    async def submit(self, draft: ChannelDraft) -> SubmissionResult:
        """Drive the HN submit form via Playwright, then post the first comment.

        Playwright is heavy; this method is async-wrapped sync to keep the
        rest of the runtime non-blocking. Returns success=False with a clear
        error if Playwright isn't installed or credentials are missing.
        """
        if draft.channel != self.channel_name:
            return SubmissionResult(
                channel=self.channel_name,
                success=False,
                error=f"draft.channel mismatch: got {draft.channel!r}",
            )
        if not draft.title or not draft.url:
            return SubmissionResult(
                channel=self.channel_name,
                success=False,
                error="HN submit requires both title and url",
            )
        s = get_settings()
        if not (s.hn_username and s.hn_password):
            return SubmissionResult(
                channel=self.channel_name,
                success=False,
                error="HN_USERNAME / HN_PASSWORD not configured",
            )
        try:
            external_id, external_url = await asyncio.to_thread(
                _submit_via_playwright,
                username=s.hn_username,
                password=s.hn_password,
                title=draft.title,
                url=draft.url,
                first_comment=draft.body,
                headless=s.hn_playwright_headless,
            )
        except Exception as exc:
            log.error({"msg": "hn_submit_failed", "err": str(exc)})
            return SubmissionResult(
                channel=self.channel_name,
                success=False,
                error=f"Playwright submit error: {exc!r}",
            )

        log.info(
            {
                "msg": "hn_submitted",
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
        """Poll HN's public Algolia API for comments on the submission.

        external_submission_id is the HN item id (numeric, as a string).
        """
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(
                    _HN_ALGOLIA_SEARCH,
                    params={
                        "tags": f"comment,story_{external_submission_id}",
                        "hitsPerPage": 100,
                    },
                )
                r.raise_for_status()
                data = r.json()
        except Exception as exc:
            log.error({"msg": "hn_monitor_fetch_failed", "err": str(exc)})
            return []

        hits = data.get("hits") or []
        replies: list[ReplyEvent] = []
        for hit in hits:
            reply_id = str(hit.get("objectID") or "")
            if not reply_id:
                continue
            replies.append(
                ReplyEvent(
                    channel=self.channel_name,
                    external_submission_id=external_submission_id,
                    reply_id=reply_id,
                    author=hit.get("author"),
                    body=(hit.get("comment_text") or "").strip(),
                )
            )
        return replies


def _submit_via_playwright(
    *,
    username: str,
    password: str,
    title: str,
    url: str,
    first_comment: str,
    headless: bool,
) -> tuple[str, str]:
    """Synchronous Playwright HN submit flow.

    Returns (item_id, item_url). Raises on failure; the caller wraps in
    asyncio.to_thread and converts exceptions to SubmissionResult errors.
    """
    # Local import so the rest of the runtime does not require Playwright.
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            page = browser.new_page()
            page.goto("https://news.ycombinator.com/login", wait_until="networkidle")
            page.fill("input[name='acct']", username)
            page.fill("input[name='pw']", password)
            page.click("input[type='submit']")
            page.wait_for_load_state("networkidle")
            if "logout" not in page.content().lower():
                raise RuntimeError("HN login appears to have failed")

            page.goto("https://news.ycombinator.com/submit", wait_until="networkidle")
            page.fill("input[name='title']", title)
            page.fill("input[name='url']", url)
            page.click("input[type='submit']")
            page.wait_for_load_state("networkidle")

            current = page.url
            if "/item?id=" not in current and "/newest" not in current:
                raise RuntimeError(f"HN submit landed on unexpected URL: {current}")

            if "/item?id=" not in current:
                # If HN redirected to /newest, find our submission by title.
                links = page.locator(f"a:has-text({title!r})")
                if links.count() == 0:
                    raise RuntimeError("submission did not appear on /newest")
                links.first.click()
                page.wait_for_load_state("networkidle")
                current = page.url

            item_id = current.rsplit("=", 1)[-1]

            # Post the first comment on the submission thread.
            page.fill("textarea[name='text']", first_comment)
            page.click("input[type='submit'][value='add comment']")
            page.wait_for_load_state("networkidle")

            return item_id, f"https://news.ycombinator.com/item?id={item_id}"
        finally:
            browser.close()
