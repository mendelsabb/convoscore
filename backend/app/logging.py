"""Structured JSON logging.

Every line carries the component and, where relevant, the job id, source, attempt, status and
error type. Conversation content and secrets are never logged: callers pass a content hash prefix
instead of the transcript.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

# Attributes present on every LogRecord; anything else a caller attaches via `extra=` is treated
# as a structured field and included in the JSON output.
_RESERVED = frozenset({
    "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
    "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
    "pathname", "process", "processName", "relativeCreated", "stack_info", "thread",
    "threadName", "taskName",
})


class JsonFormatter(logging.Formatter):
    def __init__(self, component: str) -> None:
        super().__init__()
        self.component = component

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname.lower(),
            "component": self.component,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            # Core fields are never overwritten by a caller's `extra=`: a log line that renamed its
            # own level or component would be actively misleading during an incident.
            if key not in _RESERVED and key not in payload and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["error_class"] = record.exc_info[0].__name__ if record.exc_info[0] else None
            payload["traceback"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(component: str, level: str = "INFO", as_json: bool = True) -> None:
    """Install a single stdout handler. Safe to call more than once."""
    handler = logging.StreamHandler(sys.stdout)
    if as_json:
        handler.setFormatter(JsonFormatter(component))
    else:
        handler.setFormatter(
            logging.Formatter(f"%(asctime)s %(levelname)-5s [{component}] %(name)s: %(message)s")
        )

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    # uvicorn duplicates access logs through its own handlers; let them propagate to ours instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
