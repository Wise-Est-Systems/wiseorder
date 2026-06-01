from __future__ import annotations

import email
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import make_msgid
from imaplib import IMAP4_SSL
from typing import Any

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


class EmailOutreachAdapter(ChannelAdapter):
    """Cold-reach 1:1 email adapter.

    draft()    — LLM-generates a personalised body from the event payload
                 + the named recipient. Body is short, professional, lowkey
                 (matches the credibility-filter rule).
    submit()   — sends via SMTPS using settings configured in
                 OUTREACH_SMTP_* env vars. Returns the Message-Id as
                 external_id.
    monitor()  — polls IMAP for messages with In-Reply-To matching the
                 submitted Message-Id; surfaces each as a ReplyEvent.
    """

    channel_name = "email_outreach"
    status = ChannelStatus.READY

    def __init__(self, drafter: Any | None = None) -> None:
        # Inject the drafter so tests can stub it.
        if drafter is None:
            from agents.outreach.drafter import OutreachDrafter
            drafter = OutreachDrafter()
        self._drafter = drafter

    async def draft(self, event: DistributionEvent) -> ChannelDraft:
        if event.recipient is None:
            raise ValueError("email_outreach.draft: event.recipient is required")
        title, body = await self._drafter.draft_email(
            recipient=event.recipient,
            event_type=event.event_type,
            payload=event.payload,
        )
        return ChannelDraft(
            channel=self.channel_name,
            ask_type=AskType.EMAIL,
            title=title,
            body=body,
            recipient=event.recipient,
            metadata={"event_type": event.event_type},
        )

    async def submit(self, draft: ChannelDraft) -> SubmissionResult:
        if draft.channel != self.channel_name:
            return SubmissionResult(
                channel=self.channel_name,
                success=False,
                error=f"draft.channel mismatch: got {draft.channel!r}",
            )
        if draft.recipient is None:
            return SubmissionResult(
                channel=self.channel_name,
                success=False,
                error="draft.recipient is required",
            )
        s = get_settings()
        if not (
            s.outreach_smtp_host
            and s.outreach_smtp_username
            and s.outreach_smtp_password
            and s.outreach_from_address
        ):
            return SubmissionResult(
                channel=self.channel_name,
                success=False,
                error="OUTREACH_SMTP_* settings not fully configured",
            )

        msg = EmailMessage()
        msg["Subject"] = draft.title or "(no subject)"
        msg["From"] = s.outreach_from_address
        msg["To"] = draft.recipient
        message_id = make_msgid(domain=s.outreach_from_address.split("@", 1)[-1])
        msg["Message-Id"] = message_id
        msg["Date"] = email.utils.format_datetime(datetime.now(timezone.utc))
        msg.set_content(draft.body)

        try:
            with smtplib.SMTP_SSL(s.outreach_smtp_host, s.outreach_smtp_port, timeout=30) as srv:
                srv.login(s.outreach_smtp_username, s.outreach_smtp_password)
                srv.send_message(msg)
        except Exception as exc:  # SMTP-level errors are returned, not raised
            log.error({"msg": "email_outreach_submit_failed", "err": str(exc)})
            return SubmissionResult(
                channel=self.channel_name,
                success=False,
                error=f"SMTP error: {exc!r}",
            )

        log.info(
            {
                "msg": "email_outreach_submitted",
                "to": draft.recipient,
                "message_id": message_id,
            }
        )
        return SubmissionResult(
            channel=self.channel_name,
            success=True,
            external_id=message_id,
            external_url=None,
        )

    async def monitor(self, external_submission_id: str) -> list[ReplyEvent]:
        s = get_settings()
        if not (
            s.outreach_imap_host
            and s.outreach_smtp_username
            and s.outreach_smtp_password
        ):
            return []

        replies: list[ReplyEvent] = []
        try:
            with IMAP4_SSL(s.outreach_imap_host, s.outreach_imap_port) as imap:
                imap.login(s.outreach_smtp_username, s.outreach_smtp_password)
                imap.select("INBOX")
                # IMAP HEADER search on In-Reply-To
                status, data = imap.search(
                    None, f'HEADER In-Reply-To "{external_submission_id}"'
                )
                if status != "OK" or not data or not data[0]:
                    return []
                for num in data[0].split():
                    status, fetched = imap.fetch(num, "(RFC822)")
                    if status != "OK" or not fetched:
                        continue
                    raw = fetched[0][1] if isinstance(fetched[0], tuple) else None
                    if not raw:
                        continue
                    parsed = email.message_from_bytes(raw)
                    reply_id = parsed.get("Message-Id") or num.decode()
                    author = parsed.get("From")
                    body = _extract_plain_text(parsed)
                    replies.append(
                        ReplyEvent(
                            channel=self.channel_name,
                            external_submission_id=external_submission_id,
                            reply_id=reply_id,
                            author=author,
                            body=body,
                        )
                    )
        except Exception as exc:
            log.error({"msg": "email_outreach_monitor_failed", "err": str(exc)})
            return []
        return replies


def _extract_plain_text(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        return ""
    payload = msg.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return str(payload or "")
