from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import desc, func, select

from core.approvals.gateway import get_gateway
from core.memory.db import session_scope
from core.memory.models import Approval, Memory, Task, Workflow
from core.memory.vector import get_vector_store
from core.queues import get_queue


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
    async def health() -> dict:
        q = await get_queue()
        return {
            "redis": await q.ping(),
            "queue_depths": await q.all_depths(),
        }

    @app.get("/tasks")
    async def list_tasks(status: str | None = None, limit: int = 50) -> list[dict]:
        async with session_scope() as session:
            stmt = select(Task).order_by(desc(Task.id)).limit(limit)
            if status:
                stmt = stmt.where(Task.status == status)
            rows = (await session.execute(stmt)).scalars().all()
            return [_task_dict(r) for r in rows]

    @app.get("/workflows")
    async def list_workflows(limit: int = 50) -> list[dict]:
        async with session_scope() as session:
            stmt = select(Workflow).order_by(desc(Workflow.id)).limit(limit)
            rows = (await session.execute(stmt)).scalars().all()
            return [_wf_dict(r) for r in rows]

    @app.get("/workflows/{wf_id}")
    async def get_workflow(wf_id: int) -> dict:
        async with session_scope() as session:
            res = await session.execute(select(Workflow).where(Workflow.id == wf_id))
            wf = res.scalar_one_or_none()
            if wf is None:
                raise HTTPException(404, "workflow not found")
            tasks_res = await session.execute(
                select(Task).where(Task.workflow_id == wf_id).order_by(Task.id)
            )
            tasks = tasks_res.scalars().all()
            return {**_wf_dict(wf), "tasks": [_task_dict(t) for t in tasks]}

    @app.get("/approvals")
    async def list_approvals(pending: bool = False, limit: int = 50) -> list[dict]:
        async with session_scope() as session:
            stmt = select(Approval).order_by(desc(Approval.id)).limit(limit)
            if pending:
                stmt = stmt.where(Approval.decision.is_(None))
            rows = (await session.execute(stmt)).scalars().all()
            return [_approval_dict(r) for r in rows]

    @app.post("/approvals/{approval_id}/decide")
    async def decide_approval(approval_id: int, body: DecisionBody) -> dict:
        ok = await get_gateway().decide(approval_id, body.decision)
        if not ok:
            raise HTTPException(404, "approval not found")
        return {"approval_id": approval_id, "decision": body.decision}

    @app.get("/memory/search")
    async def memory_search(q: str, n: int = 5) -> list[dict]:
        vs = get_vector_store()
        return vs.query(q, n_results=n)

    @app.get("/memory/recent")
    async def memory_recent(limit: int = 20) -> list[dict]:
        async with session_scope() as session:
            stmt = select(Memory).order_by(desc(Memory.id)).limit(limit)
            rows = (await session.execute(stmt)).scalars().all()
            return [
                {
                    "id": r.id,
                    "category": r.category,
                    "content": r.content,
                    "metadata": r.meta,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ]

    @app.get("/logs/recent")
    async def logs_recent(limit: int = 100) -> list[dict]:
        async with session_scope() as session:
            stmt = select(Workflow).order_by(desc(Workflow.id)).limit(20)
            rows = (await session.execute(stmt)).scalars().all()
            entries: list[dict] = []
            for r in rows:
                for entry in r.logs[-10:]:
                    entries.append({"workflow_id": r.id, **entry})
            entries.sort(key=lambda x: x.get("ts", ""), reverse=True)
            return entries[:limit]

    @app.get("/stats")
    async def stats() -> dict:
        async with session_scope() as session:
            res = await session.execute(
                select(Task.status, func.count(Task.id)).group_by(Task.status)
            )
            task_counts = {row[0]: int(row[1]) for row in res.all()}
            res = await session.execute(
                select(Workflow.status, func.count(Workflow.id)).group_by(Workflow.status)
            )
            wf_counts = {row[0]: int(row[1]) for row in res.all()}
        q = await get_queue()
        return {
            "tasks": task_counts,
            "workflows": wf_counts,
            "queues": await q.all_depths(),
            "vector_count": _safe_vector_count(),
        }

    @app.post("/trigger")
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
        "id": t.id,
        "type": t.type,
        "status": t.status,
        "workflow_id": t.workflow_id,
        "payload": t.payload,
        "result": t.result,
        "error": t.error,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "completed_at": t.completed_at.isoformat() if t.completed_at else None,
    }


def _wf_dict(w: Workflow) -> dict:
    return {
        "id": w.id,
        "workflow_name": w.workflow_name,
        "status": w.status,
        "task_chain": w.task_chain,
        "logs": w.logs,
        "created_at": w.created_at.isoformat() if w.created_at else None,
        "completed_at": w.completed_at.isoformat() if w.completed_at else None,
    }


def _approval_dict(a: Approval) -> dict:
    return {
        "id": a.id,
        "workflow_id": a.workflow_id,
        "task_id": a.task_id,
        "summary": a.summary,
        "outputs": a.outputs,
        "affected": a.affected,
        "risk_level": a.risk_level,
        "decision": a.decision,
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
input{background:#11161c;border:1px solid #2a3a4d;color:#dde2e7;padding:6px 8px;border-radius:4px;font-family:inherit;width:100%;box-sizing:border-box}
</style></head>
<body>
<h1>WISEORDER RUNTIME v0.1</h1>
<div id="stats" class="grid"></div>
<h2>PENDING APPROVALS</h2>
<div id="approvals"></div>
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
function el(html){const d=document.createElement('div');d.innerHTML=html;return d.firstElementChild}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function refresh(){
  const s=await j('/stats')
  document.getElementById('stats').innerHTML=`
    <div class="card"><div class="k">TASKS</div><pre>${esc(JSON.stringify(s.tasks,null,2))}</pre></div>
    <div class="card"><div class="k">WORKFLOWS</div><pre>${esc(JSON.stringify(s.workflows,null,2))}</pre></div>
    <div class="card"><div class="k">QUEUES</div><pre>${esc(JSON.stringify(s.queues,null,2))}</pre></div>
    <div class="card"><div class="k">VECTOR MEMORY</div><div class="v">${s.vector_count} items</div></div>`
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
  const wf=await j('/workflows?limit=10')
  document.getElementById('workflows').innerHTML=wf.map(w=>`
    <div class="card"><b>#${w.id}</b> ${esc(w.workflow_name)} — ${esc(w.status)} <span class="k">${esc(w.created_at||'')}</span></div>`).join('')
  const ts=await j('/tasks?limit=15')
  document.getElementById('tasks').innerHTML=ts.map(t=>`
    <div class="card"><b>#${t.id}</b> ${esc(t.type)} — ${esc(t.status)} ${t.error?'<pre class="risk-high">'+esc(t.error)+'</pre>':''}</div>`).join('')
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
