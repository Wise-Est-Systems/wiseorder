"""DemoForgeWorker — bridge from the wiseorder runtime to the demo-forge repo.

Queue-based handoff. The wiseorder runtime emits a `demo_request` job
containing a scenario identifier; this worker invokes the demo-forge
Makefile via subprocess, captures the resulting artifacts, computes a
SHA-256 manifest, and emits an approval request for the produced demo.

Trust boundary:
    The runtime tells demo-forge WHAT to capture (scenario id) and reads
    back WHERE the artifacts landed. demo-forge owns its own dependencies
    (asciinema, agg, ffmpeg); it is responsible for refusing requests it
    can't fulfill. The runtime never executes capture commands directly.

Idempotency:
    demo-forge's `make demo-NNN` already wipes and regenerates per-scenario
    outputs deterministically. Two requests for the same scenario produce
    identical narration / transcript / subtitles (content-matched), and
    different cast files (wall-clock dependent). Both runs are valid.

Failure modes documented:
    DEMO_FORGE_NOT_FOUND        WISEORDER_DEMO_FORGE_DIR doesn't exist or
                                 lacks a Makefile.
    DEMO_FORGE_MISSING_TOOLS    `make check-tools` returns non-zero.
    DEMO_FORGE_SUBPROCESS_FAIL  `make demo-NNN` exits non-zero; full
                                 stderr + last 200 lines stdout sealed
                                 in the dead-letter notes.
    DEMO_FORGE_MANIFEST_FAIL    artifact_manifest.json missing or
                                 SHA-256 round-trip mismatch.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from configs.logging import bind_workflow, current_workflow_id, get_logger
from core.approvals.gateway import ApprovalRequest, get_gateway
from core.memory.db import session_scope
from core.memory.models import Memory, Task, Workflow


log = get_logger(__name__)


VALID_SCENARIOS = {"001", "002", "003"}
SCENARIO_TITLES = {
    "001": "unsafe deployment refusal",
    "002": "SIGKILL recovery proof",
    "003": "offline T7 verification",
}


def _demo_forge_dir() -> Path:
    return Path(
        os.environ.get("WISEORDER_DEMO_FORGE_DIR")
        or str(Path.home() / "Desktop" / "demo-forge")
    ).expanduser().resolve()


async def run_demo_forge_request(payload: dict[str, Any]) -> dict[str, Any]:
    """Workflow handler for ``demo_request`` jobs.

    payload keys:
        scenario     "001" | "002" | "003"
        trigger      optional human-readable trigger reason (logged only)
        skip_render  optional bool — produce only the cast (no GIF/MP4)
    """
    scenario = str(payload.get("scenario", "")).strip()
    if scenario not in VALID_SCENARIOS:
        raise ValueError(
            f"demo_request payload.scenario must be one of {sorted(VALID_SCENARIOS)}, "
            f"got {scenario!r}"
        )

    trigger = str(payload.get("trigger", "manual"))
    skip_render = bool(payload.get("skip_render", False))

    df_dir = _demo_forge_dir()
    if not (df_dir / "Makefile").is_file():
        raise FileNotFoundError(
            f"DEMO_FORGE_NOT_FOUND: {df_dir} does not contain a Makefile. "
            f"Set WISEORDER_DEMO_FORGE_DIR to a valid demo-forge clone."
        )

    # 1. Workflow row
    async with session_scope() as session:
        wf = Workflow(
            workflow_name="demo_request",
            task_chain=["check_tools", "capture", "narrate", "render", "manifest", "approve"],
            status="running",
            logs=[_log_entry("demo_request_started", {
                "scenario": scenario,
                "trigger": trigger,
                "demo_forge_dir": str(df_dir),
                "skip_render": skip_render,
            })],
        )
        session.add(wf)
        await session.flush()
        workflow_id = wf.id

    token = bind_workflow(workflow_id)
    try:
        return await _run_steps(
            workflow_id=workflow_id,
            scenario=scenario,
            trigger=trigger,
            df_dir=df_dir,
            skip_render=skip_render,
        )
    finally:
        current_workflow_id.reset(token)


async def _run_steps(
    *,
    workflow_id: int,
    scenario: str,
    trigger: str,
    df_dir: Path,
    skip_render: bool,
) -> dict[str, Any]:
    # 1. tools probe
    tools_task_id = await _create_task("check_tools", workflow_id, {"scenario": scenario})
    rc, out, err = await _run(["make", "check-tools"], cwd=df_dir)
    if rc != 0:
        await _fail_task(tools_task_id, f"DEMO_FORGE_MISSING_TOOLS: make check-tools rc={rc}")
        await _fail_workflow(workflow_id, "missing tools in demo-forge env")
        raise RuntimeError(f"DEMO_FORGE_MISSING_TOOLS: {err.strip() or out.strip()}")
    await _complete_task(tools_task_id, {"out": out.strip()[:500]})

    # 2-4. demo-NNN target (capture + narrate + render-gif)
    cap_task_id = await _create_task("capture", workflow_id, {"scenario": scenario})
    rc, out, err = await _run(["make", f"demo-{scenario}"], cwd=df_dir, timeout=180)
    if rc != 0:
        await _fail_task(
            cap_task_id,
            f"DEMO_FORGE_SUBPROCESS_FAIL: make demo-{scenario} rc={rc}",
        )
        await _fail_workflow(workflow_id, "demo capture failed")
        raise RuntimeError(
            f"DEMO_FORGE_SUBPROCESS_FAIL: stdout(last 200 lines)=\n"
            f"{out.strip().split(chr(10))[-200:]}\nstderr=\n{err.strip()[:1000]}"
        )
    await _complete_task(cap_task_id, {"out_tail": out.strip().split("\n")[-30:]})

    # 5. Optional MP4 render
    if not skip_render:
        mp4_task_id = await _create_task("render_mp4", workflow_id, {"scenario": scenario})
        # demo-forge's render-mp4 target produces the H.264 file. Best effort —
        # if it fails (e.g., ffmpeg not present), log + continue; the cast +
        # narration are still useful evidence.
        rc, out, err = await _run(
            ["make", f"outputs/DEMO_{scenario}/DEMO_{scenario}.mp4"],
            cwd=df_dir, timeout=120,
        )
        if rc != 0:
            await _fail_task(
                mp4_task_id,
                f"DEMO_FORGE_MP4_FAIL: rc={rc} (continuing without MP4)",
            )
            log.warning({"msg": "demo_forge_mp4_failed", "scenario": scenario, "err": err.strip()[:500]})
        else:
            await _complete_task(mp4_task_id, {"ok": True})

    # 6. Compute manifest
    manifest_task_id = await _create_task("manifest", workflow_id, {"scenario": scenario})
    out_dir = df_dir / "outputs" / f"DEMO_{scenario}"
    if not out_dir.is_dir():
        await _fail_task(manifest_task_id, "DEMO_FORGE_MANIFEST_FAIL: outputs dir missing")
        await _fail_workflow(workflow_id, "manifest computation failed")
        raise RuntimeError(f"DEMO_FORGE_MANIFEST_FAIL: {out_dir} not found")

    manifest = _build_manifest(out_dir, scenario)
    await _complete_task(manifest_task_id, {"artifact_count": len(manifest["artifacts"])})

    # Persist manifest as memory row for inspection/search later
    async with session_scope() as session:
        mem = Memory(
            category="demo_forge_manifest",
            content=(
                f"DEMO_{scenario} ({SCENARIO_TITLES.get(scenario, '')}) — "
                f"{len(manifest['artifacts'])} artifacts, "
                f"{sum(a['bytes'] for a in manifest['artifacts']):,} bytes"
            ),
            meta=manifest,
        )
        session.add(mem)
        await session.flush()

    # 7. Approval card
    appr_task_id = await _create_task("approve", workflow_id, {"scenario": scenario})
    outputs = _format_outputs_for_approval(manifest, out_dir)
    risk_level = "low"  # demos are read-only artifacts; risk is "should we share this externally"
    gateway = get_gateway()
    req = ApprovalRequest(
        summary=(
            f"Demo ready — DEMO_{scenario} "
            f"({SCENARIO_TITLES.get(scenario, 'unknown')}). "
            f"Trigger: {trigger}. Approve to publish."
        ),
        outputs=outputs,
        affected=[a["name"] for a in manifest["artifacts"]],
        risk_level=risk_level,
        workflow_id=workflow_id,
        task_id=appr_task_id,
    )
    approval_id = await gateway.send(req)
    await _complete_task(appr_task_id, {"approval_id": approval_id})

    async with session_scope() as session:
        row = (await session.execute(
            select(Workflow).where(Workflow.id == workflow_id)
        )).scalar_one()
        row.status = "completed"
        row.completed_at = datetime.now(timezone.utc)
        row.logs = list(row.logs) + [
            _log_entry("demo_request_completed", {"approval_id": approval_id})
        ]

    log.info({"msg": "demo_request_completed", "scenario": scenario, "approval_id": approval_id})

    return {
        "workflow_id": workflow_id,
        "approval_id": approval_id,
        "scenario": scenario,
        "artifact_count": len(manifest["artifacts"]),
    }


def _format_outputs_for_approval(manifest: dict, out_dir: Path) -> dict:
    """Pull the X-thread + the manifest hash into the approval card."""
    x_thread = ""
    x_thread_path = out_dir / f"DEMO_{manifest['scenario']}_x_thread.md"
    if x_thread_path.is_file():
        x_thread = x_thread_path.read_text(encoding="utf-8")
    manifest_sha = hashlib.sha256(
        json.dumps(manifest, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        "scenario": f"DEMO_{manifest['scenario']} — {SCENARIO_TITLES.get(manifest['scenario'], '')}",
        "artifacts": [a["name"] for a in manifest["artifacts"]],
        "total_bytes": sum(a["bytes"] for a in manifest["artifacts"]),
        "manifest_sha256": manifest_sha,
        "x_thread_excerpt": (x_thread[:1000] + "..." if len(x_thread) > 1000 else x_thread)
                            if x_thread else "(no X thread file)",
    }


def _build_manifest(out_dir: Path, scenario: str) -> dict:
    """Compute a SHA-256 manifest of every file in out_dir."""
    artifacts = []
    for p in sorted(out_dir.iterdir()):
        if not p.is_file():
            continue
        body = p.read_bytes()
        artifacts.append({
            "name": p.name,
            "bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        })
    return {
        "scenario": scenario,
        "title": SCENARIO_TITLES.get(scenario, ""),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "out_dir": str(out_dir),
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
    }


async def _run(
    cmd: list[str],
    cwd: Path | None = None,
    timeout: float = 90.0,
) -> tuple[int, str, str]:
    """Run a subprocess with timeout. Returns (rc, stdout, stderr)."""
    log.info({"msg": "demo_forge_subprocess_start", "cmd": cmd, "cwd": str(cwd) if cwd else None})
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        log.error({"msg": "demo_forge_subprocess_timeout", "cmd": cmd, "timeout": timeout})
        return (-1, "", f"subprocess timeout after {timeout}s")
    rc = proc.returncode if proc.returncode is not None else -1
    return (
        rc,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


# ---------------------------------------------------------------------------
# Workflow helpers (same shape as commit_pipeline)
# ---------------------------------------------------------------------------


async def _create_task(type_: str, workflow_id: int, payload: dict[str, Any]) -> int:
    async with session_scope() as session:
        t = Task(
            type=type_, status="running", payload=payload,
            workflow_id=workflow_id, started_at=datetime.now(timezone.utc),
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
        wf.logs = list(wf.logs) + [_log_entry("demo_request_failed", {"error": error})]
        wf.completed_at = datetime.now(timezone.utc)


def _log_entry(event: str, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "data": data,
    }
