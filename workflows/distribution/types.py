from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class AskType(str, Enum):
    """The shape of a distribution ask. Maps roughly to adapter behaviour."""

    POST = "post"  # public post (HN, Reddit, Lobsters)
    EMAIL = "email"  # 1:1 email reach
    SCHEDULED_PROMO = "scheduled_promo"  # public scheduled post


class ChannelStatus(str, Enum):
    """Adapter lifecycle states. An adapter exists in the registry only if
    it is at least DEGRADED — fully-non-functional adapters are not
    registered."""

    READY = "ready"
    DEGRADED = "degraded"


@dataclass
class DistributionEvent:
    """Trigger for a distribution attempt.

    Examples of event_type values used by the pipeline:
        - "release_published"
        - "demo_packet_captured"
        - "outreach_campaign_tick"
        - "manual_post_request"

    target_channels narrows the channel set; empty means "use classifier".
    """

    event_type: str
    payload: dict[str, Any]
    target_channels: list[str] = field(default_factory=list)
    recipient: str | None = None  # for EMAIL ask
    ask_type: AskType = AskType.POST
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ChannelDraft:
    """An adapter-shaped draft ready for human approval."""

    channel: str
    ask_type: AskType
    title: str | None
    body: str
    url: str | None = None  # the artifact URL being distributed (for posts)
    recipient: str | None = None  # for EMAIL
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "ask_type": self.ask_type.value,
            "title": self.title,
            "body": self.body,
            "url": self.url,
            "recipient": self.recipient,
            "metadata": self.metadata,
        }


@dataclass
class SubmissionResult:
    """The result of an adapter's submit() call after approval."""

    channel: str
    success: bool
    external_id: str | None = None  # HN item id, Reddit post id, email Message-Id, etc.
    external_url: str | None = None  # public URL of the submission, if any
    error: str | None = None
    submitted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "success": self.success,
            "external_id": self.external_id,
            "external_url": self.external_url,
            "error": self.error,
            "submitted_at": self.submitted_at.isoformat(),
        }


@dataclass
class ReplyEvent:
    """A reply observed by adapter.monitor() — a comment, a DM, an email reply."""

    channel: str
    external_submission_id: str
    reply_id: str
    author: str | None
    body: str
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "external_submission_id": self.external_submission_id,
            "reply_id": self.reply_id,
            "author": self.author,
            "body": self.body,
            "received_at": self.received_at.isoformat(),
        }
