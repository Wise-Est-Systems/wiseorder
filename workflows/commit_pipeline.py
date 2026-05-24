from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from agents.engineering.summarizer import EngineeringSummarizer
from agents.social.post_generator import SocialDrafter
from configs.logging import get_logger
from core.approvals.gateway import ApprovalRequest, get_gateway
from core.memory.db import session_scope
from core.memory.models import Memory, Task, Workflow
from core.memory.vector import get_vector_store


log = get_logger(__name__)


async def run_commit_pipeline(payload: dict[str, Any]) -> dict[str, Any]:
    """Event → summarize → draft social → save → approval request.

    payload keys: repo, sha, prev_sha, author, subject, diff
    """
    repo = payload["repo"]
    sha = payload["sha"]
    subject = payload.get("subject", "")
    author = payload.get("author", "")
    diff = payload.get("diff", "")

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

    async def _append_log(entry: dict[str, Any]) -> None:
        async with session_scope() as session:
            res = await session.execute(select(Workflow).where(Workflow.id == workflow_id))
            row = res.scalar_one()
            row.logs = list(row.logs) + [entry]

    # 1. Engineering summary
    sum_task_id = await _create_task("summarize", workflow_id, {"sha": sha, "repo": repo})
    try:
        summarizer = EngineeringSummarizer()
        eng = await summarizer.summarize(subject=subject, author=author, sha=sha, diff=diff)
        await _complete_task(sum_task_id, eng.to_dict())
        await _append_log(_log_entry("summary_done", {"risk": eng.risk_level}))
    except Exception as e:
        await _fail_task(sum_task_id, str(e))
        await _fail_workflow(workflow_id, f"summarize failed: {e}")
        raise

    # 2. Social draft
    soc_task_id = await _create_task("draft_social", workflow_id, {"sha": sha})
    try:
        drafter = SocialDrafter()
        soc = await drafter.draft(
            summary=eng.summary, changelog=eng.changelog, risk_level=eng.risk_level
        )
        await _complete_task(soc_task_id, soc.to_dict())
        await _append_log(_log_entry("social_done", {"len": len(soc.post)}))
    except Exception as e:
        await _fail_task(soc_task_id, str(e))
        await _fail_workflow(workflow_id, f"draft failed: {e}")
        raise

    # 3. Save to memory (both structured and vector)
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
                    "repo": repo,
                    "sha": sha,
                    "author": author,
                    "risk": eng.risk_level,
                    "changelog": eng.changelog,
                    "files": eng.changed_files,
                    "subject": subject,
                },
            )
            session.add(mem)
            await session.flush()
            memory_id = mem.id

        try:
            vs = get_vector_store()
            vs.add(
                ids=[f"commit:{sha}"],
                documents=[mem.content],
                metadatas=[{"repo": repo, "sha": sha, "risk": eng.risk_level}],
            )
            async with session_scope() as session:
                res = await session.execute(select(Memory).where(Memory.id == memory_id))
                row = res.scalar_one()
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
        gateway = get_gateway()
        req = ApprovalRequest(
            summary=(
                f"Commit {sha[:8]} in {repo.split('/')[-1]}: {subject or '(no subject)'}"
            ),
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
        approval_id = await gateway.send(req)
        await _complete_task(appr_task_id, {"approval_id": approval_id})
        await _append_log(_log_entry("approval_sent", {"id": approval_id}))
    except Exception as e:
        await _fail_task(appr_task_id, str(e))
        await _fail_workflow(workflow_id, f"approval failed: {e}")
        raise

    async with session_scope() as session:
        res = await session.execute(select(Workflow).where(Workflow.id == workflow_id))
        row = res.scalar_one()
        row.status = "completed"
        row.completed_at = datetime.now(timezone.utc)
        row.logs = list(row.logs) + [_log_entry("workflow_completed", {})]

    log.info({"msg": "workflow_completed", "workflow_id": workflow_id, "sha": sha})

    return {
        "workflow_id": workflow_id,
        "approval_id": approval_id,
        "summary": eng.to_dict(),
        "social_post": soc.post,
    }


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
        res = await session.execute(select(Task).where(Task.id == task_id))
        t = res.scalar_one()
        t.status = "completed"
        t.result = result
        t.completed_at = datetime.now(timezone.utc)


async def _fail_task(task_id: int, error: str) -> None:
    async with session_scope() as session:
        res = await session.execute(select(Task).where(Task.id == task_id))
        t = res.scalar_one()
        t.status = "failed"
        t.error = error
        t.completed_at = datetime.now(timezone.utc)


async def _fail_workflow(workflow_id: int, error: str) -> None:
    async with session_scope() as session:
        res = await session.execute(select(Workflow).where(Workflow.id == workflow_id))
        wf = res.scalar_one()
        wf.status = "failed"
        wf.logs = list(wf.logs) + [_log_entry("workflow_failed", {"error": error})]
        wf.completed_at = datetime.now(timezone.utc)


def _log_entry(event: str, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "data": data,
    }
