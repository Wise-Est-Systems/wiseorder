from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text

from agents.engineering.summarizer import EngineeringSummarizer
from agents.social.post_generator import SocialDrafter
from configs.logging import bind_workflow, current_workflow_id, get_logger
from core.approvals.gateway import ApprovalRequest, get_gateway
from core.memory.db import session_scope
from core.memory.models import Memory, Task, Workflow
from core.memory.vector import get_vector_store


log = get_logger(__name__)


async def run_commit_pipeline(payload: dict[str, Any]) -> dict[str, Any]:
    """Event → summarize → draft social → save → approval request.

    Idempotent on (repo, sha): if a workflow for this commit is already
    running or completed, returns its workflow_id and skips re-processing.

    payload keys: repo, sha, prev_sha, author, subject, diff
    """
    _require_keys(payload, {"repo", "sha"})
    repo = payload["repo"]
    sha = payload["sha"]
    subject = payload.get("subject", "")
    author = payload.get("author", "")
    diff = payload.get("diff", "")

    existing = await _find_existing_workflow(repo, sha)
    if existing is not None:
        log.info({"msg": "commit_pipeline_idempotent_skip",
                  "existing_workflow_id": existing, "sha": sha, "repo": repo})
        return {"workflow_id": existing, "skipped": True, "reason": "duplicate_sha"}

    async with session_scope() as session:
        wf = Workflow(
            workflow_name="commit_pipeline",
            task_chain=["summarize", "draft_social", "save", "request_approval"],
            status="running",
            logs=[_log_entry("workflow_started", {"repo": repo, "sha": sha})],
        )
        session.add(wf)
        await session.flush()
        workflow_id = wf.id

    token = bind_workflow(workflow_id)
    try:
        return await _run_pipeline_steps(
            workflow_id=workflow_id,
            repo=repo, sha=sha, subject=subject, author=author, diff=diff,
        )
    finally:
        current_workflow_id.reset(token)


async def _run_pipeline_steps(
    *,
    workflow_id: int,
    repo: str,
    sha: str,
    subject: str,
    author: str,
    diff: str,
) -> dict[str, Any]:
    # 1. Engineering summary
    sum_task_id = await _create_task("summarize", workflow_id, {"sha": sha, "repo": repo})
    try:
        eng = await EngineeringSummarizer().summarize(
            subject=subject, author=author, sha=sha, diff=diff
        )
        await _complete_task(sum_task_id, eng.to_dict())
        await _append_log(workflow_id, _log_entry("summary_done", {"risk": eng.risk_level}))
    except Exception as e:
        await _fail_task(sum_task_id, str(e))
        await _fail_workflow(workflow_id, f"summarize failed: {e}")
        raise

    # 2. Social draft
    soc_task_id = await _create_task("draft_social", workflow_id, {"sha": sha})
    try:
        soc = await SocialDrafter().draft(
            summary=eng.summary, changelog=eng.changelog, risk_level=eng.risk_level
        )
        await _complete_task(soc_task_id, soc.to_dict())
        await _append_log(workflow_id, _log_entry("social_done", {"len": len(soc.post)}))
    except Exception as e:
        await _fail_task(soc_task_id, str(e))
        await _fail_workflow(workflow_id, f"draft failed: {e}")
        raise

    # 3. Save to memory (structured + vector)
    save_task_id = await _create_task("save", workflow_id, {"sha": sha})
    try:
        async with session_scope() as session:
            mem = Memory(
                category="commit_summary",
                content=(
                    f"[{sha[:8]}] {subject}\n\n{eng.summary}\n\n"
                    f"Changelog: {eng.changelog}\nRisk: {eng.risk_level}\n"
                    f"Files: {', '.join(eng.changed_files)}"
                ),
                meta={
                    "repo": repo, "sha": sha, "author": author,
                    "risk": eng.risk_level, "changelog": eng.changelog,
                    "files": eng.changed_files, "subject": subject,
                },
            )
            session.add(mem)
            await session.flush()
            memory_id = mem.id
            content_for_vector = mem.content

        try:
            get_vector_store().upsert(
                ids=[f"commit:{sha}"],
                documents=[content_for_vector],
                metadatas=[{"repo": repo, "sha": sha, "risk": eng.risk_level}],
            )
            async with session_scope() as session:
                row = (await session.execute(
                    select(Memory).where(Memory.id == memory_id)
                )).scalar_one()
                row.embedding_id = f"commit:{sha}"
        except Exception as e:
            log.warning({"msg": "vector_index_failed", "err": str(e), "sha": sha})

        await _complete_task(save_task_id, {"memory_id": memory_id})
    except Exception as e:
        await _fail_task(save_task_id, str(e))
        await _fail_workflow(workflow_id, f"save failed: {e}")
        raise

    # 4. Approval request
    appr_task_id = await _create_task("request_approval", workflow_id, {"sha": sha})
    try:
        req = ApprovalRequest(
            summary=f"Commit {sha[:8]} in {repo.split('/')[-1]}: {subject or '(no subject)'}",
            outputs={
                "engineering_summary": eng.summary,
                "changelog": eng.changelog,
                "social_post": soc.post,
            },
            affected=eng.changed_files,
            risk_level=eng.risk_level,
            workflow_id=workflow_id,
            task_id=appr_task_id,
        )
        approval_id = await get_gateway().send(req)
        await _complete_task(appr_task_id, {"approval_id": approval_id})
        await _append_log(workflow_id, _log_entry("approval_sent", {"id": approval_id}))
    except Exception as e:
        await _fail_task(appr_task_id, str(e))
        await _fail_workflow(workflow_id, f"approval failed: {e}")
        raise

    async with session_scope() as session:
        row = (await session.execute(
            select(Workflow).where(Workflow.id == workflow_id)
        )).scalar_one()
        row.status = "completed"
        row.completed_at = datetime.now(timezone.utc)
        row.logs = list(row.logs) + [_log_entry("workflow_completed", {})]

    log.info({"msg": "workflow_completed", "sha": sha})

    return {
        "workflow_id": workflow_id,
        "approval_id": approval_id,
        "summary": eng.to_dict(),
        "social_post": soc.post,
    }


def _require_keys(payload: dict[str, Any], required: set[str]) -> None:
    missing = required - payload.keys()
    if missing:
        raise ValueError(f"commit_pipeline payload missing required keys: {sorted(missing)}")


async def _find_existing_workflow(repo: str, sha: str) -> int | None:
    """Return workflow_id if a running/completed workflow for (repo, sha) exists, else None.

    Uses the JSONB @> containment operator against the workflow's first log
    entry, which holds {"event": "workflow_started", "data": {"repo": ..., "sha": ...}}.
    """
    marker = json.dumps([{"data": {"sha": sha, "repo": repo}}])
    async with session_scope() as session:
        res = await session.execute(
            text(
                "SELECT id FROM workflows "
                "WHERE workflow_name = 'commit_pipeline' "
                "  AND logs @> CAST(:marker AS jsonb) "
                "  AND status IN ('running', 'completed') "
                "ORDER BY id ASC LIMIT 1"
            ),
            {"marker": marker},
        )
        row = res.first()
        return int(row[0]) if row else None


async def _append_log(workflow_id: int, entry: dict[str, Any]) -> None:
    async with session_scope() as session:
        row = (await session.execute(
            select(Workflow).where(Workflow.id == workflow_id)
        )).scalar_one()
        row.logs = list(row.logs) + [entry]


async def _create_task(type_: str, workflow_id: int, payload: dict[str, Any]) -> int:
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
        t = (await session.execute(select(Task).where(Task.id == task_id))).scalar_one()
        t.status = "completed"
        t.result = result
        t.completed_at = datetime.now(timezone.utc)


async def _fail_task(task_id: int, error: str) -> None:
    async with session_scope() as session:
        t = (await session.execute(select(Task).where(Task.id == task_id))).scalar_one()
        t.status = "failed"
        t.error = error
        t.completed_at = datetime.now(timezone.utc)


async def _fail_workflow(workflow_id: int, error: str) -> None:
    async with session_scope() as session:
        wf = (await session.execute(
            select(Workflow).where(Workflow.id == workflow_id)
        )).scalar_one()
        wf.status = "failed"
        wf.logs = list(wf.logs) + [_log_entry("workflow_failed", {"error": error})]
        wf.completed_at = datetime.now(timezone.utc)


def _log_entry(event: str, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "data": data,
    }
