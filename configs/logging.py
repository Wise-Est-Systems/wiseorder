from __future__ import annotations

import contextvars
import logging
import sys
import threading
from pathlib import Path

from pythonjsonlogger import jsonlogger

from configs.settings import get_settings


_configured = False
_configure_lock = threading.Lock()

current_workflow_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "wiseorder_workflow_id", default=None
)
current_job_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "wiseorder_job_id", default=None
)


class CorrelationFilter(logging.Filter):
    """Injects workflow_id and job_id from ContextVars into every record.

    These fields appear as top-level keys in the JSON output, so operators can
    grep `workflow_id=42` to reconstruct the full life of a workflow across
    files, queues, and worker boundaries.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        wf = current_workflow_id.get()
        jid = current_job_id.get()
        if wf is not None:
            record.workflow_id = wf
        if jid is not None:
            record.job_id = jid
        return True


def configure_logging() -> None:
    global _configured
    if _configured:
        return
    with _configure_lock:
        if _configured:
            return
        s = get_settings()
        log_dir = Path(s.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)

        root = logging.getLogger()
        root.setLevel(s.log_level)
        for h in list(root.handlers):
            root.removeHandler(h)

        fmt = "%(asctime)s %(name)s %(levelname)s %(message)s %(workflow_id)s %(job_id)s"
        json_fmt = jsonlogger.JsonFormatter(fmt)
        corr = CorrelationFilter()

        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(json_fmt)
        stream.addFilter(corr)
        root.addHandler(stream)

        fileh = logging.FileHandler(log_dir / "wiseorder.jsonl")
        fileh.setFormatter(json_fmt)
        fileh.addFilter(corr)
        root.addHandler(fileh)

        _configured = True


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)


def bind_workflow(workflow_id: int | None) -> contextvars.Token:
    """Bind a workflow_id to the current async context. Returns a token to reset."""
    return current_workflow_id.set(workflow_id)


def bind_job(job_id: str | None) -> contextvars.Token:
    return current_job_id.set(job_id)
