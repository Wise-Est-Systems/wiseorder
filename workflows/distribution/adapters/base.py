from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from workflows.distribution.types import (
    ChannelDraft,
    ChannelStatus,
    DistributionEvent,
    ReplyEvent,
    SubmissionResult,
)


class ChannelAdapter(ABC):
    """Contract every distribution channel must satisfy.

    The pipeline never calls submit() before the draft has been approved by
    an operator via the shared core.approvals.gateway. Adapters MUST NOT
    perform any outbound network I/O in draft(); draft() is LLM + local
    formatting only.
    """

    channel_name: str  # e.g., "hacker_news", "email_outreach"
    status: ChannelStatus  # READY / DEGRADED

    @abstractmethod
    async def draft(self, event: DistributionEvent) -> ChannelDraft:
        """Produce a channel-shaped draft from a distribution event.

        Must not perform outbound network I/O to the channel itself; may use
        the LLM dispatch shared with agents.engineering.summarizer.
        """

    @abstractmethod
    async def submit(self, draft: ChannelDraft) -> SubmissionResult:
        """Submit an *approved* draft to the channel.

        Called only after the approval gate returns 'approved'. Errors are
        returned as SubmissionResult(success=False, error=...), not raised.
        """

    @abstractmethod
    async def monitor(self, external_submission_id: str) -> list[ReplyEvent]:
        """Poll the channel for replies on a previously-submitted draft.

        Adapter implementations decide pagination, deduplication, and
        idle-state behaviour. Returning [] is the no-new-replies signal.
        """

    def health(self) -> dict[str, Any]:
        """Lightweight, sync, no-I/O health probe. Returned as part of /distribution/status."""
        return {"channel": self.channel_name, "status": self.status.value}
