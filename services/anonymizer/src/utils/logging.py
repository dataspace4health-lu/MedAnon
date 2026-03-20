"""Structured JSON logging + async-safe request ID propagation.

Usage:
    from utils.logging import REQUEST_ID, setup_logging

    setup_logging(level="INFO")   # call once at startup

    # In middleware:
    token = REQUEST_ID.set(str(uuid.uuid4()))
    try:
        ...
    finally:
        REQUEST_ID.reset(token)
"""

import json
import logging
from contextvars import ContextVar

REQUEST_ID: ContextVar[str] = ContextVar("request_id", default="-")


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per log record.

    Fields: ts, level, logger, service, request_id, msg
    """

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "service": "medanon",
            "request_id": REQUEST_ID.get("-"),
            "msg": record.getMessage(),
        })


def setup_logging(level: str = "INFO") -> None:
    """Replace the root logger's handlers with a single JSON stream handler."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
