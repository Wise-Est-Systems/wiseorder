from __future__ import annotations

import hmac
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import desc, func, select

from configs.settings import get_settings
from core.approvals.gateway import get_gateway
from core.memory.db import ping as db_ping, session_scope
from core.memory.models import Approval, Memory, Task, Workflow
from core.memory.vector import get_vector_store
from core.queues import QueueName, get_queue


def _require_token(
    authorization: str | None = Header(default=None),
) -> None:
    """Reject mutating requests if WISEORDER_API_AUTH_TOKEN is set and the
    Authorization header does not carry a matching bearer token.

    When the env var is empty, this dependency is a no-op (preserves the
    friction-free localhost workflow). When set, every mutating endpoint
    (POST /trigger, POST /approvals/{id}/decide) must include
    ``Authorization: Bearer <token>``.
    """
    expected = get_settings().api_auth_token
    if not expected:
        return
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bearer token required")
    presented = authorization[len("Bearer "):]
    # Constant-time compare: prevents timing-side-channel inference of the token.
    if not hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid token")


if TYPE_CHECKING:
    from core.orchestrator.main import Orchestrator


class TriggerBody(BaseModel):
    type: str
    payload: dict[str, Any] = {}


class DecisionBody(BaseModel):
    decision: str  # "approved" | "rejected"


def build_app(orch: "Orchestrator") -> FastAPI:
    app = FastAPI(title="WiseOrder", version="0.1.0")

    @app.get("/", response_class=HTMLResponse)
    async def dashboard() -> str:
        return _DASHBOARD_HTML

    @app.get("/health")
    @app.get("/healthz")
    async def health() -> dict:
        q = await get_queue()
        return {
            "redis": await q.ping(),
            "queue_depths": await q.all_depths(),
        }

    @app.get("/ready")
    async def ready():
        q = await get_queue()
        db_ok = await db_ping()
        redis_ok = await q.ping()
        body = {"database": db_ok, "redis": redis_ok}
        if not (db_ok and redis_ok):
            return JSONResponse(status_code=503, content=body)
        return body

    @app.get("/tasks")
    async def list_tasks(status: str | None = None, limit: int = 50) -> list[dict]:
        async with session_scope() as session:
            stmt = select(Task).order_by(desc(Task.id)).limit(limit)
            if status:
                stmt = stmt.where(Task.status == status)
            rows = (await session.execute(stmt)).scalars().all()
            return [_task_dict(r) for r in rows]

    @app.get("/workflows")
    async def list_workflows(
        limit: int = 50, status: str | None = None
    ) -> list[dict]:
        async with session_scope() as session:
            stmt = select(Workflow).order_by(desc(Workflow.id)).limit(limit)
            if status:
                stmt = stmt.where(Workflow.status == status)
            rows = (await session.execute(stmt)).scalars().all()
            return [_wf_dict(r) for r in rows]

    @app.get("/workflows/{wf_id}")
    async def get_workflow(wf_id: int) -> dict:
        async with session_scope() as session:
            wf = (await session.execute(
                select(Workflow).where(Workflow.id == wf_id)
            )).scalar_one_or_none()
            if wf is None:
                raise HTTPException(404, "workflow not found")
            tasks = (await session.execute(
                select(Task).where(Task.workflow_id == wf_id).order_by(Task.id)
            )).scalars().all()
            return {**_wf_dict(wf), "tasks": [_task_dict(t) for t in tasks]}

    @app.get("/approvals")
    async def list_approvals(pending: bool = False, limit: int = 50) -> list[dict]:
        async with session_scope() as session:
            stmt = select(Approval).order_by(desc(Approval.id)).limit(limit)
            if pending:
                stmt = stmt.where(Approval.decision.is_(None))
            rows = (await session.execute(stmt)).scalars().all()
            return [_approval_dict(r) for r in rows]

    @app.post(
        "/approvals/{approval_id}/decide",
        dependencies=[Depends(_require_token)],
    )
    async def decide_approval(approval_id: int, body: DecisionBody) -> dict:
        ok = await get_gateway().decide(approval_id, body.decision)
        if not ok:
            raise HTTPException(404, "approval not found")
        return {"approval_id": approval_id, "decision": body.decision}

    @app.get("/watchers")
    async def watchers_status() -> dict:
        """Status of background watchers running inside the orchestrator.

        Today returns IntegrityWatcher state (poll history + current head).
        """
        iw = getattr(orch, "integrity_watcher", None)
        if iw is None:
            return {"integrity_watcher": {"status": "not_running", "history": []}}
        history = [h.to_dict() for h in iw.history[-20:]]
        return {
            "integrity_watcher": {
                "status": "running",
                "chain_dir": str(iw.chain_dir),
                "interval_seconds": iw.interval_seconds,
                "last_known_head": iw.last_head,
                "history": history,
            }
        }

    @app.get("/summary/latest")
    async def latest_daily_summary() -> dict:
        """The most recent daily-summary memory row. Returns
        {"available": False, ...} if none has been generated yet."""
        async with session_scope() as session:
            row = (await session.execute(
                select(Memory)
                .where(Memory.category == "daily_summary")
                .order_by(desc(Memory.id))
                .limit(1)
            )).scalar_one_or_none()
            if row is None:
                return {"available": False, "reason": "no_summary_yet"}
            return {
                "available": True,
                "id": row.id,
                "date": row.meta.get("date") if row.meta else None,
                "summary": row.content,
                "stats": row.meta.get("stats") if row.meta else None,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }

    @app.post("/summary/run-now", dependencies=[Depends(_require_token)])
    async def run_summary_now() -> dict:
        """Manually trigger the daily-summary worker (operator action).

        Idempotent: if today's summary already exists, returns the
        existing record without re-running. Auth-gated when
        WISEORDER_API_AUTH_TOKEN is set."""
        from workflows.daily_summary import run_daily_summary
        return await run_daily_summary()

    @app.get("/memory/search")
    async def memory_search(q: str, n: int = 5) -> list[dict]:
        return get_vector_store().query(q, n_results=n)

    @app.get("/memory/recent")
    async def memory_recent(limit: int = 20) -> list[dict]:
        async with session_scope() as session:
            rows = (await session.execute(
                select(Memory).order_by(desc(Memory.id)).limit(limit)
            )).scalars().all()
            return [
                {
                    "id": r.id, "category": r.category, "content": r.content,
                    "metadata": r.meta,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ]

    @app.get("/logs/recent")
    async def logs_recent(limit: int = 100) -> list[dict]:
        async with session_scope() as session:
            rows = (await session.execute(
                select(Workflow).order_by(desc(Workflow.id)).limit(20)
            )).scalars().all()
            entries: list[dict] = []
            for r in rows:
                for entry in r.logs[-10:]:
                    entries.append({"workflow_id": r.id, **entry})
            entries.sort(key=lambda x: x.get("ts", ""), reverse=True)
            return entries[:limit]

    @app.get("/queues/failed")
    async def queues_failed(limit: int = 50) -> list[dict]:
        """Dead-letter inspection. Returns failed jobs with their last error
        and original payload so an operator can decide whether to investigate,
        replay, or discard."""
        q = await get_queue()
        jobs = await q.peek_failed(limit)
        return [
            {
                "id": j.id, "type": j.type, "attempt": j.attempt,
                "created_at": j.created_at, "notes": j.notes,
                "payload": j.payload,
            }
            for j in jobs
        ]

    @app.get("/stats")
    async def stats() -> dict:
        async with session_scope() as session:
            task_counts = {
                row[0]: int(row[1]) for row in (await session.execute(
                    select(Task.status, func.count(Task.id)).group_by(Task.status)
                )).all()
            }
            wf_counts = {
                row[0]: int(row[1]) for row in (await session.execute(
                    select(Workflow.status, func.count(Workflow.id)).group_by(Workflow.status)
                )).all()
            }
        q = await get_queue()
        return {
            "tasks": task_counts,
            "workflows": wf_counts,
            "queues": await q.all_depths(),
            "vector_count": _safe_vector_count(),
        }

    @app.post(
        "/trigger",
        dependencies=[Depends(_require_token)],
    )
    async def trigger(body: TriggerBody) -> dict:
        job_id = await orch.enqueue_manual(body.type, body.payload)
        return {"job_id": job_id}

    return app


def _safe_vector_count() -> int:
    try:
        return get_vector_store().count()
    except Exception:
        return 0


def _task_dict(t: Task) -> dict:
    return {
        "id": t.id, "type": t.type, "status": t.status,
        "workflow_id": t.workflow_id, "payload": t.payload,
        "result": t.result, "error": t.error,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "completed_at": t.completed_at.isoformat() if t.completed_at else None,
    }


def _wf_dict(w: Workflow) -> dict:
    return {
        "id": w.id, "workflow_name": w.workflow_name, "status": w.status,
        "task_chain": w.task_chain, "logs": w.logs,
        "created_at": w.created_at.isoformat() if w.created_at else None,
        "completed_at": w.completed_at.isoformat() if w.completed_at else None,
    }


def _approval_dict(a: Approval) -> dict:
    return {
        "id": a.id, "workflow_id": a.workflow_id, "task_id": a.task_id,
        "summary": a.summary, "outputs": a.outputs, "affected": a.affected,
        "risk_level": a.risk_level, "decision": a.decision,
        "delivered_via": a.delivered_via,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "decided_at": a.decided_at.isoformat() if a.decided_at else None,
    }


_DASHBOARD_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>WiseOrder</title>
<style>
body{font-family:ui-monospace,Menlo,monospace;background:#0b0d10;color:#dde2e7;margin:0;padding:24px;max-width:1200px}
h1{font-size:18px;letter-spacing:2px;color:#9ec3ff;margin:0 0 16px}
h2{font-size:13px;letter-spacing:1px;color:#7aa7ff;margin:24px 0 8px;border-bottom:1px solid #1f2a36;padding-bottom:4px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}
.card{background:#11161c;border:1px solid #1f2a36;border-radius:6px;padding:12px;font-size:12px}
.k{color:#7d8a99}
.v{color:#dde2e7}
pre{margin:0;white-space:pre-wrap;word-break:break-word;font-size:11px;color:#c8d1da}
button{background:#1c2733;color:#dde2e7;border:1px solid #2a3a4d;border-radius:4px;padding:4px 10px;cursor:pointer;font-family:inherit;font-size:11px;margin-right:6px}
button:hover{background:#243140}
.risk-low{color:#5fd28f}
.risk-medium{color:#f5c065}
.risk-high{color:#ff7a7a}
.s-failed,.s-interrupted{color:#ff7a7a}
.s-completed{color:#5fd28f}
.s-running{color:#7aa7ff}
input{background:#11161c;border:1px solid #2a3a4d;color:#dde2e7;padding:6px 8px;border-radius:4px;font-family:inherit;width:100%;box-sizing:border-box}
</style></head>
<body>
<h1>WISEORDER RUNTIME v0.1</h1>
<div id="stats" class="grid"></div>
<h2>DAILY SUMMARY</h2>
<div id="summary"></div>
<h2>PENDING APPROVALS</h2>
<div id="approvals"></div>
<h2>WATCHERS</h2>
<div id="watchers"></div>
<h2>DEAD-LETTER (FAILED JOBS)</h2>
<div id="failed"></div>
<h2>RECENT WORKFLOWS</h2>
<div id="workflows"></div>
<h2>RECENT TASKS</h2>
<div id="tasks"></div>
<h2>MEMORY SEARCH</h2>
<input id="q" placeholder="search vector memory..." />
<div id="memresults"></div>
<h2>RECENT LOG ENTRIES</h2>
<div id="logs"></div>
<script>
async function j(u,o){return (await fetch(u,o)).json()}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function refresh(){
  const s=await j('/stats')
  document.getElementById('stats').innerHTML=`
    <div class="card"><div class="k">TASKS</div><pre>${esc(JSON.stringify(s.tasks,null,2))}</pre></div>
    <div class="card"><div class="k">WORKFLOWS</div><pre>${esc(JSON.stringify(s.workflows,null,2))}</pre></div>
    <div class="card"><div class="k">QUEUES</div><pre>${esc(JSON.stringify(s.queues,null,2))}</pre></div>
    <div class="card"><div class="k">VECTOR MEMORY</div><div class="v">${s.vector_count} items</div></div>`
  const sm=await j('/summary/latest')
  document.getElementById('summary').innerHTML = sm.available ? `
    <div class="card">
      <div class="k">${esc(sm.date||'')}</div>
      <pre>${esc(sm.summary||'')}</pre>
    </div>` : '<div class="card k">no daily summary yet (will be generated at 09:00 UTC; or POST /summary/run-now to force)</div>'
  const wsr=await j('/watchers')
  const iw=wsr.integrity_watcher||{}
  const last=(iw.history||[]).slice(-3).reverse()
  document.getElementById('watchers').innerHTML = iw.status==='running' ? `
    <div class="card">
      <div class="k">integrity_watcher · ${esc(iw.chain_dir)} · poll ${esc(iw.interval_seconds)}s</div>
      <div>head: <span class="v">${esc(iw.last_known_head||'(none)')}</span></div>
      <pre>${esc(last.map(h=>h.ts.slice(11,19)+' '+h.status+' count='+h.count+(h.note?' note='+h.note:'')).join('\\n'))}</pre>
    </div>` : '<div class="card k">integrity_watcher not running</div>'
  const ap=await j('/approvals?pending=true')
  document.getElementById('approvals').innerHTML=ap.length?ap.map(a=>`
    <div class="card">
      <div><span class="risk-${a.risk_level}">[${a.risk_level.toUpperCase()}]</span> <b>#${a.id}</b> — ${esc(a.summary)}</div>
      <pre>${esc(JSON.stringify(a.outputs,null,2))}</pre>
      <div class="k">files: ${esc((a.affected||[]).join(', '))}</div>
      <div style="margin-top:8px">
        <button onclick="decide(${a.id},'approved')">approve</button>
        <button onclick="decide(${a.id},'rejected')">reject</button>
      </div>
    </div>`).join(''):'<div class="card k">none pending</div>'
  const fl=await j('/queues/failed?limit=10')
  document.getElementById('failed').innerHTML=fl.length?fl.map(f=>`
    <div class="card"><b>${esc(f.type)}</b> id=${esc(f.id)} attempt=${f.attempt}
      <pre>${esc((f.notes||[]).join('\\n'))}</pre>
    </div>`).join(''):'<div class="card k">none</div>'
  const wf=await j('/workflows?limit=10')
  document.getElementById('workflows').innerHTML=wf.map(w=>`
    <div class="card"><b>#${w.id}</b> ${esc(w.workflow_name)} — <span class="s-${w.status}">${esc(w.status)}</span> <span class="k">${esc(w.created_at||'')}</span></div>`).join('')
  const ts=await j('/tasks?limit=15')
  document.getElementById('tasks').innerHTML=ts.map(t=>`
    <div class="card"><b>#${t.id}</b> ${esc(t.type)} — <span class="s-${t.status}">${esc(t.status)}</span> ${t.error?'<pre class="risk-high">'+esc(t.error)+'</pre>':''}</div>`).join('')
  const lg=await j('/logs/recent?limit=20')
  document.getElementById('logs').innerHTML=lg.map(l=>`
    <div class="card"><span class="k">${esc(l.ts||'')}</span> wf#${l.workflow_id} <b>${esc(l.event)}</b> <pre>${esc(JSON.stringify(l.data))}</pre></div>`).join('')
}
async function decide(id,d){await fetch(`/approvals/${id}/decide`,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({decision:d})});refresh()}
document.getElementById('q').addEventListener('keydown',async e=>{
  if(e.key!=='Enter')return
  const r=await j('/memory/search?q='+encodeURIComponent(e.target.value))
  document.getElementById('memresults').innerHTML=r.map(x=>`
    <div class="card"><div class="k">dist=${x.distance?.toFixed?.(3)}</div><pre>${esc(x.document)}</pre></div>`).join('')
})
refresh();setInterval(refresh,5000)
</script>
</body></html>"""
