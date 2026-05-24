from __future__ import annotations

import logging
import sys
from pathlib import Path

from pythonjsonlogger import jsonlogger

from configs.settings import get_settings


_configured = False


def configure_logging() -> None:
    global _configured
    if _configured:
        return
    s = get_settings()
    log_dir = Path(s.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(s.log_level)
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = "%(asctime)s %(name)s %(levelname)s %(message)s"
    json_fmt = jsonlogger.JsonFormatter(fmt)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(json_fmt)
    root.addHandler(stream)

    fileh = logging.FileHandler(log_dir / "wiseorder.jsonl")
    fileh.setFormatter(json_fmt)
    root.addHandler(fileh)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)
