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


async def run_cross_post_pipeline(payload: dict[str, Any]) -> dict[str, Any]:
    """Fan-out distribution event → drafts across all READY adapters → one
    approval card per draft.

    Mirrors run_distribution_pipeline's shape but with a different default
    channel-selection policy: when ``target_channels`` is empty AND
    ``ask_type`` is POST, fire on *every* READY adapter (not the per-ask
    default). This is what you want for a release-day announcement.

    Payload schema:
        event_type        : str        (required)
        payload           : dict       (required)  — must contain 'url'
        target_channels   : list[str]  (optional)  — narrows the fan-out
        ask_type          : str        (defaults to 'post')
    """
    event = _event_from_payload(payload)

    async with session_scope() as session:
        wf = Workflow(
            workflow_name="cross_post_pipeline",
            task_chain=["select_channels", "draft_per_channel", "request_approval"],
            status="running",
            logs=[_log_entry("workflow_started", {"event_type": event.event_type})],
        )
        session.add(wf)
        await session.flush()
        workflow_id = wf.id

    token = bind_workflow(workflow_id)
    try:
        return await _run_steps(workflow_id=workflow_id, event=event)
    finally:
        current_workflow_id.reset(token)


async def _run_steps(*, workflow_id: int, event: DistributionEvent) -> dict[str, Any]:
    registry = get_registry()

    select_task_id = await _create_task(
        "select_channels", workflow_id, {"event_type": event.event_type}
    )
    try:
        channels = _select_channels(event=event, registry=registry)
        await _complete_task(select_task_id, {"channels": channels})
    except Exception as exc:
        await _fail_task(select_task_id, str(exc))
        await _fail_workflow(workflow_id, f"select failed: {exc}")
        raise

    if not channels:
        await _complete_workflow(workflow_id, reason="no_ready_adapters")
        return {
            "workflow_id": workflow_id,
            "approvals": [],
            "skipped": True,
            "reason": "no_ready_adapters",
        }

    draft_task_id = await _create_task(
        "draft_per_channel", workflow_id, {"channels": channels}
    )
    drafts: list[ChannelDraft] = []
    draft_errors: dict[str, str] = {}
    for channel in channels:
        adapter = registry.get(channel)
        try:
            draft = await adapter.draft(event)
            drafts.append(draft)
        except Exception as exc:
            log.warning(
                {"msg": "cross_post_draft_error", "channel": channel, "err": str(exc)}
            )
            draft_errors[channel] = str(exc)
    await _complete_task(
        draft_task_id,
        {
            "drafts": [d.to_dict() for d in drafts],
            "errors": draft_errors,
        },
    )

    if not drafts:
        await _fail_workflow(workflow_id, f"no drafts produced; errors: {draft_errors}")
        return {
            "workflow_id": workflow_id,
            "approvals": [],
            "skipped": False,
            "errors": draft_errors,
        }

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
    except Exception as exc:
        await _fail_task(appr_task_id, str(exc))
        await _fail_workflow(workflow_id, f"approval failed: {exc}")
        raise

    await _complete_workflow(workflow_id)
    return {
        "workflow_id": workflow_id,
        "approvals": approval_ids,
        "drafts": [d.to_dict() for d in drafts],
        "draft_errors": draft_errors,
    }


def _event_from_payload(payload: dict[str, Any]) -> DistributionEvent:
    missing = {"event_type", "payload"} - payload.keys()
    if missing:
        raise ValueError(
            f"cross_post_pipeline payload missing required keys: {sorted(missing)}"
        )
    ask_raw = payload.get("ask_type", "post")
    try:
        ask = AskType(ask_raw)
    except ValueError as exc:
        raise ValueError(
            f"cross_post_pipeline: unknown ask_type {ask_raw!r}"
        ) from exc
    return DistributionEvent(
        event_type=str(payload["event_type"]),
        payload=dict(payload["payload"] or {}),
        target_channels=list(payload.get("target_channels") or []),
        recipient=payload.get("recipient"),
        ask_type=ask,
    )


def _select_channels(
    *,
    event: DistributionEvent,
    registry,
) -> list[str]:
    """Cross-post default: fan out across *every* READY adapter.

    Differs from distribution_pipeline._classify_channels (which uses a
    per-ask-type singleton default). target_channels still narrows the set
    if explicitly provided.
    """
    ready = registry.ready_names()
    if event.target_channels:
        ready_set = set(ready)
        return [c for c in event.target_channels if c in ready_set]
    return list(ready)


def _approval_request_from_draft(
    *,
    draft: ChannelDraft,
    workflow_id: int,
    task_id: int,
) -> ApprovalRequest:
    summary = (
        f"[cross-post → {draft.channel}] {draft.title}"
        if draft.title
        else f"[cross-post → {draft.channel}] {draft.ask_type.value}"
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
        risk_level="medium",
        workflow_id=workflow_id,
        task_id=task_id,
    )


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
