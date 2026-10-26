"""JSON logs on stdout, one line per event, with the request_id (shipped to Log Analytics)."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

from flask import g, has_request_context

# Standard LogRecord attributes; anything else comes from `extra=` and goes into the JSON
_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class RequestIdFilter(logging.Filter):
    """Adds the current request_id to every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = g.get("request_id") if has_request_context() else None
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "service": "backend",
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class StdoutHandler(logging.StreamHandler):
    """Writes to the current sys.stdout (works with pytest output capture)."""

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stdout
        super().emit(record)


def configure_logging(level: str = "INFO") -> None:
    handler = StdoutHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RequestIdFilter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    # The app writes its own access log (with request_id and duration)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
