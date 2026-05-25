"""IntegrityWatcher — polls the wiseorder-protocol chain on a schedule
and surfaces a divergence event when the head hash changes or when
verification fails.

This is **read-only**: the watcher never mutates the chain or the
protocol repo. It runs `intellagent_runtime.chain.verify_chain` against
a configured chain directory and records the result.

When the head moves (a new triple is appended), the watcher records an
event with the new head + count. The operator gets this on the
dashboard at `/watchers` — they decide whether to investigate.

Configurable via env:
    WISEORDER_PROTOCOL_DIR     — defaults to ~/Desktop/wiseorder-protocol
    WISEORDER_INTEGRITY_CHAIN_REL — chain dir relative to protocol root;
                                     defaults to "chain"
    WISEORDER_INTEGRITY_INTERVAL_SECONDS — poll cadence; default 300 (5 min)

Failure modes:
    chain missing      → records FAIL with reason="chain_missing"; keeps polling
    verify_chain raises → records FAIL with the exception text
    head changed       → records EVENT with new head + diff vs prior head
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from configs.logging import bind_workflow, current_workflow_id, get_logger
from configs.settings import get_settings


log = get_logger(__name__)


@dataclass
class IntegrityCheck:
    ts: str
    status: str   # "CHAIN_VALID" | "CHAIN_TAMPERED" | "CHAIN_INVALID" | "CHAIN_EMPTY" | "ERROR"
    count: int
    head: str | None
    chain_dir: str
    note: str = ""   # extra detail, especially on errors

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class IntegrityWatcher:
    """Polls a wiseorder-protocol chain directory on a schedule.

    Designed to run inside the orchestrator's asyncio loop as a background
    task — does not block, does not require a thread. Low-resource idle
    mode: polls at the configured interval, sleeps in between.
    """

    def __init__(
        self,
        protocol_dir: str | None = None,
        chain_rel: str | None = None,
        interval_seconds: float | None = None,
    ) -> None:
        self.protocol_dir = Path(
            protocol_dir
            or os.environ.get("WISEORDER_PROTOCOL_DIR")
            or str(Path.home() / "Desktop" / "wiseorder-protocol")
        )
        self.chain_rel = chain_rel or os.environ.get("WISEORDER_INTEGRITY_CHAIN_REL", "chain")
        self.interval_seconds = float(
            interval_seconds
            or os.environ.get("WISEORDER_INTEGRITY_INTERVAL_SECONDS", "300")
        )
        # In-memory ring of the most recent checks; the operator reads via /watchers
        self.history: list[IntegrityCheck] = []
        self.last_head: str | None = None
        self._stop = asyncio.Event()

    @property
    def chain_dir(self) -> Path:
        return self.protocol_dir / self.chain_rel

    async def run(self) -> None:
        """Run the watcher loop until stop() is called. Designed to be
        scheduled as an asyncio Task by the orchestrator."""
        log.info(
            {
                "msg": "integrity_watcher_started",
                "chain_dir": str(self.chain_dir),
                "interval_seconds": self.interval_seconds,
            }
        )
        # Initial check on startup so the dashboard isn't empty
        await self._check_once()
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
                break  # stop was set
            except asyncio.TimeoutError:
                pass  # interval elapsed; check
            await self._check_once()
        log.info({"msg": "integrity_watcher_stopped"})

    def stop(self) -> None:
        self._stop.set()

    async def _check_once(self) -> IntegrityCheck:
        """Run one verification cycle. Records the result and (if the head
        moved) logs an event. Returns the IntegrityCheck for callers that
        want it synchronously."""
        check = self._verify_once()
        self.history.append(check)
        # Keep history bounded — most-recent 100 checks
        if len(self.history) > 100:
            self.history = self.history[-100:]

        # Compare to last known head; log if it moved.
        if check.head != self.last_head and check.head is not None and check.status == "CHAIN_VALID":
            prior = self.last_head
            self.last_head = check.head
            log.info(
                {
                    "msg": "integrity_watcher_head_moved",
                    "prior_head": prior,
                    "new_head": check.head,
                    "count": check.count,
                }
            )
        elif check.status not in {"CHAIN_VALID", "CHAIN_EMPTY"}:
            log.warning(
                {
                    "msg": "integrity_watcher_divergence",
                    "status": check.status,
                    "note": check.note,
                }
            )
        return check

    def _verify_once(self) -> IntegrityCheck:
        """Synchronous, blocking-only-on-disk-reads verify. Wrapped by the
        async run() loop. Imports the protocol package on first call;
        records ERROR (not a Python traceback) on import failure so the
        dashboard surfaces a clean message."""
        now = datetime.now(timezone.utc).isoformat()
        if not self.chain_dir.is_dir():
            return IntegrityCheck(
                ts=now,
                status="ERROR",
                count=0,
                head=None,
                chain_dir=str(self.chain_dir),
                note=f"chain directory not found: {self.chain_dir}",
            )

        try:
            # Add the protocol repo to sys.path so we can import intellagent_runtime.chain
            protocol_str = str(self.protocol_dir)
            if protocol_str not in sys.path:
                sys.path.insert(0, protocol_str)
            from intellagent_runtime.chain import verify_chain  # type: ignore
        except ImportError as e:
            return IntegrityCheck(
                ts=now,
                status="ERROR",
                count=0,
                head=None,
                chain_dir=str(self.chain_dir),
                note=f"cannot import protocol verifier: {e}. "
                     f"Set WISEORDER_PROTOCOL_DIR to a clone of wiseorder-protocol.",
            )

        try:
            result = verify_chain(self.chain_dir)
        except Exception as e:
            return IntegrityCheck(
                ts=now,
                status="ERROR",
                count=0,
                head=None,
                chain_dir=str(self.chain_dir),
                note=f"verify_chain raised: {type(e).__name__}: {e}",
            )

        return IntegrityCheck(
            ts=now,
            status=str(result.status),
            count=int(result.count),
            head=result.head,
            chain_dir=str(self.chain_dir),
            note=str(result.reason or ""),
        )
