from __future__ import annotations

from typing import TYPE_CHECKING

from configs.logging import get_logger

if TYPE_CHECKING:
    from workflows.distribution.adapters.base import ChannelAdapter


log = get_logger(__name__)


class ChannelRegistry:
    """In-process registry of distribution channel adapters.

    Adapters register at orchestrator startup; the pipeline looks them up
    by canonical channel name (e.g., "hacker_news", "email_outreach").
    """

    def __init__(self) -> None:
        self._adapters: dict[str, ChannelAdapter] = {}

    def register(self, adapter: ChannelAdapter) -> None:
        name = adapter.channel_name
        if name in self._adapters:
            raise ValueError(f"channel adapter already registered: {name}")
        self._adapters[name] = adapter
        log.info({"msg": "channel_adapter_registered", "channel": name})

    def get(self, channel: str) -> ChannelAdapter:
        try:
            return self._adapters[channel]
        except KeyError as exc:
            raise KeyError(
                f"no channel adapter registered for '{channel}'. "
                f"Registered: {sorted(self._adapters)}"
            ) from exc

    def names(self) -> list[str]:
        return sorted(self._adapters)

    def ready_names(self) -> list[str]:
        from workflows.distribution.types import ChannelStatus

        return sorted(
            name
            for name, a in self._adapters.items()
            if a.status == ChannelStatus.READY
        )

    def degraded_names(self) -> list[str]:
        from workflows.distribution.types import ChannelStatus

        return sorted(
            name
            for name, a in self._adapters.items()
            if a.status == ChannelStatus.DEGRADED
        )


_registry: ChannelRegistry | None = None


def get_registry() -> ChannelRegistry:
    global _registry
    if _registry is None:
        _registry = ChannelRegistry()
    return _registry


def reset_registry() -> None:
    """Test-only: clear the singleton between tests."""
    global _registry
    _registry = None
