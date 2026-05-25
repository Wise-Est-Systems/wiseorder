"""DailySummaryWorker — runs once per day, produces a plain-English
summary of the prior 24 hours of operational activity.

What changed.
What passed.
What failed.
What matters.
Recommended actions.

In plain English. No jargon dumping. The output goes to:
- a row in the `memory` table (category='daily_summary') for the dashboard
- a JSON Lines append to `logs/daily_summary.jsonl` for offline reading

This worker is **scheduled, not triggered**. The orchestrator schedules
it via an asyncio task that wakes every WISEORDER_DAILY_SUMMARY_HOUR
(default 09:00 UTC) and writes one summary per day.

Inputs (read-only):
    Postgres: workflows + tasks + approvals rows from the past 24 h
    Redis:    queue depths (HIGH / NORMAL / FAILED) — current snapshot
    File:     logs/wiseorder.jsonl — last 10000 lines for error counts

Outputs:
    memory row with summary text + structured stats
    logs/daily_summary.jsonl appended row

Idempotent: if a summary for today already exists, the worker no-ops.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import desc, func, select

from configs.logging import get_logger
from configs.settings import get_settings
from core.memory.db import session_scope
from core.memory.models import Approval, Memory, Task, Workflow
from core.queues import QueueName, get_queue


log = get_logger(__name__)


@dataclass
class DailyStats:
    period_start_utc: str
    period_end_utc: str
    workflows_completed: int
    workflows_failed: int
    workflows_interrupted: int
    workflows_running: int
    tasks_completed: int
    tasks_failed: int
    approvals_pending: int
    approvals_decided: int
    approvals_approved: int
    approvals_rejected: int
    queue_depth_high: int
    queue_depth_normal: int
    queue_depth_failed: int
    most_common_workflow_names: list[tuple[str, int]]
    most_common_failure_types: list[tuple[str, int]]
    error_log_lines_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def run_daily_summary() -> dict[str, Any]:
    """Compute + persist the 24-hour summary. Returns the summary text and
    stats. Safe to run multiple times in a day; subsequent runs no-op."""
    now = datetime.now(timezone.utc)
    period_start = now - timedelta(hours=24)

    # Idempotency: one summary per UTC date
    today_marker = now.strftime("%Y-%m-%d")
    async with session_scope() as session:
        existing = (await session.execute(
            select(Memory)
            .where(Memory.category == "daily_summary")
            .order_by(desc(Memory.id))
            .limit(1)
        )).scalar_one_or_none()
        if existing and existing.meta and existing.meta.get("date") == today_marker:
            log.info({"msg": "daily_summary_already_exists_for_today", "memory_id": existing.id})
            return {
                "skipped": True,
                "reason": "already_ran_today",
                "memory_id": existing.id,
                "date": today_marker,
            }

    stats = await _gather_stats(period_start, now)
    summary_text = _format_summary(stats, now)

    # Persist as a memory row
    async with session_scope() as session:
        mem = Memory(
            category="daily_summary",
            content=summary_text,
            meta={
                "date": today_marker,
                "period_start_utc": stats.period_start_utc,
                "period_end_utc": stats.period_end_utc,
                "stats": stats.to_dict(),
            },
        )
        session.add(mem)
        await session.flush()
        memory_id = mem.id

    # Append to logs/daily_summary.jsonl for offline reading
    log_dir = Path(get_settings().log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": now.isoformat(),
        "date": today_marker,
        "memory_id": memory_id,
        "stats": stats.to_dict(),
        "summary": summary_text,
    }
    try:
        with (log_dir / "daily_summary.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception as e:
        log.warning({"msg": "daily_summary_file_write_failed", "err": str(e)})

    log.info({"msg": "daily_summary_written", "memory_id": memory_id, "date": today_marker})
    return {
        "memory_id": memory_id,
        "date": today_marker,
        "summary": summary_text,
        "stats": stats.to_dict(),
    }


async def _gather_stats(start: datetime, end: datetime) -> DailyStats:
    async with session_scope() as session:
        wf_status_counts = dict(
            (row[0], int(row[1])) for row in (await session.execute(
                select(Workflow.status, func.count(Workflow.id))
                .where(Workflow.created_at >= start, Workflow.created_at < end)
                .group_by(Workflow.status)
            )).all()
        )
        task_status_counts = dict(
            (row[0], int(row[1])) for row in (await session.execute(
                select(Task.status, func.count(Task.id))
                .where(Task.created_at >= start, Task.created_at < end)
                .group_by(Task.status)
            )).all()
        )
        approval_decision_counts = dict(
            (row[0] or "pending", int(row[1])) for row in (await session.execute(
                select(Approval.decision, func.count(Approval.id))
                .where(Approval.created_at >= start, Approval.created_at < end)
                .group_by(Approval.decision)
            )).all()
        )
        wf_name_counts = [
            (row[0], int(row[1])) for row in (await session.execute(
                select(Workflow.workflow_name, func.count(Workflow.id))
                .where(Workflow.created_at >= start, Workflow.created_at < end)
                .group_by(Workflow.workflow_name)
                .order_by(desc(func.count(Workflow.id)))
                .limit(5)
            )).all()
        ]

        # Failure types (from Task.error text)
        failed_task_errors = (await session.execute(
            select(Task.error)
            .where(
                Task.status == "failed",
                Task.created_at >= start,
                Task.created_at < end,
                Task.error.isnot(None),
            )
            .limit(200)
        )).scalars().all()
        failure_type_counts = Counter(
            _classify_error(e or "") for e in failed_task_errors
        ).most_common(5)

    q = await get_queue()
    depths = await q.all_depths()

    error_lines = _count_recent_error_lines(get_settings().log_dir)

    return DailyStats(
        period_start_utc=start.isoformat(),
        period_end_utc=end.isoformat(),
        workflows_completed=int(wf_status_counts.get("completed", 0)),
        workflows_failed=int(wf_status_counts.get("failed", 0)),
        workflows_interrupted=int(wf_status_counts.get("interrupted", 0)),
        workflows_running=int(wf_status_counts.get("running", 0)),
        tasks_completed=int(task_status_counts.get("completed", 0)),
        tasks_failed=int(task_status_counts.get("failed", 0)),
        approvals_pending=int(approval_decision_counts.get("pending", 0)),
        approvals_decided=int(approval_decision_counts.get("approved", 0)) + int(approval_decision_counts.get("rejected", 0)),
        approvals_approved=int(approval_decision_counts.get("approved", 0)),
        approvals_rejected=int(approval_decision_counts.get("rejected", 0)),
        queue_depth_high=int(depths.get(QueueName.HIGH.name.lower(), 0)),
        queue_depth_normal=int(depths.get(QueueName.NORMAL.name.lower(), 0)),
        queue_depth_failed=int(depths.get(QueueName.FAILED.name.lower(), 0)),
        most_common_workflow_names=wf_name_counts,
        most_common_failure_types=failure_type_counts,
        error_log_lines_count=error_lines,
    )


def _classify_error(text: str) -> str:
    """Map a freeform error string to a short classifier label so the
    summary can count failure shapes without listing every variation."""
    t = text.lower()
    if "timeout" in t or "timed out" in t:
        return "timeout"
    if "connection" in t and ("refused" in t or "reset" in t):
        return "connection_refused"
    if "permission" in t or "authoriz" in t:
        return "permission_denied"
    if "invalid" in t and "json" in t:
        return "json_parse"
    if "no handler" in t:
        return "no_handler"
    if "summarize failed" in t or "draft failed" in t:
        return "llm_failed"
    return "other"


def _count_recent_error_lines(log_dir: str) -> int:
    """Count ERROR-level lines in logs/wiseorder.jsonl over the most recent
    ~10000 lines (cheap O(N) scan; not historical). Returns 0 on any
    parse failure so this never breaks the summary."""
    path = Path(log_dir) / "wiseorder.jsonl"
    if not path.is_file():
        return 0
    try:
        # tail the last ~10k lines for speed
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            read_n = min(size, 1_500_000)  # ~1.5MB ~ ~10k structured log lines
            fh.seek(size - read_n)
            tail = fh.read().decode("utf-8", errors="replace")
    except Exception:
        return 0
    return sum(1 for line in tail.splitlines() if '"levelname": "ERROR"' in line)


def _format_summary(s: DailyStats, now: datetime) -> str:
    """Render the stats as a plain-English narrative. No jargon dumping.

    The reader is an operator scanning their morning. The summary should
    be readable in ~15 seconds and end with a recommended-action line
    when there is one.
    """
    lines: list[str] = []
    lines.append(f"WiseOrder daily summary — {now.strftime('%Y-%m-%d')} (last 24 hours).")
    lines.append("")

    # Headline: workflows + approvals
    completed = s.workflows_completed
    failed = s.workflows_failed
    interrupted = s.workflows_interrupted
    running = s.workflows_running
    pending_approvals = s.approvals_pending

    if completed == 0 and failed == 0 and interrupted == 0:
        lines.append("No workflows ran. The runtime was idle.")
    else:
        parts = []
        parts.append(f"{completed} workflow(s) completed")
        if failed:
            parts.append(f"{failed} failed")
        if interrupted:
            parts.append(f"{interrupted} interrupted")
        if running:
            parts.append(f"{running} still running")
        lines.append(", ".join(parts) + ".")

    if pending_approvals:
        lines.append(
            f"{pending_approvals} approval card(s) pending — open the dashboard."
        )

    if s.approvals_decided:
        ratio = (
            "all approved" if s.approvals_rejected == 0
            else "all rejected" if s.approvals_approved == 0
            else f"{s.approvals_approved} approved, {s.approvals_rejected} rejected"
        )
        lines.append(f"{s.approvals_decided} approval(s) decided ({ratio}).")

    # Queue health
    if s.queue_depth_failed:
        lines.append(
            f"Dead-letter queue has {s.queue_depth_failed} job(s) — "
            f"check /queues/failed."
        )
    if s.queue_depth_high or s.queue_depth_normal:
        lines.append(
            f"Live queues: {s.queue_depth_high} high-priority, "
            f"{s.queue_depth_normal} normal-priority."
        )

    # Failure breakdown
    if s.most_common_failure_types:
        most = s.most_common_failure_types[0]
        if len(s.most_common_failure_types) == 1:
            lines.append(f"Failures were dominated by `{most[0]}` ({most[1]} task(s)).")
        else:
            lines.append(
                f"Top failure shapes: " +
                ", ".join(f"`{kind}` ({n})" for kind, n in s.most_common_failure_types[:3])
                + "."
            )

    # Logs
    if s.error_log_lines_count:
        lines.append(
            f"`logs/wiseorder.jsonl` has {s.error_log_lines_count} ERROR-level line(s) in the recent tail."
        )

    # Recommended action
    lines.append("")
    if pending_approvals:
        lines.append("Recommended action: review the pending approvals first.")
    elif failed or s.queue_depth_failed:
        lines.append(
            "Recommended action: triage the dead-letter queue and the failed workflows."
        )
    elif interrupted:
        lines.append(
            "Recommended action: investigate why workflow(s) were interrupted "
            "(orchestrator restart, crash, or stale running > max-age)."
        )
    else:
        lines.append("Recommended action: none. The runtime is in a clean state.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Scheduler — call from the orchestrator
# ---------------------------------------------------------------------------


async def schedule_daily_summary(hour_utc: int | None = None) -> None:
    """Background coroutine that runs run_daily_summary() once per day at
    the configured UTC hour. The orchestrator's start() spawns this as a
    task; the orphan reaper deals with it on restart.
    """
    target_hour = (
        hour_utc
        if hour_utc is not None
        else int(os.environ.get("WISEORDER_DAILY_SUMMARY_HOUR", "9"))
    )
    log.info({"msg": "daily_summary_scheduler_started", "target_hour_utc": target_hour})
    while True:
        now = datetime.now(timezone.utc)
        # Compute next firing: today at target_hour, or tomorrow if past
        fire = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
        if fire <= now:
            fire += timedelta(days=1)
        sleep_s = (fire - now).total_seconds()
        log.info({
            "msg": "daily_summary_sleeping",
            "next_fire_utc": fire.isoformat(),
            "sleep_seconds": int(sleep_s),
        })
        try:
            await asyncio.sleep(sleep_s)
        except asyncio.CancelledError:
            log.info({"msg": "daily_summary_scheduler_cancelled"})
            return
        try:
            await run_daily_summary()
        except Exception as e:
            log.exception({"msg": "daily_summary_run_failed", "err": str(e)})
