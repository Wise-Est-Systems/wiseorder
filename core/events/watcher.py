from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from watchdog.events import FileSystemEventHandler, FileSystemEvent
from watchdog.observers import Observer

from configs.logging import get_logger
from configs.settings import get_settings
from core.queues import QueueName, get_queue
from core.queues.redis_queue import Job


log = get_logger(__name__)


@dataclass
class GitCommitEvent:
    repo: str
    sha: str
    author: str
    subject: str
    diff: str


class _GitHeadHandler(FileSystemEventHandler):
    """Watches `.git/HEAD` / `.git/refs/heads/*` / `.git/logs/HEAD` for commit changes."""

    def __init__(self, watcher: "EventWatcher", repo_path: Path) -> None:
        super().__init__()
        self.watcher = watcher
        self.repo_path = repo_path
        self._last_sha = _current_head_sha(repo_path)

    def on_any_event(self, event: FileSystemEvent) -> None:  # pragma: no cover (file io)
        src = Path(event.src_path)
        if ".git" not in src.parts:
            return
        if src.name not in {"HEAD", "ORIG_HEAD"} and "refs/heads" not in str(src) and "logs/HEAD" not in str(src):
            return
        new_sha = _current_head_sha(self.repo_path)
        if new_sha and new_sha != self._last_sha:
            prev = self._last_sha
            self._last_sha = new_sha
            asyncio.run_coroutine_threadsafe(
                self.watcher._emit_commit(self.repo_path, prev, new_sha),
                self.watcher.loop,
            )


def _current_head_sha(repo: Path) -> str | None:
    try:
        out = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
        )
        return out.decode().strip()
    except Exception:
        return None


def _read_commit(repo: Path, sha: str) -> tuple[str, str, str]:
    """Return (author, subject, diff)."""
    try:
        author = subprocess.check_output(
            ["git", "-C", str(repo), "log", "-1", "--format=%an <%ae>", sha],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        author = "unknown"
    try:
        subject = subprocess.check_output(
            ["git", "-C", str(repo), "log", "-1", "--format=%s", sha],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        subject = ""
    try:
        diff = subprocess.check_output(
            ["git", "-C", str(repo), "show", "--stat", "--patch", sha],
            stderr=subprocess.DEVNULL,
        ).decode(errors="replace")
        if len(diff) > 200_000:
            diff = diff[:200_000] + "\n\n[diff truncated]\n"
    except Exception:
        diff = ""
    return author, subject, diff


class EventWatcher:
    """Detects git commits across configured repo paths and enqueues commit jobs.

    Single-process; runs an asyncio loop alongside the Watchdog Observer thread.
    """

    def __init__(self, paths: Iterable[str | Path] | None = None) -> None:
        s = get_settings()
        configured = list(paths) if paths is not None else list(s.watch_paths)
        self.repos: list[Path] = [Path(p).expanduser().resolve() for p in configured]
        self._observer: Observer | None = None
        self.loop: asyncio.AbstractEventLoop | None = None

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        if not self.repos:
            log.warning({"msg": "event_watcher_no_paths", "hint": "set WISEORDER_WATCH_PATHS"})
            return
        observer = Observer()
        for repo in self.repos:
            if not (repo / ".git").exists():
                log.warning({"msg": "event_watcher_skip_non_git", "path": str(repo)})
                continue
            handler = _GitHeadHandler(self, repo)
            observer.schedule(handler, str(repo / ".git"), recursive=True)
            log.info({"msg": "event_watcher_watching", "path": str(repo)})
        observer.start()
        self._observer = observer

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None

    async def _emit_commit(self, repo: Path, prev_sha: str | None, new_sha: str) -> None:
        author, subject, diff = _read_commit(repo, new_sha)
        event = GitCommitEvent(
            repo=str(repo), sha=new_sha, author=author, subject=subject, diff=diff
        )
        log.info(
            {
                "msg": "commit_detected",
                "repo": event.repo,
                "sha": event.sha,
                "subject": event.subject,
            }
        )
        q = await get_queue()
        job = Job.new(
            type="commit_pipeline",
            payload={
                "repo": event.repo,
                "sha": event.sha,
                "prev_sha": prev_sha,
                "author": event.author,
                "subject": event.subject,
                "diff": event.diff,
            },
        )
        await q.enqueue(job, queue=QueueName.NORMAL)
