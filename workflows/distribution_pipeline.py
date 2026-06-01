from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from configs.logging import bind_workflow, current_workflow_id, get_logger
from core.approvals.gateway import ApprovalRequest, get_gateway
from core.memory.db import session_scope
from core.memory.models import Task, Workflow
from workflows.distribution.registry import get_registry
from workflows.distribution.types import (
    AskType,
    ChannelDraft,
    DistributionEvent,
)


log = get_logger(__name__)


async def run_distribution_pipeline(payload: dict[str, Any]) -> dict[str, Any]:
    """Event → classify channels → draft per channel → request approval per draft.

    Payload schema (consumed by DistributionEvent):
        event_type        : str           — required, e.g. "release_published"
        payload           : dict          — required, the artifact payload
        target_channels   : list[str]     — optional, narrow the channel set
        recipient         : str | None    — required for EMAIL ask
        ask_type          : str           — "post" | "email" | "scheduled_promo"

    Approval gates: every produced ChannelDraft is sent through the shared
    core.approvals.gateway. Nothing is submitted to any channel from inside
    this function — submission happens after the operator approves the
    card via the existing FastAPI dashboard.
    """
    event = _event_from_payload(payload)

    async with session_scope() as session:
        wf = Workflow(
            workflow_name="distribution_pipeline",
            task_chain=["classify_channels", "draft_per_channel", "request_approval"],
            status="running",
            logs=[_log_entry("workflow_started", {"event_type": event.event_type})],
        )
        session.add(wf)
        await session.flush()
        workflow_id = wf.id

    token = bind_workflow(workflow_id)
    try:
        return await _run_distribution_steps(workflow_id=workflow_id, event=event)
    finally:
        current_workflow_id.reset(token)


async def _run_distribution_steps(
    *, workflow_id: int, event: DistributionEvent
) -> dict[str, Any]:
    registry = get_registry()

    # 1. Classify channels
    classify_task_id = await _create_task(
        "classify_channels", workflow_id, {"event_type": event.event_type}
    )
    try:
        channels = _classify_channels(event=event, registry=registry)
        if not channels:
            await _complete_task(classify_task_id, {"channels": []})
            await _complete_workflow(
                workflow_id, reason="no_channels_selected"
            )
            return {
                "workflow_id": workflow_id,
                "approvals": [],
                "skipped": True,
                "reason": "no_channels_selected",
            }
        await _complete_task(classify_task_id, {"channels": channels})
        await _append_log(
            workflow_id, _log_entry("channels_classified", {"channels": channels})
        )
    except Exception as exc:
        await _fail_task(classify_task_id, str(exc))
        await _fail_workflow(workflow_id, f"classify failed: {exc}")
        raise

    # 2. Draft per channel
    draft_task_id = await _create_task(
        "draft_per_channel", workflow_id, {"channels": channels}
    )
    drafts: list[ChannelDraft] = []
    try:
        for channel in channels:
            adapter = registry.get(channel)
            draft = await adapter.draft(event)
            drafts.append(draft)
        await _complete_task(
            draft_task_id, {"drafts": [d.to_dict() for d in drafts]}
        )
        await _append_log(
            workflow_id,
            _log_entry(
                "drafts_completed",
                {"channels": [d.channel for d in drafts], "count": len(drafts)},
            ),
        )
    except Exception as exc:
        await _fail_task(draft_task_id, str(exc))
        await _fail_workflow(workflow_id, f"draft failed: {exc}")
        raise

    # 3. Request approval per draft
    appr_task_id = await _create_task(
        "request_approval", workflow_id, {"drafts": len(drafts)}
    )
    approval_ids: list[int] = []
    try:
        gateway = get_gateway()
        for draft in drafts:
            req = _approval_request_from_draft(
                draft=draft, workflow_id=workflow_id, task_id=appr_task_id
            )
            approval_id = await gateway.send(req)
            approval_ids.append(approval_id)
        await _complete_task(appr_task_id, {"approval_ids": approval_ids})
        await _append_log(
            workflow_id,
            _log_entry("approvals_sent", {"approval_ids": approval_ids}),
        )
    except Exception as exc:
        await _fail_task(appr_task_id, str(exc))
        await _fail_workflow(workflow_id, f"approval failed: {exc}")
        raise

    await _complete_workflow(workflow_id)
    return {
        "workflow_id": workflow_id,
        "approvals": approval_ids,
        "drafts": [d.to_dict() for d in drafts],
    }


def _event_from_payload(payload: dict[str, Any]) -> DistributionEvent:
    missing = {"event_type", "payload"} - payload.keys()
    if missing:
        raise ValueError(
            f"distribution_pipeline payload missing required keys: {sorted(missing)}"
        )
    ask_raw = payload.get("ask_type", "post")
    try:
        ask = AskType(ask_raw)
    except ValueError as exc:
        raise ValueError(
            f"distribution_pipeline: unknown ask_type {ask_raw!r}"
        ) from exc
    return DistributionEvent(
        event_type=str(payload["event_type"]),
        payload=dict(payload["payload"] or {}),
        target_channels=list(payload.get("target_channels") or []),
        recipient=payload.get("recipient"),
        ask_type=ask,
    )


def _classify_channels(
    *,
    event: DistributionEvent,
    registry,
) -> list[str]:
    """Pick channels for this event.

    If event.target_channels is set, intersect with READY adapters and use
    that. Otherwise fall back to the per-ask-type default set, intersected
    with READY adapters.
    """
    ready = set(registry.ready_names())
    if event.target_channels:
        return [c for c in event.target_channels if c in ready]
    defaults = {
        AskType.POST: ["hacker_news"],
        AskType.EMAIL: ["email_outreach"],
        AskType.SCHEDULED_PROMO: [],
    }
    return [c for c in defaults.get(event.ask_type, []) if c in ready]


def _approval_request_from_draft(
    *,
    draft: ChannelDraft,
    workflow_id: int,
    task_id: int,
) -> ApprovalRequest:
    summary = (
        f"[{draft.channel}] {draft.title}"
        if draft.title
        else f"[{draft.channel}] {draft.ask_type.value} draft"
    )
    affected: list[str] = []
    if draft.recipient:
        affected.append(f"recipient: {draft.recipient}")
    if draft.url:
        affected.append(f"url: {draft.url}")
    return ApprovalRequest(
        summary=summary,
        outputs={
            "channel": draft.channel,
            "ask_type": draft.ask_type.value,
            "title": draft.title,
            "body": draft.body,
            "url": draft.url,
            "recipient": draft.recipient,
            "metadata": draft.metadata,
        },
        affected=affected,
        risk_level="medium",  # distribution always touches a public-or-1:1 surface
        workflow_id=workflow_id,
        task_id=task_id,
    )


async def _append_log(workflow_id: int, entry: dict[str, Any]) -> None:
    async with session_scope() as session:
        row = (
            await session.execute(select(Workflow).where(Workflow.id == workflow_id))
        ).scalar_one()
        row.logs = list(row.logs) + [entry]


async def _create_task(
    type_: str, workflow_id: int, payload: dict[str, Any]
) -> int:
    async with session_scope() as session:
        t = Task(
            type=type_,
            status="running",
            payload=payload,
            workflow_id=workflow_id,
            started_at=datetime.now(timezone.utc),
        )
        session.add(t)
        await session.flush()
        return t.id


async def _complete_task(task_id: int, result: dict[str, Any]) -> None:
    async with session_scope() as session:
        t = (
            await session.execute(select(Task).where(Task.id == task_id))
        ).scalar_one()
        t.status = "completed"
        t.result = result
        t.completed_at = datetime.now(timezone.utc)


async def _fail_task(task_id: int, error: str) -> None:
    async with session_scope() as session:
        t = (
            await session.execute(select(Task).where(Task.id == task_id))
        ).scalar_one()
        t.status = "failed"
        t.error = error
        t.completed_at = datetime.now(timezone.utc)


async def _fail_workflow(workflow_id: int, error: str) -> None:
    async with session_scope() as session:
        wf = (
            await session.execute(select(Workflow).where(Workflow.id == workflow_id))
        ).scalar_one()
        wf.status = "failed"
        wf.logs = list(wf.logs) + [_log_entry("workflow_failed", {"error": error})]
        wf.completed_at = datetime.now(timezone.utc)


async def _complete_workflow(workflow_id: int, *, reason: str | None = None) -> None:
    async with session_scope() as session:
        wf = (
            await session.execute(select(Workflow).where(Workflow.id == workflow_id))
        ).scalar_one()
        wf.status = "completed"
        wf.completed_at = datetime.now(timezone.utc)
        wf.logs = list(wf.logs) + [
            _log_entry("workflow_completed", {"reason": reason} if reason else {})
        ]


def _log_entry(event: str, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "data": data,
    }
