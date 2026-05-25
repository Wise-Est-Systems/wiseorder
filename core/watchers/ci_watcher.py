"""CIWatcher — polls GitHub Actions for the canonical repos.

Surfaces:
    * the latest run of every configured (repo, workflow)
    * recent failures
    * "flaky" patterns: workflows that have failed THEN passed within the
      last N runs without a code change between

Read-only against GitHub. Uses the `gh` CLI's stored token; no separate
token management in the wiseorder runtime. If `gh` is not authed, the
watcher records an `auth_unavailable` history entry and keeps polling
(it'll start working as soon as `gh auth login` succeeds).

Configurable via env:
    WISEORDER_CI_REPOS              comma-separated owner/repo entries;
                                    default: "Wise-Est-Systems/wiseorder-protocol,
                                              Wise-Est-Systems/wiseorder"
    WISEORDER_CI_INTERVAL_SECONDS   poll cadence; default 600 (10 min)

Failure modes:
    gh missing       records ERROR with note="gh CLI not installed"; keeps polling
    gh not authed    records ERROR with note="gh CLI not authenticated"; keeps polling
    rate limited     gh CLI handles backoff; we log a warning + a long retry delay
    no recent runs   records OK with note="no runs in last 24h"; not an error
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from configs.logging import get_logger
from configs.settings import get_settings


log = get_logger(__name__)


@dataclass
class WorkflowRun:
    repo: str
    name: str
    status: str       # 'queued' | 'in_progress' | 'completed'
    conclusion: str   # 'success' | 'failure' | 'cancelled' | '' (in flight)
    head_sha: str
    created_at: str
    url: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CICheck:
    ts: str
    repo: str
    status: str           # 'OK' | 'WARN' | 'ERROR'
    note: str = ""
    failed_runs: list[WorkflowRun] = field(default_factory=list)
    in_flight_runs: list[WorkflowRun] = field(default_factory=list)
    flaky_workflows: list[str] = field(default_factory=list)  # workflows with pattern fail→pass

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


class CIWatcher:
    """Polls GitHub Actions for each configured repo on a schedule.

    Lives in the orchestrator's asyncio loop as a background task.
    Stop via stop() (cancellation-aware).
    """

    DEFAULT_REPOS = [
        "Wise-Est-Systems/wiseorder-protocol",
        "Wise-Est-Systems/wiseorder",
    ]

    def __init__(
        self,
        repos: list[str] | None = None,
        interval_seconds: float | None = None,
    ) -> None:
        import os
        env_repos = os.environ.get("WISEORDER_CI_REPOS", "")
        if env_repos.strip():
            self.repos = [r.strip() for r in env_repos.split(",") if r.strip()]
        else:
            self.repos = repos if repos is not None else self.DEFAULT_REPOS
        self.interval_seconds = float(
            interval_seconds
            or os.environ.get("WISEORDER_CI_INTERVAL_SECONDS", "600")
        )
        self.history: dict[str, list[CICheck]] = {r: [] for r in self.repos}
        self._stop = asyncio.Event()

    async def run(self) -> None:
        log.info({
            "msg": "ci_watcher_started",
            "repos": self.repos,
            "interval_seconds": self.interval_seconds,
        })
        # First check on startup so dashboard is not empty
        await self._check_all()
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
                break
            except asyncio.TimeoutError:
                pass
            await self._check_all()
        log.info({"msg": "ci_watcher_stopped"})

    def stop(self) -> None:
        self._stop.set()

    async def _check_all(self) -> None:
        for repo in self.repos:
            check = await self._check_one(repo)
            self.history.setdefault(repo, []).append(check)
            if len(self.history[repo]) > 50:
                self.history[repo] = self.history[repo][-50:]
            if check.status == "ERROR":
                log.warning({
                    "msg": "ci_watcher_error", "repo": repo, "note": check.note,
                })
            elif check.failed_runs or check.flaky_workflows:
                log.info({
                    "msg": "ci_watcher_attention",
                    "repo": repo,
                    "failed": len(check.failed_runs),
                    "flaky": len(check.flaky_workflows),
                })

    async def _check_one(self, repo: str) -> CICheck:
        now = datetime.now(timezone.utc).isoformat()
        gh = shutil.which("gh")
        if gh is None:
            return CICheck(ts=now, repo=repo, status="ERROR", note="gh CLI not installed")
        # Get the last 20 runs for this repo
        try:
            proc = await asyncio.create_subprocess_exec(
                gh, "run", "list",
                "--repo", repo,
                "--limit", "20",
                "--json", "name,status,conclusion,headSha,createdAt,url",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            return CICheck(ts=now, repo=repo, status="ERROR", note="gh run list timed out (30s)")
        except Exception as e:
            return CICheck(ts=now, repo=repo, status="ERROR", note=f"gh subprocess error: {e}")

        if proc.returncode != 0:
            err_text = stderr.decode("utf-8", errors="replace").strip()
            note = "gh not authenticated" if "auth" in err_text.lower() else f"gh rc={proc.returncode}: {err_text[:200]}"
            return CICheck(ts=now, repo=repo, status="ERROR", note=note)

        try:
            runs_data = json.loads(stdout.decode("utf-8"))
        except json.JSONDecodeError as e:
            return CICheck(ts=now, repo=repo, status="ERROR", note=f"json parse: {e}")

        runs = [
            WorkflowRun(
                repo=repo,
                name=str(r.get("name", "?")),
                status=str(r.get("status", "?")),
                conclusion=str(r.get("conclusion") or ""),
                head_sha=str(r.get("headSha", "")),
                created_at=str(r.get("createdAt", "")),
                url=str(r.get("url", "")),
            )
            for r in runs_data
        ]

        failed = [r for r in runs if r.status == "completed" and r.conclusion == "failure"]
        in_flight = [r for r in runs if r.status in {"queued", "in_progress"}]
        flaky = self._detect_flaky_workflows(runs)

        if failed or flaky:
            status = "WARN"
            note = (
                f"{len(failed)} failed run(s), "
                f"{len(flaky)} flaky workflow(s)"
            )
        elif in_flight:
            status = "OK"
            note = f"{len(in_flight)} run(s) in flight"
        else:
            status = "OK"
            note = "all recent runs green"

        return CICheck(
            ts=now,
            repo=repo,
            status=status,
            note=note,
            failed_runs=failed[:5],
            in_flight_runs=in_flight[:5],
            flaky_workflows=flaky,
        )

    def _detect_flaky_workflows(self, runs: list[WorkflowRun]) -> list[str]:
        """A workflow is 'flaky' if within the recent runs we see the same
        (workflow_name, head_sha) pair with both failure AND success.

        That means the workflow was re-run on the same commit and produced
        a different conclusion — usually a transient infrastructure flake.
        """
        seen: dict[tuple[str, str], set[str]] = {}
        for r in runs:
            if r.status != "completed" or not r.conclusion:
                continue
            key = (r.name, r.head_sha)
            seen.setdefault(key, set()).add(r.conclusion)
        flaky: set[str] = set()
        for (name, _sha), conclusions in seen.items():
            if "failure" in conclusions and "success" in conclusions:
                flaky.add(name)
        return sorted(flaky)
