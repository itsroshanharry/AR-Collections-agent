"""Structured logging with trace and request correlation.

`LOG_FORMAT=json` emits one JSON object per line - what Loki, CloudWatch, Datadog and
every other log pipeline parse natively - carrying the active trace and span id and the
HTTP request id. A log line can then be joined to the trace that produced it, and every
line from one request found by a single id. `text` (the default) stays readable in a
terminal.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import UTC, datetime

from shared.observability.tracing import current_trace_ids

# Set per HTTP request by the dashboard's middleware; empty outside a request.
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")

# Attributes every LogRecord has; anything else was passed via `extra=` and is kept.
# color_message is uvicorn's ANSI-coloured duplicate of the message.
_STANDARD_ATTRS = set(vars(logging.makeLogRecord({}))) | {
    "message",
    "asctime",
    "taskName",
    "request_id",
    "color_message",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        trace_id, span_id = current_trace_ids()
        if trace_id:
            entry["trace_id"] = trace_id
            entry["span_id"] = span_id
        if request_id := request_id_var.get():
            entry["request_id"] = request_id
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                entry[key] = value
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


class _ContextFilter(logging.Filter):
    """Adds request_id to text-format records so the plain format can show it too."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get() or "-"
        return True


TEXT_FORMAT = "%(asctime)s %(levelname)-7s %(name)s [%(request_id)s] | %(message)s"


def configure_logging(fmt: str = "text", level: str = "INFO") -> None:
    """Install one stdout handler on the root logger. Safe to call more than once."""
    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.addFilter(_ContextFilter())
        handler.setFormatter(logging.Formatter(TEXT_FORMAT))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # Uvicorn installs its own handlers; route them through ours for one consistent stream.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers[:] = []
        logging.getLogger(name).propagate = True
